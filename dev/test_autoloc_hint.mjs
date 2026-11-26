// 11-26 自动定位先在附近找 /「指定大概位置」：
//   D=$HOME/.cache/go2w_web_data_al; rm -rf $D; mkdir -p $D/maps && cp ~/.cache/go2w_web_data/maps/lab_20261021.* $D/maps/
//   echo '{"lab_20261021": {"pose": [-1.49, 8.4, 0], "t": 0}}' > $D/last_pose.json      # 故意记错的上次位置（离起点 9 m）
//   GO2W_DATA=$D GO2W_FAKE_START=2,-0.5,0 dev/run.sh
//   node dev/test_autoloc_hint.mjs [ws://127.0.0.1:8080/ws] [$D]
// 1. 启动后自动定位：上次位置记错 → 附近找不到 → 改搜整张图，照样成功
// 2. 定位正常几秒后 last_pose.json 记成现在的位置
// 3. 再点「自动定位」：在上次位置附近就找到
// 4. 指定大概位置：圈在狗身上 → 成功；圈在 9 m 外 → 不可信，提示把圈画大；格式不对 → 拒绝
// 5. hints（任务站点）只有一个对的：也能找到
import fs from 'node:fs';
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const DATA = process.argv[3] || `${process.env.HOME}/.cache/go2w_web_data_al`;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ws = new WebSocket(URL_); let state = null, robot = null, seq = 0; const acks = new Map(); const logs = [];
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
  if (m.t === 'state') state = m; if (m.t === 'pose' && m.valid) robot = m;
  if (m.t === 'logs') for (const it of m.items || []) logs.push(typeof it === 'string' ? it : JSON.stringify(it));
  if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
const ping = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 300);
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return; await sleep(100); } throw new Error('超时：' + what); };
const res = []; const ok = (n, c, x = '') => res.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  · ' + x : ''}`);
const prof = k => state.profiles[k];
const lastPose = () => { try { return JSON.parse(fs.readFileSync(`${DATA}/last_pose.json`, 'utf8')).lab_20261021?.pose; } catch { return null; } };
await new Promise(r => ws.onopen = r); await until(() => state, 6000, '首个状态');
try {
  await cmd('arm', { on: false });
  const seq0 = state.autoLoc?.seq ?? 0;
  if (prof('navigation') !== 'run') {
    const r = await cmd('set_mode', { mode: 'navigation' }); ok('启动导航', r.ok, r.msg);
    await until(() => prof('navigation') === 'run' && !state.busy, 25000, '导航起来');
  }
  // 假后端可能一开始导航就开着（没有启动这一下）：等不到启动后自动定位就自己点一次（同一套逻辑）
  await until(() => (state.autoLoc?.seq ?? 0) > seq0 && state.autoLoc.state !== 'run', 10000, '启动后自动定位')
    .catch(async () => { if (state.autoLoc?.state !== 'run') await cmd('auto_localize'); });
  await until(() => (state.autoLoc?.seq ?? 0) > seq0 && state.autoLoc.state !== 'run', 60000, '自动定位结果');
  const a = state.autoLoc;
  ok('1 上次位置记错：退回整图搜，成功', a.state === 'ok' && a.how === 'map', `${a.how} ${a.msg}`);
  ok('1 结果在起点 (2, -0.5)', Math.hypot(a.x - 2, a.y + 0.5) < 0.3, `(${a.x?.toFixed(2)}, ${a.y?.toFixed(2)})`);
  ok('1 日志说了附近没找到', logs.some(l => /上次位置附近没有可信结果/.test(l)), logs.filter(l => /自动定位/.test(l)).join(' | ').slice(0, 300));

  await until(() => { const p = lastPose(); return p && Math.hypot(p[0] - 2, p[1] + 0.5) < 0.5; }, 15000, 'last_pose 更新').catch(() => null);
  const lp = lastPose();
  ok('2 last_pose.json 记成现在的位置', lp && Math.hypot(lp[0] - 2, lp[1] + 0.5) < 0.5, JSON.stringify(lp));

  let t = Date.now(), r = await cmd('auto_localize');
  ok('3 再自动定位：上次位置附近找到', r.ok && r.data?.how === 'hint' && /在上次位置附近找到/.test(r.msg), `${Date.now() - t} ms · ${r.msg}`);

  t = Date.now(); r = await cmd('auto_localize', { area: { x: robot.x + 1.5, y: robot.y - 1, r: 5 } });
  ok('4 圈在狗身上：成功', r.ok && r.data?.how === 'area' && /半径 5 m/.test(r.msg), `${Date.now() - t} ms · ${r.msg}`);
  ok('4 结果和整图一致', r.ok && Math.hypot(r.data.x - a.x, r.data.y - a.y) < 0.1, `(${r.data?.x}, ${r.data?.y})`);
  const before = { x: robot.x, y: robot.y };
  r = await cmd('auto_localize', { area: { x: -1.49, y: 8.4, r: 3 } });
  ok('4 圈在 9 m 外：不可信', r.ok === false && /在指定范围里/.test(r.msg) && /把圈画大/.test(r.msg), r.msg);
  await sleep(500);
  ok('4 不可信时位姿没动', Math.hypot(robot.x - before.x, robot.y - before.y) < 0.05);
  r = await cmd('auto_localize', { area: { x: 'a', y: 1, r: 5 } });
  ok('4 格式不对拒绝', r.ok === false && /格式不对/.test(r.msg), r.msg);
  r = await cmd('auto_localize', { area: { x: 500, y: 500, r: 5 } });
  ok('4 圈在地图外：拒绝', r.ok === false && /地图外/.test(r.msg), r.msg);

  r = await cmd('auto_localize', { hints: [{ x: -1.49, y: 11.4, label: '第 9 站' }, { x: 2.3, y: -0.2, label: '第 1 站' }] });
  ok('5 hints：附近找到', r.ok && r.data?.how === 'hint', r.msg);
} catch (e) { ok('异常', false, e.message); }
clearInterval(ping); ws.close();
console.log(res.join('\n'));
console.log(`${res.filter(x => x.startsWith('PASS')).length}/${res.length} PASS`);
process.exit(res.every(x => x.startsWith('PASS')) ? 0 : 1);
