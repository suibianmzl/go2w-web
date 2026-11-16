// 自动定位失败后「移动后重试」+「两处结果一致」：
//   GO2W_AUTOLOC_MIN_SCORE=1.01 GO2W_FAKE_START=2,-0.5,0 dev/run.sh
//   （单次定位永远不可信，只能靠两处一致；lab_20261021 原点附近只有 0.47 m 空间，换个空旷的起点）
//   node dev/test_autoloc_move.mjs [ws://127.0.0.1:8080/ws]
// 只能对本地假 ROS 运行（会让假机器人动）
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ws = new WebSocket(URL_); let state = null, robot = null, seq = 0; const acks = new Map();
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
  if (m.t === 'state') state = m; if (m.t === 'pose' && m.valid) robot = m; if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
const ping = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 300);
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return; await sleep(100); } throw new Error('超时：' + what); };
const res = []; const ok = (n, c, x = '') => res.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  · ' + x : ''}`);
const prof = k => state.profiles[k];
const pose = () => ({ x: robot?.x, y: robot?.y });
await new Promise(r => ws.onopen = r); await until(() => state, 6000, '首个状态');
try {
  await cmd('arm', { on: false });
  if (prof('navigation') !== 'run') {
    const r = await cmd('set_mode', { mode: 'navigation' }); ok('启动导航', r.ok, r.msg);
    await until(() => prof('navigation') === 'run' && !state.busy, 25000, '导航起来');
  }
  // 启动后自动定位一次（必然不可信）：等它出结果（最多 40 s），没等到就自己点
  await until(() => state.autoLoc?.state === 'fail', 40000, '启动后自动定位').catch(() => null);
  if (state.autoLoc?.state !== 'fail') { const r = await cmd('auto_localize'); ok('手动自动定位：不可信', r.ok === false, r.msg); }
  await until(() => state.autoLoc?.state === 'fail', 3000, '失败状态推到网页');
  ok('失败后可以移动重试', state.autoLoc.movesLeft === 3, JSON.stringify(state.autoLoc).slice(0, 160));
  ok('失败原因：分数（不是歧义）', /分数太低/.test(state.autoLoc.msg), state.autoLoc.msg);
  // 原地再定一次：结果一样，但没挪开，不能算两处一致
  let r = await cmd('auto_localize');
  ok('原地重复定位不算一致', r.ok === false, r.msg);
  ok('失败提示里有「移动后重试」', /移动后重试/.test(r.msg));

  // 急停时拒绝
  await cmd('estop'); r = await cmd('autoloc_move'); ok('急停时拒绝移动', r.ok === false && /急停/.test(r.msg), r.msg);
  await cmd('release_estop');

  const p0 = pose(), armed0 = state.armed;
  let sawMove = false; const watch = setInterval(() => { if (state.autoLoc?.state === 'move') sawMove = true; }, 50);
  r = await cmd('autoloc_move');
  clearInterval(watch);
  const p1 = pose(), d = Math.hypot(p1.x - p0.x, p1.y - p0.y);
  ok('移动后重试：两处一致，定位成功', r.ok === true && /两处结果一致/.test(r.msg), r.msg);
  ok('移动中网页看到 move 状态', sawMove);
  ok('真的挪了约 0.3 m（前面不够空时是原地转）', d > 0.2 || /转/.test(r.msg), `位移 ${d.toFixed(2)} m`);
  await sleep(700);
  ok('结束后恢复上锁（原来没解锁）', armed0 !== true ? state.armed !== true : true, `armed=${state.armed}`);
  ok('成功后移动次数复位', state.autoLoc?.state === 'ok' && state.autoLoc.movesLeft === 3, JSON.stringify(state.autoLoc).slice(0, 120));
  r = await cmd('autoloc_move'); ok('定位成功后不能再移动重试', r.ok === false, r.msg);
} catch (e) { res.push('FAIL  ' + e.message); }
console.log(res.join('\n')); clearInterval(ping); ws.close();
process.exit(res.some(x => x.startsWith('FAIL')) ? 1 : 0);
