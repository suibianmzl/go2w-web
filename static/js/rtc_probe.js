const video = document.querySelector('#remote');
const output = document.querySelector('#result');
const state = { sdk: false, joined: false, publisher: null, firstFrame: false, error: null, events: [] };
const render = () => { output.textContent = JSON.stringify(state); };
let engine = null;

async function run(){
  try {
    state.sdk = !!window.VERTC;
    if (!state.sdk) throw new Error('Web RTC SDK missing');
    const response = await fetch('/quadruped/rtc/config?role=viewer', { cache: 'no-store' });
    const config = await response.json();
    engine = VERTC.createEngine(config.app_id);
    const remember = (name, value) => {
      state.events.push({ name, value });
      state.events = state.events.slice(-20);
      if (name === 'firstFrameDecoded') state.firstFrame = true;
      if (name === 'remoteVideoSize') state.firstFrame = true;
      render();
    };
    engine.on(VERTC.events.onVideoFirstFrameDecoded, value => remember('firstFrameDecoded', value));
    engine.on(VERTC.events.onRemoteVideoSizeChanged, value => remember('remoteVideoSize', value));
    engine.on(VERTC.events.onRemoteStreamStats, value => remember('remoteStats', value));
    engine.on(VERTC.events.onError, value => remember('error', value));
    engine.on(VERTC.events.onUserPublishStream, async event => {
      const userId = event?.userId || event?.user?.userId;
      if (userId !== config.publisher_id) return;
      state.publisher = userId;
      render();
      try {
        await engine.subscribeStream(userId, VERTC.MediaType.VIDEO);
        await engine.setRemoteVideoPlayer(VERTC.StreamIndex.STREAM_INDEX_MAIN, {
          userId,
          renderDom: video,
        });
      } catch (error){
        state.error = error?.message || String(error);
      }
      render();
    });
    await engine.joinRoom(
      config.token,
      config.room_id,
      { userId: config.user_id },
      { isAutoPublish: false, isAutoSubscribeAudio: false, isAutoSubscribeVideo: true },
    );
    state.joined = true;
  } catch (error){
    state.error = error?.message || String(error);
  }
  render();
}

run();
setInterval(render, 1000);
