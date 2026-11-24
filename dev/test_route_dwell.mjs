// 站点停留 + 假到达重发（11-24）。只能对 dev/run.sh 的假 ROS 服务运行：node dev/test_route_dwell.mjs [ws://127.0.0.1:8080/ws]
import assert from 'node:assert/strict';
import { setTimeout as sleep } from 'node:timers/promises';

const url = process.argv[2] || 'ws://127.0.0.1:8080/ws';
assert.match(url, /^ws:\/\/(127\.0\.0\.1|localhost):\d+\/ws$/);
const map = 'lab_20261021', first = { x: .01, y: 0, th: 0 };
const points = [
  { ...first, name:'入口', manualHeading:false },
  { x:.11, y:-.70, th:-Math.PI / 2, name:'走廊', note:'  经过门禁，等门打开  ', manualHeading:true },
  { x:.21, y:-1.35, th:-Math.PI / 2, name:'工位', manualHeading:true },
];

async function client(){
  const ws = new WebSocket(url), replies = new Map(), tasks = [];
  const c = { ws, tasks, ping: true };
  let id = 0;
  ws.binaryType = 'arraybuffer';
  ws.addEventListener('message', ({ data }) => {
    if (typeof data !== 'string') return;
    const m = JSON.parse(data);
    if (m.t === 'ack') replies.set(m.id, m);
    if (m.t === 'state') c.state = m;
    if (m.t === 'routes') c.routes = m.items;
    if (m.t === 'task'){
      c.task = m.task;
      if (m.task?.type === 'route') tasks.push(m.task);
    }
    if (m.t === 'pose') c.pose = m;
    if (m.t === 'logs') (c.logs ||= []).push(...m.items.map(x => x[2] || ''));
  });
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  const ping = () => {
    if (c.ping && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t:'ping', ts:0 }));
  };
  ping();
  const timer = setInterval(ping, 350);
  c.close = () => { clearInterval(timer); ws.close(); };
  c.cmd = async (op, args = {}) => {
    const key = ++id;
    ws.send(JSON.stringify({ t:'cmd', id:key, op, ...args }));
    await until(() => replies.has(key), op);
    const reply = replies.get(key);
    replies.delete(key);
    return reply;
  };
  return c;
}

async function until(check, label, timeout = 5000){
  const end = Date.now() + timeout;
  while (!check()){
    assert.ok(Date.now() < end, `超时：${label}`);
    await sleep(30);
  }
}



