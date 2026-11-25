// 火山 RTC 视频订阅：只订阅机器狗发布的视频，不采集浏览器摄像头。
export function createRtcViewer(container, show, basePath = '/quadruped'){
  let engine = null, active = false, config = null;
  let reconnectTimer = null, firstFrame = false, generation = 0;
  let watchdogTimer = null, probeTimer = null, trackedVideo = null;
  let lastFrameAt = 0, lastRemoteFrameCount = null, videoVisible = true;
  let frameToken = 0;
  let frameTracking = false;
  const FRAME_STALL_MS = 6000;

  function setVideoVisible(visible){
    if (visible === videoVisible) return;
    videoVisible = visible;
    container.style.visibility = visible ? '' : 'hidden';
    container.style.opacity = visible ? '' : '0';
  }

  function noteFrame(){
    if (!active) return;
    const resumed = !firstFrame;
    lastFrameAt = performance.now();
    firstFrame = true;
    clearReconnect();
    setVideoVisible(true);
    if (resumed) show('');
  }

  function streamLost(message, reconnectDelay = 3000){
    if (!active) return;
    firstFrame = false;
    lastFrameAt = 0;
    setVideoVisible(false);
    show(message);
    scheduleReconnect(reconnectDelay);
  }

  function attachVideoFrames(){
    if (!active) return;
    const video = container.querySelector('video');
    if (video && video !== trackedVideo){
      trackedVideo = video;
      if (typeof video.requestVideoFrameCallback === 'function'){
        frameTracking = true;
        const token = frameToken;
        const onFrame = () => {
          if (!active || token !== frameToken || trackedVideo !== video) return;
          noteFrame();
          video.requestVideoFrameCallback(onFrame);
        };
        video.requestVideoFrameCallback(onFrame);
      }
    }
    clearTimeout(probeTimer);
    if (active && !trackedVideo){
      probeTimer = setTimeout(attachVideoFrames, 250);
    }
  }

  function remoteFrameCount(value){
    const videoStats = value?.videoStats || value?.stats?.videoStats || value?.remoteVideoStats;
    if (!videoStats || typeof videoStats !== 'object') return null;
    for (const key of [
      'receivedFrameCount', 'receivedFrames', 'decodedFrameCount',
      'renderedFrameCount', 'frameCount',
    ]){
      const count = Number(videoStats[key]);
      if (Number.isFinite(count)) return count;
    }
    return null;
  }

  function startWatchdog(){
    clearInterval(watchdogTimer);
    watchdogTimer = setInterval(() => {
      if (!active || !firstFrame || !frameTracking || document.hidden) return;
      if (performance.now() - lastFrameAt >= FRAME_STALL_MS){
        streamLost('RTC 视频已中断，正在隐藏最后画面并重连…');
      }
    }, 1000);
  }

  function stopWatchdog(){
    clearInterval(watchdogTimer);
    clearTimeout(probeTimer);
    watchdogTimer = null;
    probeTimer = null;
    trackedVideo = null;
  }

  async function loadConfig(){
    const robotId = new URLSearchParams(location.search).get('robot_id');
    const query = robotId ? `&robot_id=${encodeURIComponent(robotId)}` : '';
    const response = await fetch(`${basePath}/rtc/config?role=viewer${query}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`RTC 配置失败（${response.status}）`);
    return response.json();
  }

  async function playPublished(e){
    if (!active || !engine) return;
    const userId = e?.userId || e?.user?.userId;
    if (!userId || userId !== config.publisher_id) return;
    try {
      firstFrame = false;
      lastFrameAt = 0;
      setVideoVisible(false);
      show('正在等待 RTC 首帧…');
      await engine.subscribeStream(userId, VERTC.MediaType.VIDEO);
      Promise.resolve(engine.setRemoteVideoPlayer(VERTC.StreamIndex.STREAM_INDEX_MAIN, {
        userId,
        renderDom: container,
      })).then(attachVideoFrames)
        .catch(error => show(`RTC 播放失败：${error?.message || error}`));
    } catch (error){
      show(`RTC 播放失败：${error?.message || error}`);
    }
  }

  function clearReconnect(){
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function scheduleReconnect(delay = 12000){
    clearReconnect();
    reconnectTimer = setTimeout(async () => {
      reconnectTimer = null;
      if (!active || firstFrame) return;
      show('RTC 无响应，正在重连…');
      const current = engine;
      engine = null;
      if (current){
        try { await current.leaveRoom(); } catch {}
        try { VERTC.destroyEngine(current); } catch {}
      }
      if (active) await connect();
    }, delay);
  }

  async function connect(){
    if (!active) return;
    const currentGeneration = ++generation;
    firstFrame = false;
    lastFrameAt = 0;
    lastRemoteFrameCount = null;
    frameTracking = false;
    frameToken++;
    trackedVideo = null;
    setVideoVisible(false);
    show('正在申请 RTC 房间…');
    try {
      if (!window.VERTC) throw new Error('火山 RTC Web SDK 没有加载');
      config = await loadConfig();
      if (!active || currentGeneration !== generation) return;
      const current = VERTC.createEngine(config.app_id);
      engine = current;
      const refreshToken = async () => {
        try {
          const next = await loadConfig();
          if (active && engine === current) await current.updateToken(next.token);
        } catch (error){
          show(`RTC Token 更新失败：${error?.message || error}`);
        }
      };
      current.on(VERTC.events.onUserPublishStream, playPublished);
      current.on(VERTC.events.onVideoFirstFrameDecoded, () => {
        if (active && engine === current){
          noteFrame();
          attachVideoFrames();
        }
      });
      current.on(VERTC.events.onRemoteStreamStats, value => {
        if (!active || engine !== current) return;
        const count = remoteFrameCount(value);
        if (count == null) return;
        frameTracking = true;
        if (lastRemoteFrameCount != null && count > lastRemoteFrameCount){
          noteFrame();
        }
        lastRemoteFrameCount = count;
      });
      current.on(VERTC.events.onError, e => {
        if (active) show(`RTC 错误：${e?.message || e}`);
      });
      current.on(VERTC.events.onUserUnpublishStream, e => {
        const userId = e?.userId || e?.user?.userId;
        if (userId === config.publisher_id && active){
          streamLost('机器狗 RTC 视频已停止发布，正在重连…', 6000);
        }
      });
      current.on(VERTC.events.onTokenWillExpire, refreshToken);
      current.on(VERTC.events.onTokenSubscribePrivilegeWillExpire, refreshToken);
      await current.joinRoom(
        config.token,
        config.room_id,
        { userId: config.user_id },
        { isAutoPublish: false, isAutoSubscribeAudio: false, isAutoSubscribeVideo: true },
      );
      if (active && engine === current){
        show('等待机器狗 RTC 视频…');
        scheduleReconnect();
      }
    } catch (error){
      show(`RTC 连接失败：${error?.message || error}`);
      if (active && currentGeneration === generation) scheduleReconnect();
    }
  }

  async function start(){
    if (active) return;
    active = true;
    setVideoVisible(false);
    startWatchdog();
    await connect();
  }

  async function stop(){
    active = false;
    generation++;
    clearReconnect();
    firstFrame = false;
    lastFrameAt = 0;
    lastRemoteFrameCount = null;
    frameTracking = false;
    frameToken++;
    stopWatchdog();
    setVideoVisible(false);
    const current = engine;
    engine = null;
    if (current){
      try { await current.leaveRoom(); } catch {}
      try { VERTC.destroyEngine(current); } catch {}
    }
  }

  return { start, stop };
}
