#!/usr/bin/env node
import assert from 'node:assert/strict';

let clock = 1000;
let nextTimer = 1;
const timeouts = new Map();
const intervals = new Map();

Object.defineProperty(globalThis, 'performance', {
  configurable: true,
  value: { now: () => clock },
});
globalThis.setTimeout = callback => {
  const id = nextTimer++;
  timeouts.set(id, callback);
  return id;
};
globalThis.clearTimeout = id => timeouts.delete(id);
globalThis.setInterval = callback => {
  const id = nextTimer++;
  intervals.set(id, callback);
  return id;
};
globalThis.clearInterval = id => intervals.delete(id);
globalThis.document = { hidden: false };
globalThis.location = { search: '' };
globalThis.fetch = async () => ({
  ok: true,
  json: async () => ({
    app_id: 'test-app',
    token: 'test-token',
    room_id: 'go2w-01',
    user_id: 'viewer-1',
    publisher_id: 'go2w-01-camera',
  }),
});

const handlers = new Map();
const video = {
  frameCallback: null,
  requestVideoFrameCallback(callback){
    this.frameCallback = callback;
    return 1;
  },
};
const container = {
  style: { visibility: '', opacity: '' },
  querySelector(selector){
    return selector === 'video' ? video : null;
  },
};
const engine = {
  on(name, callback){ handlers.set(name, callback); },
  async joinRoom(){},
  async subscribeStream(){},
  setRemoteVideoPlayer(){ return Promise.resolve(); },
  async leaveRoom(){},
  async updateToken(){},
};
const VERTC = {
  MediaType: { VIDEO: 1 },
  StreamIndex: { STREAM_INDEX_MAIN: 0 },
  events: {
    onUserPublishStream: 'publish',
    onVideoFirstFrameDecoded: 'first-frame',
    onRemoteStreamStats: 'stats',
    onError: 'error',
    onUserUnpublishStream: 'unpublish',
    onTokenWillExpire: 'token',
    onTokenSubscribePrivilegeWillExpire: 'subscribe-token',
  },
  createEngine(){ return engine; },
  destroyEngine(){},
};
globalThis.window = { VERTC };
globalThis.VERTC = VERTC;

const messages = [];
const { createRtcViewer } = await import(
  `../static/js/rtc_viewer.js?test=${Date.now()}`
);
const viewer = createRtcViewer(
  container,
  message => messages.push(message),
  '/quadruped',
);

await viewer.start();
assert.equal(container.style.visibility, 'hidden');
await handlers.get('publish')({ userId: 'go2w-01-camera' });
await new Promise(resolve => queueMicrotask(resolve));
handlers.get('first-frame')();
assert.equal(container.style.visibility, '');
assert.equal(messages.at(-1), '');

clock += 7000;
for (const callback of intervals.values()) callback();
assert.equal(container.style.visibility, 'hidden');
assert.match(messages.at(-1), /隐藏最后画面/);

video.frameCallback();
assert.equal(container.style.visibility, '');

await viewer.stop();
assert.equal(container.style.visibility, 'hidden');
console.log('PASS RTC viewer: hide stale frame after stall and restore on new frame');