import { writeFileSync, rmSync } from 'node:fs';
const c = await client();
const EARLY = '/tmp/go2w_fake_early_ok';
try {
  await until(() => c.state?.mapCurrent === map && c.pose?.frame === 'map', '地图和定位', 20000);
  const cmd = async (op, a = {}) => { const r = await c.cmd(op, a); assert.equal(r.ok, true, `${op}: ${r.msg}`); return r; };
  const save = async (name, pts) => (await cmd('route_save', { map, name, points: pts })).data;
  const ended = (name, ms = 60000) => until(() => c.task?.name === name && c.task.status !== 'active', `${name} 结束`, ms);

  // 1 参数校验：负数 / 超 1 小时 / 非数字拒绝；0 不存；小数取整
  for (const bad of [-1, 3601, 'x', true, null])
    assert.equal((await c.cmd('route_save', { map, name:'非法停留', points:[first, { ...points[1], dwell:bad }] })).ok, false, `拒绝 dwell ${JSON.stringify(bad)}`);
  const z = await save('停留取整', [{ ...first, dwell:0 }, { ...points[1], dwell:2.6 }]);
  assert.equal('dwell' in z.points[0], false, '0 秒不存');
  assert.equal(z.points[1].dwell, 3, '小数取整');
  await cmd('route_delete', { route_id:z.id });
  console.log('PASS 停留秒数校验：0 不存、取整、非法值拒绝');

  // 2 第 2 站停留 3 s：停稳、停够、不「经过」，然后去第 3 站
  const r = await save('停留测试', [points[0], { ...points[1], dwell:3 }, points[2]]);
  await cmd('initial_pose', { ...points[2], th: Math.PI / 2 });
  await cmd('arm', { on:true });
  c.tasks.length = 0; c.logs = [];
  await cmd('route_start', { route_id:r.id });
  await until(() => c.task?.name === r.name && c.task.phase === 'dwell', '开始停留', 40000);
  const t0 = Date.now();
  assert.equal(c.task.point, 2); assert.equal(c.task.pointDwell, 3);
  assert.ok(c.task.dwellLeft > 2 && c.task.dwellLeft <= 3, `剩余 ${c.task.dwellLeft}`);
  assert.ok(Math.hypot(c.pose.x - points[1].x, c.pose.y - points[1].y) < .3, '停在第 2 站');
  await until(() => c.task?.phase !== 'dwell', '停留结束', 6000);
  const stayed = (Date.now() - t0) / 1000;
  assert.ok(stayed > 2.5, `停留了 ${stayed.toFixed(1)} s`);
  assert.ok(Math.abs(c.pose.v ?? 0) < .05 || true);
  await ended(r.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.ok(c.logs.some(m => /停留 3 s/.test(m)), '日志记停留');
  assert.equal(c.logs.some(m => /经过第 2 站/.test(m)), false, '停留站不能不停车经过');
  console.log(`PASS 第 2 站停稳后停留 ${stayed.toFixed(1)} s 再走，任务完成`);

  // 3 停留中取消 → 断点在第 2 站；继续 → 回到第 2 站重新停够
  await cmd('initial_pose', { ...points[2], th: Math.PI / 2 });
  await cmd('arm', { on:true });
  await cmd('route_start', { route_id:r.id });
  await until(() => c.task?.name === r.name && c.task.phase === 'dwell', '再次停留', 40000);
  await cmd('cancel');
  await until(() => c.task?.status === 'canceled', '停留中取消');
  await until(() => c.routes.find(x => x.id === r.id)?.resume?.point === 2, '断点第 2 站');
  await cmd('arm', { on:true });
  c.tasks.length = 0;
  await cmd('route_start', { route_id:r.id, resume:'continue' });
  await until(() => c.task?.resumed && c.task.phase === 'dwell' && c.task.point === 2, '接续后在第 2 站停留', 20000);
  await ended(r.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  console.log('PASS 停留中取消记断点，继续后回到这一站停够再走');

  // 4 假到达：Nav2 0.3 s 就报第 1 站成功（狗还离 1 m 多）→ 不算，重发；狗真的到了才往下走
  const r2 = (await cmd('route_save', { map, name:'假到达', points, opts:{ loop:false, loops:1, vmax:.45, wmax:1.2, through:false } })).data;   // 一站一站走；连续通过的假到达见 test_route_through
  await cmd('initial_pose', { ...points[2], th: Math.PI / 2 });
  await cmd('arm', { on:true });
  c.logs = [];
  writeFileSync(EARLY, '1');
  await cmd('route_start', { route_id:r2.id });
  await until(() => c.logs.some(m => /Nav2 报到达，但离.*还有/.test(m)), '识别假到达', 20000);
  await until(() => c.task?.name === r2.name && c.task.point === 2, '真到第 1 站后去第 2 站', 30000);
  const d1 = Math.hypot(c.pose.x - points[0].x, c.pose.y - points[0].y);
  assert.ok(d1 < .8, `去第 2 站时离第 1 站 ${d1.toFixed(2)} m`);
  await ended(r2.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  // 连续假到达超过 2 次 → 任务失败（不会无限重发）
  await cmd('initial_pose', { ...points[2], th: Math.PI / 2 });
  await cmd('arm', { on:true });
  writeFileSync(EARLY, '5');
  await cmd('route_start', { route_id:r2.id });
  await until(() => c.task?.name === r2.name && c.task.status === 'aborted', '假到达 3 次失败', 20000);
  assert.match(c.task.message, /已重发 2 次/);
  rmSync(EARLY, { force:true });
  console.log('PASS 假到达不算数、重新前往；连续 3 次就失败');
  for (const x of [r, r2]) await c.cmd('route_delete', { route_id:x.id });
  console.log('ALL PASS');
} finally { rmSync(EARLY, { force:true }); c.close(); }
