// 火山 RTC 视频订阅：只订阅机器狗发布的视频，不采集浏览器摄像头。
export function createRtcViewer(container, show, basePath = '/quadruped'){
  let engine = null, active = false, config = null;
  let reconnectTimer = null, firstFrame = false, generation = 0;

  async function loadConfig(){
    const response = await fetch(`${basePath}/rtc/config?role=viewer`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`RTC 配置失败（${response.status}）`);
    return response.json();
  }

  async function playPublished(e){
    if (!active || !engine) return;
    const userId = e?.userId || e?.user?.userId;
    if (!userId || userId !== config.publisher_id) return;
    try {
      firstFrame = false;
      show('正在等待 RTC 首帧…');
      await engine.subscribeStream(userId, VERTC.MediaType.VIDEO);
      Promise.resolve(engine.setRemoteVideoPlayer(VERTC.StreamIndex.STREAM_INDEX_MAIN, {
        userId,
        renderDom: container,
      })).catch(error => show(`RTC 播放失败：${error?.message || error}`));
    } catch (error){
      show(`RTC 播放失败：${error?.message || error}`);
    }
  }

  function clearReconnect(){
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function scheduleReconnect(){
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
    }, 12000);
  }

  async function connect(){
    if (!active) return;
    const currentGeneration = ++generation;
    firstFrame = false;
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
          firstFrame = true;
          clearReconnect();
          show('');
        }
      });
      current.on(VERTC.events.onError, e => {
        if (active) show(`RTC 错误：${e?.message || e}`);
      });
      current.on(VERTC.events.onUserUnpublishStream, e => {
        const userId = e?.userId || e?.user?.userId;
        if (userId === config.publisher_id && active){
          firstFrame = false;
          show('机器狗 RTC 视频已停止发布');
          scheduleReconnect();
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
    await connect();
  }

  async function stop(){
    active = false;
    generation++;
    clearReconnect();
    firstFrame = false;
    const current = engine;
    engine = null;
    if (current){
      try { await current.leaveRoom(); } catch {}
      try { VERTC.destroyEngine(current); } catch {}
    }
  }

  return { start, stop };
}
