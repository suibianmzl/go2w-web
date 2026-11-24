import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const code = await readFile(new URL('../static/js/recording-capture.js', import.meta.url), 'utf8').catch(() => '');
const M = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
assert.equal(typeof M.createRollbackCapture, 'function', '提供共享录包和语音控制器');

const timers = new Map(); let clock = 0, serial = 0, stopped = 0, saves = 0, uploads = [];
globalThis.setTimeout = (fn, ms) => { timers.set(++serial, {fn, at:clock+ms}); return serial; };
globalThis.clearTimeout = id => timers.delete(id);
globalThis.setInterval = (fn, ms) => { timers.set(++serial, {fn, at:clock+ms, every:ms}); return serial; };
globalThis.clearInterval = globalThis.clearTimeout;
globalThis.performance = { now: () => clock };
globalThis.localStorage = { getItem: () => null, setItem(){} };
globalThis.isSecureContext = true;
globalThis.document = { hidden:false, addEventListener(){}, removeEventListener(){} };
globalThis.window = { addEventListener(){}, removeEventListener(){} };
const flush = async () => { for (let i=0;i<15;i++) await Promise.resolve(); };
async function advance(ms){ clock += ms; for (const [id,t] of [...timers]) if (t.at <= clock){ if(t.every) t.at=clock+t.every; else timers.delete(id); t.fn(); } await flush(); }
const stream = { getTracks: () => [{ stop: () => stopped++ }] };
Object.defineProperty(globalThis, 'navigator', { configurable:true, value:{ mediaDevices:{ getUserMedia: async () => stream } } });
globalThis.MediaRecorder = class {
  static isTypeSupported(type){ return type.startsWith('audio/webm'); }
  constructor(){this.state='inactive';this.mimeType='audio/webm';}
  start(){this.state='recording';}
  stop(){this.state='inactive';this.ondataavailable({data:new Blob(['voice'],{type:this.mimeType})});this.onstop();}
};
let resolveBag, requestedVoiceSeconds;
const store = { link:{ok:true}, rollback:{run:true,saving:false} };
const src = { name:'ros', cmd:{ saveRollback:(_requestId,voiceSeconds) => { saves++;requestedVoiceSeconds=voiceSeconds;return new Promise(resolve=>resolveBag=resolve); } } };
globalThis.fetch = async (url, options) => { uploads.push({url,options});return {ok:true,json:async()=>({})}; };
const capture = M.createRollbackCapture(store,src);
assert.equal(capture.state.seconds,5);
capture.setSeconds(100); assert.equal(capture.state.seconds,20);
capture.setSeconds(0); assert.equal(capture.state.seconds,1);
capture.setSeconds('bad'); assert.equal(capture.state.seconds,5);
const run = capture.save();
await flush();
assert.equal(saves,1);
assert.equal(requestedVoiceSeconds,5,'请求先声明本次语音，提前点击一键上传时后端才能等待');
assert.equal(capture.state.recording,true);
await capture.save(); assert.equal(saves,1,'忙碌时的第二次按下不生成第二个包');
await advance(5000);
assert.equal(stopped,1,'录到设定时长即释放麦克风');
assert.equal(uploads.length,0,'后10秒包未完成时不猜包名');
resolveBag({ok:true,data:{name:'bag-one'}});
await run;
assert.equal(uploads.length,1);
assert.match(uploads[0].url,/bag-one\/audio\?duration=5/);
assert.equal(capture.state.busy,false);

globalThis.isSecureContext=false;
const unavailable = capture.save(); await flush(); resolveBag({ok:true,data:{name:'bag-two'}}); await unavailable;
assert.match(capture.state.message,/HTTPS/);
assert.equal(uploads.filter(u=>u.url.includes('/audio?')).length,1,'普通HTTP不能伪报录音已保存');
const missing=uploads.find(u=>u.url.includes('/audio-status'));
assert.equal(JSON.parse(missing.options.body).state,'unavailable','明确上报无录音，整包上传不会永久等待');

globalThis.isSecureContext=true;
globalThis.fetch=async()=>{throw Error('offline');};
capture.setSeconds(1);
const failed=capture.save();await flush();resolveBag({ok:true,data:{name:'bag-three'}});await advance(1000);await failed;
assert.equal(capture.state.pending.name,'bag-three','上传失败保留正确包名与Blob');
assert.ok(capture.state.pending.blob.size);
globalThis.fetch=async()=>({ok:true,json:async()=>({})});
await capture.retry(); assert.equal(capture.state.pending,null);

const recoveredUrls=[];
globalThis.fetch=async(url,options={})=>{
  recoveredUrls.push(url);
  return {ok:true,json:async()=>({items:[{name:'recovered-bag',dur:30}]})};
};
const recovered=capture.save();await flush();
resolveBag({ok:false,uncertain:true,msg:'connection lost'});await flush();
assert.equal(capture.state.recording,true,'回执未知时继续录语音，不丢弃');
await advance(1000);await recovered;
assert.ok(recoveredUrls.some(url=>url.includes('?request_id=')),'按稳定请求ID查回同一次保存');
assert.ok(recoveredUrls.some(url=>url.includes('recovered-bag/audio')),'恢复后绑定实际完成的包');
assert.equal(capture.state.pending,null);

const partial=capture.save();await flush();resolveBag({ok:true,data:{name:'short-bag',duration_s:26,requested_s:30}});await advance(1000);await partial;
assert.match(capture.state.message,/26\.0 秒.*30 秒/,'缓存不足时显示实际长度');

navigator.mediaDevices.getUserMedia=async()=>{const e=Error('denied');e.name='NotAllowedError';throw e;};
const denied=capture.save();await flush();resolveBag({ok:true,data:{name:'denied-mic'}});await denied;
assert.match(capture.state.message,/允许麦克风权限/);
assert.equal(capture.state.pending,null);

navigator.mediaDevices.getUserMedia=async()=>stream;
const unsaved=capture.save();await flush();await advance(1000);resolveBag({ok:false,msg:'保存失败'});await unsaved;
assert.ok(capture.state.pending?.blob);
globalThis.fetch=async()=>({ok:true,json:async()=>({items:[]})});
await capture.discard();assert.equal(capture.state.pending,null,'找不到包时也能明确结束本地处理并继续录制');

const event = (overrides={}) => ({key:'Enter',shiftKey:true,repeat:false,isComposing:false,ctrlKey:false,altKey:false,metaKey:false,
 target:{closest:()=>null},preventDefault(){this.prevented=true;},...overrides});
let hotkeys=0;
const handler=M.rollbackShortcut(()=>hotkeys++);
handler(event());handler(event({repeat:true}));handler(event({isComposing:true}));handler(event({target:{closest:()=>({})}}));
assert.equal(hotkeys,1,'输入框、编辑区域、对话框、长按不触发快捷键');
console.log('录音默认/边界、重复按下、关联、HTTP降级、失败重试及快捷键通过');
