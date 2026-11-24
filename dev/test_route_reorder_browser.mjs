// 任务站点调整顺序（拖拽 / 改序号 / 插入）浏览器验收：纯前端模拟，不连后端
//   python3 -m http.server 8765 -d static &   然后   node dev/test_route_reorder_browser.mjs [截图目录]
import { spawn } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import assert from 'node:assert/strict';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const OUT = process.argv[2] || mkdtempSync(tmpdir() + '/reorder-shots-'), BASE = 'http://127.0.0.1:8765/?mock&debug';
const prof = mkdtempSync(tmpdir() + '/cdpr-');
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ['--headless=new', '--remote-debugging-port=9334', `--user-data-dir=${prof}`, '--window-size=1500,950', '--no-first-run', 'about:blank'], { stdio: 'ignore' });
let ws, seq = 0; const waits = new Map(), errors = [];
try {
  let tgt;
  for (let i = 0; i < 60 && !tgt; i++){ try { tgt = (await (await fetch('http://127.0.0.1:9334/json')).json()).find(t => t.type === 'page'); } catch {} await sleep(200); }
  ws = new WebSocket(tgt.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r, { once: true }));
  ws.addEventListener('message', ({ data }) => { const m = JSON.parse(data);
    if (m.id && waits.has(m.id)){ waits.get(m.id)(m); waits.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map(a => a.value ?? a.description).join(' ')); });
  const send = (method, params = {}) => new Promise(r => { const id = ++seq; waits.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
  const ev = async expr => { const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true }); if (r.result.exceptionDetails) throw new Error(expr + ' → ' + JSON.stringify(r.result.exceptionDetails.exception?.description)); return r.result.result.value; };
  const until = async (expr, what, ms = 10000) => { const t = Date.now(); while (!(await ev(expr))){ assert.ok(Date.now() - t < ms, '超时：' + what); await sleep(100); } };
  const shot = async name => { const r = await send('Page.captureScreenshot', { format: 'png' }); writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.result.data, 'base64')); };
  const mouse = async (type, x, y, extra = {}) => send('Input.dispatchMouseEvent', { type, x, y, button: 'left', buttons: type === 'mouseReleased' ? 0 : 1, clickCount: 1, ...extra });
  const PASS = s => console.log('PASS', s);
  // 站名顺序（用站名当标记）
  const names = () => ev('go2w.store.routeDraft.points.map(p => p.name).join(",")');
  // 地图上找可走的格子（mock 地图），换成屏幕坐标
  const freeScreen = async n => ev(`(()=>{
    const cv=document.getElementById('map'), r=cv.getBoundingClientRect(), out=[];
    for (let gy=0.35; gy<0.7 && out.length<${n}; gy+=0.05) for (let gx=0.35; gx<0.7 && out.length<${n}; gx+=0.06){
      const px=r.left+r.width*gx, py=r.top+r.height*gy;
      if (document.elementFromPoint(px,py)?.id!=='map') continue;
      out.push([px,py]);
    } return out; })()`);
  const tap = async (x, y) => { await mouse('mouseMoved', x, y, { buttons: 0 }); await mouse('mousePressed', x, y); await mouse('mouseReleased', x, y); await sleep(80); };

  await send('Runtime.enable'); await send('Page.enable');
  await send('Page.navigate', { url: BASE });
  await until('!!(window.go2w && go2w.store.map && go2w.store.mapCurrent)', '页面加载、地图');
  await ev(`document.getElementById('routeNew').click()`);
  await sleep(200);
  // 先用代码放 4 站（避开 mock 地图上障碍的不确定），站名 A B C D
  const spots = await freeScreen(12);
  let added = 0;
  for (const [x, y] of spots){
    if (added >= 4) break;
    const before = await ev('go2w.store.routeDraft.points.length');
    await tap(x, y);
    const after = await ev('go2w.store.routeDraft.points.length');
    if (after > before) added++;
  }
  assert.equal(added, 4, '加了 4 站');
  await ev(`(()=>{ const d=go2w.store.routeDraft; go2w.store.routeDraft={...d, points:d.points.map((p,i)=>({...p,name:'ABCD'[i]}))}; document.getElementById('routeName').dispatchEvent(new Event('input')); })()`);
  await sleep(100);
  assert.equal(await names(), 'A,B,C,D'); PASS('加 4 站');

  // 1) 改序号：把第 4 站（D）改成 1
  await ev(`(()=>{ const o=document.querySelectorAll('#routePoints .route-order')[3]; o.focus(); o.value='1'; o.dispatchEvent(new Event('change')); })()`);
  await sleep(100);
  assert.equal(await names(), 'D,A,B,C'); PASS('改序号 4→1');
  // 超范围 → 放到最后；非法 → 不动
  await ev(`(()=>{ const o=document.querySelectorAll('#routePoints .route-order')[0]; o.value='99'; o.dispatchEvent(new Event('change')); })()`);
  await sleep(100);
  assert.equal(await names(), 'A,B,C,D'); PASS('改序号 99 → 末尾');
  await ev(`(()=>{ const o=document.querySelectorAll('#routePoints .route-order')[1]; o.value='x'; o.dispatchEvent(new Event('change')); })()`);
  await sleep(100);
  assert.equal(await names(), 'A,B,C,D');
  assert.equal(await ev(`document.querySelectorAll('#routePoints .route-order')[1].value`), '2'); PASS('非法序号还原');

  // 2) 拖拽：把 A 的把手拖到 C 下面
  const box = async i => ev(`(()=>{ const r=document.querySelectorAll('#routePoints li')[${i}].getBoundingClientRect(); const g=document.querySelectorAll('#routePoints .route-grip')[${i}].getBoundingClientRect(); return {gx:g.left+g.width/2, gy:g.top+g.height/2, top:r.top, bottom:r.bottom}; })()`);
  await ev(`(()=>{ const l=document.getElementById('routePoints'); l.style.maxHeight='none'; l.scrollTop=0; })()`);
  const g0 = await box(0), c2 = await box(2);
  await mouse('mouseMoved', g0.gx, g0.gy, { buttons: 0 });
  await mouse('mousePressed', g0.gx, g0.gy);
  for (let k = 1; k <= 6; k++){ await mouse('mouseMoved', g0.gx, g0.gy + (c2.bottom - 4 - g0.gy) * k / 6); await sleep(30); }
  assert.ok(await ev(`document.querySelectorAll('#routePoints li')[2].classList.contains('drop-after')`), '拖拽时显示落点线');
  await shot('drag');
  await mouse('mouseReleased', g0.gx, c2.bottom - 4);
  await sleep(150);
  assert.equal(await names(), 'B,C,A,D'); PASS('拖拽 1 → 3');
  // 往上拖：D 拖到最上
  const g3 = await box(3), t0 = await box(0);
  await mouse('mouseMoved', g3.gx, g3.gy, { buttons: 0 });
  await mouse('mousePressed', g3.gx, g3.gy);
  for (let k = 1; k <= 6; k++){ await mouse('mouseMoved', g3.gx, g3.gy + (t0.top + 3 - g3.gy) * k / 6); await sleep(30); }
  await mouse('mouseReleased', g3.gx, t0.top + 3);
  await sleep(150);
  assert.equal(await names(), 'D,B,C,A'); PASS('拖拽 4 → 1');
  // 原地松手不动
  const g1 = await box(1);
  await mouse('mouseMoved', g1.gx, g1.gy, { buttons: 0 }); await mouse('mousePressed', g1.gx, g1.gy); await mouse('mouseMoved', g1.gx, g1.gy + 2); await mouse('mouseReleased', g1.gx, g1.gy + 2);
  await sleep(100);
  assert.equal(await names(), 'D,B,C,A'); PASS('原地松手不变');

  // 3) 插入：在第 1 站（D）后面连插两站
  await ev(`document.querySelectorAll('#routePoints li')[0].querySelector('button').click()`);
  await sleep(100);
  assert.equal(await ev('go2w.store.routeInsert'), 1);
  assert.equal(await ev('go2w.mapView.tool'), 'route');
  assert.match(await ev(`document.getElementById('aimHint').textContent`), /插入|添加/);
  let inserted = 0;
  for (const [x, y] of spots.slice().reverse()){
    if (inserted >= 2) break;
    const before = await ev('go2w.store.routeDraft.points.length');
    await tap(x, y);
    if (await ev('go2w.store.routeDraft.points.length') > before) inserted++;
  }
  assert.equal(inserted, 2);
  await shot('insert');
  assert.equal(await names(), 'D,,,B,C,A'); PASS('在第 1 站后连插 2 站');
  assert.equal(await ev('go2w.store.routeInsert'), 3);
  assert.ok(await ev(`document.querySelectorAll('#routePoints li')[2].classList.contains('route-insert-after')`));
  // Esc 结束插入，之后地图点击回到「加到末尾」
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape' });
  await sleep(100);
  assert.equal(await ev('go2w.store.routeInsert'), null);
  assert.equal(await ev('go2w.mapView.tool'), 'pan'); PASS('Esc 结束插入');
  // 最后一站的「插入」按钮禁用
  assert.ok(await ev(`[...document.querySelectorAll('#routePoints li')].at(-1).querySelector('button').disabled`)); PASS('最后一站不显示插入');
  // 名称 / 备注跟着站走
  await ev(`(()=>{ const o=document.querySelectorAll('#routePoints .route-point-name')[0]; o.value='门口'; o.dispatchEvent(new Event('input')); })()`);
  await ev(`(()=>{ const o=document.querySelectorAll('#routePoints .route-order')[0]; o.value='6'; o.dispatchEvent(new Event('change')); })()`);
  await sleep(100);
  assert.equal(await names(), ',,B,C,A,门口'); PASS('站名跟着站走');
  assert.equal(await ev('go2w.store.routeDraft.confirmed'), false); PASS('改顺序后要重新确认');
  await shot('final');
  assert.deepEqual(errors, [], '页面无报错');
  console.log('ALL PASS · 截图', OUT);
} finally {
  ws?.close(); chrome.kill();
}
