// 连续通过（多点导航 NavigateThroughPoses，v1.1.11）。只能对 dev/run.sh 的假 ROS 服务运行：node dev/test_route_through.mjs [ws://127.0.0.1:8080/ws]
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
const D = { x:3.06, y:-2.71, th:0, name:'D', manualHeading:true };
const four = [{ ...points[0], name:'A' }, { ...points[1], name:'B' }, { ...points[2], name:'C' }, D];
try {
  await until(() => c.state?.mapCurrent === map && c.pose?.frame === 'map', '地图和定位', 20000);
  const cmd = async (op, a = {}) => { const r = await c.cmd(op, a); assert.equal(r.ok, true, `${op}: ${r.msg}`); return r; };
  const save = async (name, pts, opts) => (await cmd('route_save', { map, name, points: pts, ...(opts ? { opts } : {}) })).data;
  const ended = (name, ms = 90000) => until(() => c.task?.name === name && c.task.status !== 'active', `${name} 结束`, ms);
  const chk = await c.cmd('route_check', { map, points: four });
  assert.equal(chk.ok, true, `测试路线要能规划：${chk.msg}`);
  const run = async (r, args = {}) => {
    await cmd('initial_pose', { ...points[2], th:Math.PI / 2 });   // 在 C、离 A 1.37 m：从 A 开始
    await cmd('arm', { on:true });
    c.tasks.length = 0; c.logs = [];
    await cmd('route_start', { route_id:r.id, ...args });
    await until(() => c.task?.name === r.name && c.task.status === 'active', `${r.name} 开始`);
  };
  const pts = name => [...new Set(c.tasks.filter(t => t.name === name && t.phase !== 'checking').map(t => t.point))];

  // 1 默认连续通过：一个多点目标，B、C 不停车经过，只有 D 到达
  const r1 = await save('连续通过', four);
  assert.equal(r1.opts.through, true, '默认开');
  await run(r1);
  await ended(r1.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.equal(c.task.through, true);
  assert.ok(c.logs.some(m => /连续通过：第 1–4 站（4 站/.test(m)), `一段 4 站：${c.logs.filter(m => /连续/.test(m))}`);
  for (const k of [1, 2, 3]) assert.ok(c.logs.some(m => new RegExp(`经过第 ${k} 站.*不停车`).test(m)), `经过第 ${k} 站`);
  assert.equal(c.logs.filter(m => /^到达目标/.test(m)).length, 1, `只有终点「到达」：${c.logs.filter(m => /到达目标/.test(m))}`);
  assert.ok(c.logs.some(m => /多点规划用时 [\d.]+ s/.test(m)), '记规划用时');
  const seq = pts(r1.name);   // A、B 只隔 0.7 m（经过半径 0.5 m），去 B 的那一下可能不到一个推送周期，不一定采得到
  assert.ok(seq[0] === 1 && seq.at(-1) === 4 && seq.every((v, i) => !i || v > seq[i - 1]), `进度站序 ${seq}`);
  assert.ok(Math.hypot(c.pose.x - D.x, c.pose.y - D.y) < .3, '停在 D');
  console.log('PASS 连续通过：一次多点目标，中间站不停车经过，只在终点到达，进度按站更新');

  // 2 C 设停留：分两段 A→C（停留）、C→D
  const r2 = await save('分段停留', [four[0], four[1], { ...four[2], dwell:2 }, D]);
  await run(r2);
  await until(() => c.task?.name === r2.name && c.task.phase === 'dwell', 'C 停留', 60000);
  assert.equal(c.task.point, 3);
  assert.ok(Math.hypot(c.pose.x - four[2].x, c.pose.y - four[2].y) < .3, '停在 C');
  await ended(r2.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  const segs = c.logs.filter(m => /连续通过：/.test(m));
  assert.deepEqual(segs.map(m => m.match(/第 ([\d–]+) 站/)[1]), ['1–3', '4'], `分段 ${segs}`);
  assert.ok(/到了停留/.test(segs[0]));
  console.log('PASS 停留站把路线切成两段，停留站停稳停够再走下一段');

  // 3 中途取消：断点在下一站；继续 = 从这一站起新的一段
  await run(r1);
  await until(() => c.task?.name === r1.name && c.task.point === 3 && c.task.phase === 'follow', '过了 B 去 C', 60000);
  await cmd('cancel');
  await until(() => c.task?.status === 'canceled', '取消');
  await until(() => c.routes.find(x => x.id === r1.id)?.resume?.point === 3, '断点第 3 站');
  assert.equal(c.routes.find(x => x.id === r1.id).resume.done, 2);
  await cmd('arm', { on:true });
  c.tasks.length = 0; c.logs = [];
  await cmd('route_start', { route_id:r1.id, resume:'continue' });
  await until(() => c.task?.name === r1.name && c.task.status === 'active' && c.task.resumed, '接续开始');   // 旧卡片「已取消」还留 10 s
  await ended(r1.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.ok(c.logs.some(m => /连续通过：第 3–4 站/.test(m)), `接续段 ${c.logs.filter(m => /连续/.test(m))}`);
  assert.deepEqual(pts(r1.name), [3, 4]);
  console.log('PASS 连续通过中途取消，断点在下一站，继续从那一站起新的一段');

  // 4 循环 2 圈：1 段跑完（1 2 3 4 ↩1 2 3 4 ↩1 = 9 站 < 30）
  const r3 = await save('连续循环', four, { loop:true, loops:2, vmax:.45, wmax:1.2, through:true });
  await run(r3, { round_trip:true, loops:2 });
  await ended(r3.name, 180000);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.equal(c.task.message, '已完成循环 2 圈');
  assert.equal(c.logs.filter(m => /连续通过：/.test(m)).length, 1, '两圈一段');
  const laps = c.tasks.filter(t => t.name === r3.name && t.phase !== 'checking').map(t => `${t.loop}:${t.back ? '↩' : ''}${t.point}`).filter((v, i, a) => v !== a[i - 1]);
  // 第 2 站离 1 号点只有 0.7 m（经过半径 0.5 m），「去第 2 站」不一定采得到，比较时去掉
  assert.deepEqual(laps.filter(v => !v.endsWith(':2')), ['1:1', '1:3', '1:4', '1:↩1', '2:3', '2:4', '2:↩1'], `${laps}`);
  assert.ok(c.logs.filter(m => /经过第 2 站/.test(m)).length === 2, '两圈都经过第 2 站');
  console.log('PASS 循环 2 圈连续通过，圈数、回 1 号点都对');

  // 4b 连续通过下的假到达：Nav2 0.3 s 就报整段到了，A、B、C 都没经过 → 不算，从 A 重新出发，每站照样经过
  writeFileSync('/tmp/go2w_fake_early_ok', '1');
  await run(r1);
  await ended(r1.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.ok(c.logs.some(m => /Nav2 报到达，但第 1 站还没经过：不算到达/.test(m)), `识别假到达：${c.logs.filter(m => /报到达/.test(m))}`);
  for (const k of [1, 2, 3]) assert.ok(c.logs.some(m => new RegExp(`经过第 ${k} 站`).test(m)), `重发后经过第 ${k} 站`);
  assert.ok(Math.hypot(c.pose.x - D.x, c.pose.y - D.y) < .3, '最后真的到了 D');
  console.log('PASS 连续通过：Nav2 整段假到达（中间站没经过）不算数，重新出发，每站都经过');

  // 5 关掉连续通过：一站一站走（没有「连续通过」日志，每站都有 Nav2 目标）
  const r4 = await save('一站一站', four, { loop:false, loops:1, vmax:.45, wmax:1.2, through:false });
  await run(r4);
  await ended(r4.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.equal(c.task.through, false);
  assert.equal(c.logs.some(m => /连续通过：/.test(m)), false);
  console.log('PASS 关掉连续通过 = 原来的一站一站走');
  for (const x of [r1, r2, r3, r4]) await c.cmd('route_delete', { route_id:x.id });
  console.log('ALL PASS');
} finally { rmSync('/tmp/go2w_fake_early_ok', { force:true }); c.close(); }
