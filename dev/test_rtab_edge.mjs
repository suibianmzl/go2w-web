// RTAB 影子库边界测试（11-20）。对本地假 ROS，服务端要这样起（存库 3 s 但只等 1 s、对位必失败）：
//   GO2W_DATA=$D GO2W_FAKE_RTAB_STOP_S=3 GO2W_RTAB_STOP_TIMEOUT=1 GO2W_FAKE_RTAB_LOCFAIL=1 dev/run.sh
//   GO2W_DATA=$D node dev/test_rtab_edge.mjs [ws://127.0.0.1:8080/ws]          → 1、2
//   （把后端 kill -9 掉再起来）GO2W_DATA=$D node dev/test_rtab_edge.mjs <url> restart  → 3
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const PHASE = process.argv[3] || 'main';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ws = new WebSocket(URL_); ws.binaryType = 'arraybuffer';
let state = null; const logs = []; const acks = new Map(); let seq = 0;
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
  if (m.t === 'state') state = m;
  if (m.t === 'logs') for (const it of m.items) logs.push(it[2]);
  if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
const timer = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 500);
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(100); } throw new Error('超时：' + what); };
const results = []; const ok = (name, cond, extra = '') => results.push(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  · ' + extra : ''}`);
const logHas = re => logs.some(t => re.test(t));
const R = () => state.rtab || {};
const idle = () => !state.busy && !state.mapBusy;
try {
  await new Promise(r => ws.onopen = r);
  await until(() => state, 8000, '首个状态');
  if (PHASE === 'restart'){
    ok('重启后：上次硬杀的会话记成没正常停止', R().last?.ok === false && /没有正常停止/.test(R().last.msg) && !R().cur, JSON.stringify(R().last));
  } else {
    // 1. 存库超时：照常切档位，rtabmap 退出后自动收尾
    await cmd('set_mode', { mode: 'mapping' });
    await until(() => R().cur?.phase === 'recording', 40000, '自动开录');
    const s = await cmd('save_map', { name: 'edge1' });
    ok('存图', s.ok, s.msg);
    await sleep(1500);
    await cmd('set_mode', { mode: null });
    await until(() => state.profiles.mapping === 'stop' && idle(), 30000, '建图停下');
    ok('存库超时不挡切档位', logHas(/RTAB 库没有正常停下.*照常切换档位/));
    await until(() => !R().cur, 10000, '超时后自动收尾');
    ok('rtabmap 退出后自动收尾，统计还在', R().last?.summary?.sessions.length === 1, JSON.stringify(R().last));
    // 2. 对位失败：自动停，库没改动
    await cmd('set_mode', { mode: 'localization', map: 'edge1' });
    await until(() => state.profiles.localization === 'run' && idle() && R().mapDb, 30000, '定位起来');
    await sleep(2500);
    const c = await cmd('rtab_continue', {});
    ok('开始局部重扫', c.ok, c.msg);
    await until(() => R().last?.ok === false, 15000, '对位失败');
    ok('对位失败：自动停，写明原因', !R().cur && /没和旧库对上/.test(R().last.msg) && logHas(/局部重扫没和旧库对上，已停止/), R().last.msg);
    ok('对位失败：库没多出一轮', R().last.summary?.sessions.length === 1, JSON.stringify(R().last.summary));
    // 3 的准备：开录后让外面把后端 kill -9
    await cmd('set_mode', { mode: 'mapping' });
    await until(() => R().cur?.phase === 'recording', 40000, '再次开录');
    ok('再次开录（接下来 kill -9 后端）', true, R().cur.db);
  }
} catch (e) { results.push('FAIL  ' + e.message); }
clearInterval(timer); ws.close();
for (const l of results) console.log(l);
const fails = results.filter(l => l.startsWith('FAIL')).length;
console.log(`\n${results.length - fails}/${results.length} 通过`);
process.exit(fails ? 1 : 0);
