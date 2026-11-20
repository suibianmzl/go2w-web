// 建图时放大地图要保持住（以前每收到一次 /map 就无条件适配全图，几秒就缩回去）：
//   GO2W_FAKE_MAP_GROW=1 dev/run.sh（PORT=8091）+ headless Chrome --remote-debugging-port=9333
//   node dev/test_map_zoom.mjs   会切到建图、再切到导航（只能对本地假 ROS 运行）
const sleep = ms => new Promise(r => setTimeout(r, ms));
const bws = new WebSocket('ws://127.0.0.1:8091/ws'); let st = null, sq = 0; const acks = new Map();
bws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data); if (m.t === 'state') st = m; if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
setInterval(() => bws.readyState === 1 && bws.send('{"t":"ping","ts":0}'), 300);
const cmd = (op, a = {}) => new Promise(r => { const id = ++sq; acks.set(id, r); bws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
await new Promise(r => bws.onopen = r); while (!st) await sleep(100);
if (st.profiles.mapping !== 'run') { console.log('set_mode', (await cmd('set_mode', { mode: 'mapping' })).msg); while (!(st.profiles.mapping === 'run' && !st.busy)) await sleep(200); }
const tgt = await (await fetch('http://127.0.0.1:9333/json/new?http://127.0.0.1:8091/?debug', { method: 'PUT' })).json();
const cdp = new WebSocket(tgt.webSocketDebuggerUrl); let cid = 0; const pend = new Map();
cdp.onmessage = e => { const m = JSON.parse(e.data); if (m.id) { pend.get(m.id)?.(m); pend.delete(m.id); } };
await new Promise(r => cdp.onopen = r);
const send = (method, params = {}) => new Promise(r => { const id = ++cid; pend.set(id, r); cdp.send(JSON.stringify({ id, method, params })); });
const ev = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result?.result?.value;
await sleep(6000);
await ev(`window.__maps=0; window.go2w && (()=>{let last=go2w.store.map; setInterval(()=>{ if(go2w.store.map!==last){last=go2w.store.map; window.__maps++;} },20)})()`);
const s0 = await ev('go2w.mapView.view.s'); const r = await ev(`JSON.stringify(document.querySelector('#map').getBoundingClientRect())`); const R = JSON.parse(r);
for (let i = 0; i < 5; i++) await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: R.x + R.width / 2, y: R.y + R.height / 2, deltaX: 0, deltaY: -300 });
await sleep(300);
const s1 = await ev('go2w.mapView.view.s'); const ser = [];
for (let i = 0; i < 10; i++) { await sleep(1000); ser.push(+(await ev('go2w.mapView.view.s')).toFixed(1)); }
const maps = await ev('window.__maps');
const res = []; const ok = (n, c, x = '') => res.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  · ' + x : ''}`);
ok('滚轮放大生效', s1 > s0 * 1.5, `${s0.toFixed(1)} → ${s1.toFixed(1)}`);
ok('10 s 内收到多次新地图', maps >= 3, `${maps} 次`);
ok('放大后 10 s 保持', ser.every(v => Math.abs(v - s1) < 0.2), ser.join(' '));
await ev(`document.querySelector('#vFit').click()`); await sleep(300);
const s2 = await ev('go2w.mapView.view.s'); ok('点「适配」回到全图', Math.abs(s2 - s0) < s0 * 0.05, `${s2.toFixed(1)}`);
await sleep(4500); const s3 = await ev('go2w.mapView.view.s'); ok('适配后新地图照样自动适配（不报错、不乱跳）', Math.abs(s3 - s0) < s0 * 0.2, `${s3.toFixed(1)}`);
// 再放大，然后换图（停建图 → 导航）：视角应复位
for (let i = 0; i < 5; i++) await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: R.x + R.width / 2, y: R.y + R.height / 2, deltaX: 0, deltaY: -300 });
await sleep(300); const s4 = await ev('go2w.mapView.view.s');
await cmd('set_mode', { mode: 'navigation', map: 'lab_20261021' }); 
for (let i = 0; i < 60 && !(st.profiles.navigation === 'run' && st.mapCurrent); i++) await sleep(500);
await sleep(3000); const s5 = await ev('go2w.mapView.view.s');
ok('换到导航（另一张图）后视角复位', s5 < s4 * 0.7, `${s4.toFixed(1)} → ${s5.toFixed(1)} · mapCurrent=${st.mapCurrent}`);
console.log(res.join('\n')); process.exit(0);
