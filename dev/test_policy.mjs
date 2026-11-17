// 运控策略切换（只在趴着时）：
//   GO2W_FAKE_CTRL=Passive GO2W_FAKE_POLICY_FAIL=mjlab_go2w_cmd_m22 web/dev/run.sh
//   node web/dev/test_policy.mjs [ws://127.0.0.1:8080/ws]
// 只能对本地假 ROS 运行（会让假机器人站起 / 趴下）
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ws = new WebSocket(URL_); let state = null, seq = 0; const acks = new Map();
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
  if (m.t === 'state') state = m; if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
const ping = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 300);
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return; await sleep(100); } throw new Error('超时：' + what); };
const res = []; const ok = (n, c, x = '') => res.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  · ' + x : ''}`);
const C = () => state.ctrl;
await new Promise(r => ws.onopen = r); await until(() => state?.ctrl?.state, 8000, '运控状态');
try {
  ok('列出可选策略', C().policies?.length === 4, JSON.stringify(C().policies));
  const p0 = C().policy; ok('读到当前策略', !!p0, p0);
  const other = C().policies.find(p => p !== p0 && p !== 'mjlab_go2w_cmd_m22');
  let r = await cmd('set_policy', { name: 'nope' }); ok('拒绝不存在的策略', !r.ok, r.msg);
  r = await cmd('set_policy', { name: p0 }); ok('同一个策略直接返回', r.ok, r.msg);

  // 站着时拒绝
  r = await cmd('stand_up'); ok('站起', r.ok, r.msg);
  r = await cmd('set_policy', { name: other }); ok('站着时拒绝切换', !r.ok && /趴着/.test(r.msg), r.msg);
  ok('拒绝后策略没变', C().policy === p0);
  r = await cmd('lie_down'); ok('趴下', r.ok, r.msg);
  await until(() => C().state === 'Passive' && C().ready, 5000, '趴下');

  // 正常切换：过程中 op = policy，没有离线告警（ctrl 状态在重启期间会断）
  let sawOp = false; const w = setInterval(() => { if (C().op === 'policy') sawOp = true; }, 50);
  const t0 = Date.now(); r = await cmd('set_policy', { name: other });
  clearInterval(w);
  ok('趴着时切换成功', r.ok, `${r.msg} · ${Date.now() - t0} ms`);
  ok('切换中显示 op=policy', sawOp);
  await until(() => C().policy === other && !C().op, 3000, '状态里的策略更新');
  ok('切完仍趴着', C().state === 'Passive' && !C().op, JSON.stringify(C()).slice(0, 120));
  r = await cmd('stand_up'); ok('新策略下能站起', r.ok, r.msg);
  r = await cmd('lie_down'); await until(() => C().state === 'Passive' && C().ready, 5000, '趴下');

  // 新策略起不来：自动退回
  r = await cmd('set_policy', { name: 'mjlab_go2w_cmd_m22' });
  ok('新策略起不来时报失败并退回', !r.ok && /已退回/.test(r.msg), r.msg);
  await until(() => !C().op, 3000, '操作结束');
  ok('退回后策略恢复', C().policy === other, C().policy);
  ok('退回后仍趴着、在线', C().state === 'Passive', C().state);

  // 切回原来的
  r = await cmd('set_policy', { name: p0 }); ok('切回原策略', r.ok, r.msg);
} catch (e) { ok('异常', false, e.message); }
clearInterval(ping); ws.close();
console.log(res.join('\n'));
const bad = res.filter(x => x.startsWith('FAIL')).length;
console.log(bad ? `\n${bad} 项失败` : '\n全部通过'); process.exit(bad ? 1 : 0);
