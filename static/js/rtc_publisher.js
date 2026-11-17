// Headless Chromium publisher: local Go2W H.264 -> video track -> Volcengine RTC.
const params = new URLSearchParams(location.search);
const wsUrl = params.get('ws') || 'ws://127.0.0.1:8080/ws';
const cloudBase = params.get('cloud') || 'https://fleet.evoxrobot.com/quadruped';
const robotId = params.get('robot') || 'go2w-01';
const video = document.querySelector('#source');
const canvas = document.querySelector('#frame');
const canvasContext = canvas.getContext('2d', { alpha: false });
const status = document.querySelector('#status');
let engine = null, muxer = null, ws = null, config = null, pingTimer = null;
window.__go2wRtcState = { phase: 'starting', events: [] };

function report(){
  const body = {
    ...window.__go2wRtcState,
    videoWidth: video.videoWidth,
    videoHeight: video.videoHeight,
    readyState: video.readyState,
  };
  fetch(`${cloudBase}/rtc/publisher-status`, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
    body: JSON.stringify(body),
  }).catch(() => {});
}

function log(message){
  status.textContent = `${new Date().toISOString()} ${message}`;
  window.__go2wRtcState.message = message;
  console.log(message);
  report();
}

async function loadConfig(){
  const response = await fetch(`${cloudBase}/rtc/config?role=robot&robot_id=${encodeURIComponent(robotId)}`,
    { cache: 'no-store' });
  if (!response.ok) throw new Error(`RTC config ${response.status}`);
  return response.json();
}

function waitForVideo(timeout = 15000){
  return new Promise((resolve, reject) => {
    const t0 = performance.now();
    const check = () => {
      if (video.videoWidth && video.readyState >= 2) return resolve();
      if (performance.now() - t0 > timeout) return reject(new Error('camera frame timeout'));
      setTimeout(check, 100);
    };
    check();
  });
}

async function startRtc(){
  if (!window.VERTC) throw new Error('Volcengine RTC Web SDK missing');
  config = await loadConfig();
  engine = VERTC.createEngine(config.app_id);
  const refreshToken = async () => {
    try {
      const next = await loadConfig();
      await engine.updateToken(next.token);
      log('RTC token updated');
    } catch (error){
      log(`RTC token update failed: ${error}`);
    }
  };
  const remember = (name, value) => {
    window.__go2wRtcState.phase = name;
    window.__go2wRtcState.events.push({ name, value, at: Date.now() });
    window.__go2wRtcState.events = window.__go2wRtcState.events.slice(-20);
    log(`${name}: ${JSON.stringify(value ?? null)}`);
  };
  engine.on(VERTC.events.onPublishResult, value => remember('publishResult', value));
  engine.on(VERTC.events.onError, value => remember('error', value));
  engine.on(VERTC.events.onConnectionStateChanged, value => remember('connection', value));
  engine.on(VERTC.events.onLocalStreamStats, value => remember('localStats', value));
  engine.on(VERTC.events.onTokenWillExpire, refreshToken);
  engine.on(VERTC.events.onTokenPublishPrivilegeWillExpire, refreshToken);
  engine.setVideoSourceType(
    VERTC.StreamIndex.STREAM_INDEX_MAIN,
    VERTC.VideoSourceType.VIDEO_SOURCE_TYPE_EXTERNAL,
  );
  await engine.joinRoom(
    config.token,
    config.room_id,
    { userId: config.user_id },
    { isAutoPublish: false, isAutoSubscribeAudio: false, isAutoSubscribeVideo: false },
  );
  await engine.setUserVisibility(true);
  await waitForVideo();
  await video.play();
  const stream = canvas.captureStream ? canvas.captureStream(15) : canvas.mozCaptureStream(15);
  const track = stream.getVideoTracks()[0];
  if (!track) throw new Error('canvas video track unavailable');
  track.enabled = true;
  track.contentHint = 'detail';
  window.__go2wRtcState.track = {
    readyState: track.readyState,
    enabled: track.enabled,
    muted: track.muted,
    settings: typeof track.getSettings === 'function' ? track.getSettings() : null,
  };
  await engine.setVideoEncoderConfig({
    width: 1280,
    height: 720,
    frameRate: 15,
    maxKbps: 1800,
  });
  engine.setExternalVideoTrack(VERTC.StreamIndex.STREAM_INDEX_MAIN, track);
  await engine.publishStream(VERTC.MediaType.VIDEO);
  remember('publishing', { publisher: config.publisher_id, room: config.room_id });
}

function connectCamera(){
  ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    log(`camera connected ${wsUrl}`);
    ws.send(JSON.stringify({ t: 'sub', layers: [] }));
    ws.send(JSON.stringify({ t: 'cam', on: true }));
    clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: 'ping', ts: performance.now() }));
    }, 500);
  };
  ws.onmessage = event => {
    if (typeof event.data === 'string') return;
    const bytes = new Uint8Array(event.data);
    if (bytes.length < 5 || bytes[0] !== 7) return;
    const view = new DataView(event.data);
    const metaLength = view.getUint32(1, true);
    const meta = JSON.parse(new TextDecoder().decode(bytes.subarray(5, 5 + metaLength)));
    const payload = bytes.subarray(5 + metaLength);
    muxer.feed({ video: payload, duration: meta.dur || undefined });
  };
  ws.onclose = () => {
    clearInterval(pingTimer);
    log('camera disconnected; retrying');
    setTimeout(connectCamera, 2000);
  };
}

muxer = new JMuxer({
  node: video,
  mode: 'video',
  flushingTime: 0,
  fps: 15,
  clearBuffer: true,
  debug: false,
  onError: error => log(`camera muxer error: ${error}`),
});
function drawFrame(){
  if (video.videoWidth && video.readyState >= 2){
    canvasContext.drawImage(video, 0, 0, 1280, 720);
  }
  requestAnimationFrame(drawFrame);
}
requestAnimationFrame(drawFrame);
connectCamera();
setInterval(report, 2000);
startRtc().catch(error => {
  window.__go2wRtcState.phase = 'failed';
  window.__go2wRtcState.error = error?.stack || String(error);
  log(`RTC publisher failed: ${error?.stack || error}`);
  setTimeout(() => location.reload(), 5000);
});
