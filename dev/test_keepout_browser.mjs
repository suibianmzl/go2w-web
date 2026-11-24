// 禁行区（v1.1.8）浏览器验收：先运行 dev/run.sh（导航档位开着最好），再运行
//   node dev/test_keepout_browser.mjs [截图目录]     需要本机装了 Google Chrome；只连本地假后端
import { spawn } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import assert from 'node:assert/strict';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const OUT = process.argv[2] || mkdtempSync(tmpdir() + "/keepout-shots-"), BASE = 'http://127.0.0.1:8080/?debug';
const prof = mkdtempSync(tmpdir() + '/cdpk-');
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ['--headless=new', '--remote-debugging-port=9333', `--user-data-dir=${prof}`, '--window-size=1500,950', '--no-first-run', 'about:blank'], { stdio: 'ignore' });
let ws, seq = 0; const waits = new Map(), errors = [];
try {
  let tgt;
  for (let i = 0; i < 60 && !tgt; i++){ try { tgt = (await (await fetch('http://127.0.0.1:9333/json')).json()).find(t => t.type === 'page'); } catch {} await sleep(200); }
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
  const click = sel => ev(`(()=>{const e=document.querySelector(${JSON.stringify(sel)}); if(!e) throw new Error('没有 ${sel}'); e.click(); return true})()`);
  const mouse = async (type, x, y, extra = {}) => send('Input.dispatchMouseEvent', { type, x, y, button: 'left', buttons: type === 'mouseReleased' ? 0 : 1, clickCount: 1, ...extra });
  const onCanvas = async (x, y) => assert.equal(await ev(`document.elementFromPoint(${x},${y})?.id`), 'map', `(${x|0},${y|0}) 被别的元素挡住`);
  const tap = async (x, y) => { await onCanvas(x, y); await mouse('mouseMoved', x, y, { buttons: 0 }); await mouse('mousePressed', x, y); await mouse('mouseReleased', x, y); await sleep(60); };
  const key = async (k, mods = 0) => { const code = k.length === 1 ? 'Key' + k.toUpperCase() : k;
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code, modifiers: mods, windowsVirtualKeyCode: k === 'Enter' ? 13 : k.toUpperCase().charCodeAt(0) });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code, modifiers: mods }); await sleep(80); };
  const PASS = s => console.log('PASS', s);

  await send('Runtime.enable'); await send('Page.enable');
  await send('Page.navigate', { url: BASE });
  await until('!!(window.go2w && go2w.store.map && go2w.store.mapCurrent && go2w.store.keepoutState)', '页面加载、地图和禁行状态');
  const map = await ev('go2w.store.mapCurrent');
  await ev(`go2w.src.cmd.saveKeepout(${JSON.stringify(map)}, [])`);
  await until(`!go2w.store.keepouts[${JSON.stringify(map)}]`, '开始前清空');
  const R = await ev(`(()=>{const r=document.querySelector('#map').getBoundingClientRect(); return {x:r.left,y:r.top,w:r.width,h:r.height}})()`);
  await ev(`(()=>{const c=document.querySelector('#camHead'); if(c) c.parentElement.hidden=true; return 1})()`);   // 摄像头小窗会挡住点击
  await click('[data-tool=edit]');
  await until(`!document.querySelector('#medit').hidden`, '编辑面板打开');
  const ML = await ev(`document.querySelector('#medit').getBoundingClientRect().left`);
  const cx = R.x + (ML - R.x) * 0.5, cy = R.y + R.h * 0.55;
  console.log('     画布可点区域宽', Math.round(ML - R.x), 'px，中心', Math.round(cx), Math.round(cy));
  await click('#meditTabs [data-tab=keep]');
  await until(`!document.querySelector('#meditKeepPane').hidden && document.querySelector('#meditMapPane').hidden`, '切到禁行区页');
  assert.match(await ev(`document.querySelector('#keepList').textContent`), /还没有禁行区/);
  assert.match(await ev(`document.querySelector('#aimHint').textContent`), /逐个点击放折线/, '地图上方提示跟着换');
  PASS('打开编辑 → 禁行区页，空列表提示、地图提示正确');

  await click('#keepTools [data-kt=line]');
  await tap(cx - 120, cy - 60); await tap(cx, cy - 70); await tap(cx + 120, cy - 40);
  assert.equal(await ev(`document.querySelector('#keepFinish').hidden`), false, '画线时出现「完成这一条」');
  await shot('1_drawing_line');
  await key('Enter');
  await until(`document.querySelectorAll('#keepList .keep-item').length===1`, 'Enter 完成一条线');
  PASS('点 3 个点 + Enter → 1 条禁行线');
  await key('z', 4); await until(`document.querySelectorAll('#keepList .keep-item').length===0`, '撤销到空');
  await sleep(1300);   // 等编辑器的 0.5 s 巡检跑过几次：以前没有禁行区的图会在这里清掉重做记录
  await key('z', 4 | 8); await until(`document.querySelectorAll('#keepList .keep-item').length===1`, '等过之后还能重做');
  PASS('撤销到空、等 1.3 s 后仍能重做');

  await click('#keepTools [data-kt=zone]');
  const z = [[cx + 40, cy + 40], [cx + 160, cy + 40], [cx + 160, cy + 140], [cx + 40, cy + 140]];
  for (const [x, y] of z) await tap(x, y);
  await tap(z[0][0] + 2, z[0][1] + 1);   // 点回第一个点闭合
  await until(`document.querySelectorAll('#keepList .keep-item').length===2`, '点回起点闭合区域');
  assert.match(await ev(`document.querySelectorAll('#keepList .keep-item')[1].textContent`), /m²/);
  PASS('点 4 个角 + 点回起点 → 1 个禁行区域（显示面积）');

  await click('#keepTools [data-kt=line]');
  await tap(cx - 200, cy + 100); await key('Escape');
  assert.equal(await ev(`document.querySelectorAll('#keepList .keep-item').length`), 2, 'Esc 放弃');
  await tap(cx - 200, cy + 100); await tap(cx - 150, cy + 120); await key('Backspace'); await key('Backspace');
  assert.equal(await ev(`document.querySelector('#keepFinish').hidden`), true, 'Backspace 退完 = 放弃');
  PASS('Esc 放弃、Backspace 退点');

  await key('z', 4 /* meta */);
  await until(`document.querySelectorAll('#keepList .keep-item').length===1`, '⌘Z 撤销区域');
  await key('z', 4 | 8);
  await until(`document.querySelectorAll('#keepList .keep-item').length===2`, '⌘⇧Z 重做');
  PASS('撤销 / 重做');

  await ev(`(()=>{const i=document.querySelector('#keepList .keep-item input'); i.focus(); i.value='东侧 路牙'; i.dispatchEvent(new Event('input')); i.blur(); return 1})()`);
  assert.match(await ev(`document.querySelector('#meditTabs [data-tab=keep]').textContent`), /•/, '页签上有未保存标记');
  assert.equal(await ev(`document.querySelector('#keepSave').disabled`), false);
  await shot('2_before_save');
  await click('#keepSave');
  await until(`go2w.store.keepouts[${JSON.stringify(map)}]?.length===2`, '保存后 store 里有 2 个', 15000);
  assert.equal(await ev(`go2w.store.keepouts[${JSON.stringify(map)}][0].name`), '东侧 路牙');
  await until(`!document.querySelector('#meditTabs [data-tab=keep]').textContent.includes('•')`, '保存后未保存标记消失');
  const toast = await ev(`[...document.querySelectorAll('.toast,#toasts *')].map(e=>e.textContent).join('|')`);
  console.log('     提示：', toast.slice(0, 160));
  console.log('     状态行：', await ev(`document.querySelector('#keepLive').textContent`));
  PASS('保存禁行区：后端保存、名字保留、标记消失');

  // 删除工具
  await click('#keepTools [data-kt=del]');
  await tap(cx + 100, cy + 90);    // 区域内部
  await until(`document.querySelectorAll('#keepList .keep-item').length===1`, '删除工具点区域内部删掉');
  PASS('删除工具：点区域内部删除');

  // 修地图页：擦一笔 → 另存，带上未保存的禁行区
  await click('#meditTabs [data-tab=map]');
  await until(`!document.querySelector('#meditMapPane').hidden`, '回到修地图页');
  await click('#meditTools [data-et=unknown]');
  const sx0 = cx - 150, sy0 = cy - 150;
  await onCanvas(sx0, sy0); await onCanvas(sx0 + 80, sy0);
  await mouse('mouseMoved', sx0, sy0, { buttons: 0 }); await mouse('mousePressed', sx0, sy0);
  for (let k = 1; k <= 8; k++) await mouse('mouseMoved', sx0 + k * 10, sy0);
  await mouse('mouseReleased', sx0 + 80, sy0); await sleep(200);
  await until(`/已改 \\d+ 格/.test(document.querySelector('#meditDirty').textContent)`, '擦了一笔');
  await click('#meditSave');
  await until(`!!document.querySelector('.scrim [data-keepout]')`, '另存弹窗里有「带上未保存的禁行区」');
  assert.equal(await ev(`document.querySelector('.scrim [data-keepout]').checked`), true, '默认勾上');
  const nm = 'kotest_' + Date.now() % 100000;
  await ev(`(()=>{const f=document.querySelector('.scrim'); const i=f.querySelector('[data-name]'); i.value=${JSON.stringify(nm)}; i.dispatchEvent(new Event('input',{bubbles:true})); f.querySelector('[data-switch]').checked=false; return 1})()`);
  await shot('3_saveas_dialog');
  await click('.scrim [data-a=o]');
  await until(`go2w.store.keepouts[${JSON.stringify(nm)}]?.length===1`, '新图带上 1 个禁行区（未保存的版本）', 30000);
  assert.equal(await ev(`go2w.store.keepouts[${JSON.stringify(map)}].length`), 2, '原图的禁行区没改');
  PASS(`另存 ${nm}：带的是未保存的那份（1 个），原图仍是 2 个`);

  // 退出编辑后地图上照样画禁行区
  await until(`document.querySelector('#medit').hidden`, '另存后退出编辑');
  await sleep(500); await shot('4_view_after');
  PASS('退出编辑，平时视图截图');

  // 未保存时退出要确认
  await click('[data-tool=edit]'); await until(`!document.querySelector('#medit').hidden`, '再开编辑');
  await click('#meditTabs [data-tab=keep]'); await click('#keepTools [data-kt=line]');
  await tap(cx - 60, cy - 120); await tap(cx + 30, cy - 120); await key('Enter');
  await click('#meditClose');
  await until(`/禁行区修改/.test(document.querySelector('.scrim')?.textContent||'')`, '退出时提醒禁行区没保存');
  await click('.scrim [data-a=o]');
  await until(`document.querySelector('#medit').hidden`, '确认放弃后退出');
  assert.equal(await ev(`go2w.store.keepouts[${JSON.stringify(map)}].length`), 2, '放弃的没存进去');
  PASS('有未保存的禁行区时退出会确认，放弃后不保存');

  // 清理
  await ev(`go2w.src.cmd.saveKeepout(${JSON.stringify(map)}, [])`);
  await ev(`go2w.src.cmd.deleteMaps([${JSON.stringify(nm)}])`);
  assert.deepEqual(errors, [], '页面没有 JS 错误：' + errors.join('\n'));
  PASS('页面没有 JS 错误；已清理测试数据');
  console.log('\n全部通过');
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
}
