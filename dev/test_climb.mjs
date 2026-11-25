// 爬台模式（融合策略 2101）：
//   GO2W_FAKE_DUAL=1 dev/run.sh
//   node dev/test_climb.mjs [ws://127.0.0.1:8080/ws]
// 只能对本地假 ROS 运行（会让假机器人趴下 / 站起、急停）
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const sleep = ms => new Promise(r => setTimeout(r, ms));
function client(){
  const ws = new WebSocket(URL_), c = { ws, state: null, seq: 0, acks: new Map() };
  ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
    if (m.t === 'state') c.state = m; if (m.t === 'ack') { c.acks.get(m.id)?.(m); c.acks.delete(m.id); } };
  c.ping = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 300);
  c.cmd = (op, a = {}) => new Promise(r => { const id = ++c.seq; c.acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
  c.open = new Promise(r => ws.onopen = r);
  c.close = () => { clearInterval(c.ping); ws.close(); };
  return c;
}
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return Date.now() - t0; await sleep(50); } throw new Error('超时：' + what); };
const res = []; const ok = (n, c, x = '') => res.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  · ' + x : ''}`);
const A = client(); await A.open;
const C = () => A.state.ctrl, D = () => A.state.ctrl.dual;
await until(() => A.state?.ctrl?.state, 8000, '运控状态');
try {
  if (C().state !== 'Velocity') { const r = await A.cmd('stand_up'); ok('先站起', r.ok, r.msg); }
  ok('状态里有融合策略信息', !!D(), JSON.stringify(D()));
  ok('起始是 rough、爬台模式关', D()?.policy === 'rough' && !C().climb);

  let r = await A.cmd('climb', { on: true }); ok('开爬台模式', r.ok, r.msg);
  let ms = await until(() => D().policy === 'climb' && !D().switching, 2000, '切到 climb');
  ok('切到 climb 且过渡结束', true, `${ms} ms`);
  ok('ctrl.climb 有开启时间', C().climb?.since >= 0, JSON.stringify(C().climb));
  await sleep(3000);
  ok('3 s 后仍是 climb（后端在续约）', D().policy === 'climb' && D().req, JSON.stringify(D()));
  r = await A.cmd('goal', { x: 1, y: 0, th: 0 }); ok('开着时拒绝导航目标', !r.ok && /爬台/.test(r.msg), r.msg);
  r = await A.cmd('climb', { on: false }); ok('关爬台模式', r.ok, r.msg);
  ms = await until(() => D().policy === 'rough' && !D().switching, 2000, '回 rough');
  ok('关掉后回 rough', !C().climb, `${ms} ms`);

  // 开的网页断开：自动关
  const B = client(); await B.open; await until(() => B.state?.ctrl, 5000, 'B 收到状态');
  r = await B.cmd('climb', { on: true }); ok('另一个网页开爬台模式', r.ok, r.msg);
  await until(() => D().policy === 'climb', 2000, 'B 开后 climb');
  B.close();
  ms = await until(() => D().policy === 'rough' && !C().climb, 3000, '网页断开后回 rough');
  ok('开的网页断开 → 自动关、回 rough', true, `${ms} ms`);

  // 急停：自动关
  r = await A.cmd('climb', { on: true }); await until(() => D().policy === 'climb', 2000, 'climb');
  r = await A.cmd('estop'); ok('急停', true, r.msg);
  ms = await until(() => D().policy === 'rough' && !C().climb, 2000, '急停后回 rough');
  ok('急停 → 自动关', true, `${ms} ms`);
  r = await A.cmd('climb', { on: true }); ok('急停中拒绝开', !r.ok && /急停/.test(r.msg), r.msg);
  await A.cmd('release_estop');

  // 趴下：自动关；趴着时拒绝开
  r = await A.cmd('climb', { on: true }); await until(() => D().policy === 'climb', 2000, 'climb');
  r = await A.cmd('lie_down'); ok('趴下', r.ok, r.msg);
  ms = await until(() => !C().climb, 1500, '趴下后爬台模式关'); ok('趴下 → 爬台模式关', true, `${ms} ms`);
  await until(() => C().state === 'Passive', 3000, '趴下');
  r = await A.cmd('climb', { on: true }); ok('趴着时拒绝开', !r.ok && /站着/.test(r.msg), r.msg);
  r = await A.cmd('stand_up'); ok('站起', r.ok, r.msg);
  await until(() => D()?.policy === 'rough', 2000, '站起后 rough');
  ok('站起后是 rough', D().policy === 'rough');
} catch (e) { ok('异常', false, e.message); }
A.close();
console.log(res.join('\n'));
const bad = res.filter(x => x.startsWith('FAIL')).length;
console.log(bad ? `\n${bad} 项失败` : '\n全部通过'); process.exit(bad ? 1 : 0);
