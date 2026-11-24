// 任务接续（11-24）。只能对 dev/run.sh 的假 ROS 服务运行：node dev/test_route_resume.mjs [ws://127.0.0.1:8080/ws]
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


const c = await client(), observer = await client();
try {
  await until(() => c.state?.mapCurrent === map && c.pose?.frame === 'map', '地图和定位', 20000);
  async function command(op, args){
    const result = await c.cmd(op, args);
    assert.equal(result.ok, true, `${op}: ${result.msg}`);
    return result;
  }
  // 一站一站走（站距 0.7 m，连续通过时「去第 2 站」一闪而过）；连续通过下的接续见 test_route_through.mjs
  const LEGACY = { loop: false, loops: 1, vmax: .45, wmax: 1.2, through: false };
  const save = async (name, pts, opts) => (await command('route_save', { map, name, points: pts, opts: { ...LEGACY, ...(opts || {}) } })).data;
  const resumeOf = (who, id) => who.routes?.find(r => r.id === id)?.resume ?? null;
  const legs = name => c.tasks.filter(t => t.name === name && t.phase !== 'checking' && t.status === 'active')
    .map(t => `${t.loop}:${t.back ? '↩' : ''}${t.point}`).filter((v, i, a) => v !== a[i - 1]);
  async function go(route, args = {}){
    await command('arm', { on: true });
    const r = await command('route_start', { route_id: route.id, ...args });
    await until(() => c.task?.name === route.name && c.task.status === 'active', `${route.name} 开始`);
    return r;
  }
  const ended = (name, ms = 60000) => until(() => c.task?.name === name && c.task.status !== 'active', `${name} 结束`, ms);

  // 1 单程：到过第 1 站、去第 2 站路上手动取消 → 断点 = 第 2 站，已完成 1 站
  const r1 = await save('接续单程', points);
  assert.equal(resumeOf(c, r1.id), null, '新任务没有断点');
  assert.equal((await c.cmd('route_start', { route_id: r1.id, resume: 'continue' })).ok, false, '没有断点时拒绝接续');
  await command('initial_pose', { ...points[2], th: Math.PI / 2 });
  c.tasks.length = 0;
  await go(r1);
  await until(() => c.task?.point === 2 && c.task.phase === 'follow', '去第 2 站', 30000);
  await command('cancel');
  await until(() => c.task?.status === 'canceled', '取消');
  await until(() => resumeOf(observer, r1.id)?.point === 2, '另一客户端收到断点');
  let R = resumeOf(observer, r1.id);
  assert.equal(R.done, 1); assert.deepEqual(R.finished, [1]); assert.equal(R.status, 'canceled');
  assert.equal(R.count, 3); assert.equal(R.roundTrip, false); assert.equal(R.canSkip, true); assert.equal(R.skipPoint, 3);
  assert.ok(Array.isArray(R.pose) && R.pose.length === 3, '记了停下的位置');
  assert.equal(c.task.resumable, true, 'task 卡片标 resumable');
  console.log('PASS 手动取消记断点：第 2 站、已完成 1 站、停车位置');

  // 接续：从第 2 站跑完 2、3，不回第 1 站；跑完断点清掉
  c.tasks.length = 0;
  const msg = (await go(r1, { resume: 'continue' })).msg;
  assert.match(msg, /从第 2 站继续/);
  assert.equal(c.task.resumed, true);
  await ended(r1.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.deepEqual([...new Set(legs(r1.name).map(v => v.split(':')[1]))], ['2', '3'], `接续站序 ${legs(r1.name)}`);
  assert.equal(c.task.progress, 1);
  await until(() => resumeOf(observer, r1.id) === null, '跑完断点清掉');
  console.log('PASS 继续：从第 2 站跑完剩下的站，进度 100%，断点清掉');

  // 2 循环 2 圈：在第 2 圈中途取消，圈数接着算
  const r2 = await save('接续循环', points, { loop: true, loops: 2, vmax: .45, wmax: 1.2 });
  await command('initial_pose', { ...points[2], th: Math.PI / 2 });
  c.tasks.length = 0;
  await go(r2, { round_trip: true, loops: 2 });
  await until(() => c.task?.loop === 2 && c.task.point === 3 && c.task.phase === 'follow', '第 2 圈去第 3 站', 90000);
  await command('estop');
  await until(() => c.task?.status === 'canceled', '急停取消');
  await command('release_estop');
  await until(() => resumeOf(c, r2.id)?.loop === 2, '循环断点');
  R = resumeOf(c, r2.id);
  assert.equal(R.point, 3); assert.equal(R.loops, 2); assert.equal(R.roundTrip, true);
  assert.deepEqual(R.finished, [1, 2]); assert.equal(R.done, 5, `1 2 3 ↩1 2 = 5 站，实际 ${R.done}`); assert.equal(R.total, 7);
  c.tasks.length = 0;
  await go(r2, { resume: 'continue', round_trip: false, loops: 1 });   // 接续不看这次给的圈数
  await ended(r2.name, 60000);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.equal(c.task.message, '已完成循环 2 圈');
  assert.deepEqual(legs(r2.name), ['2:3', '2:↩1'], `第 2 圈接着跑 ${legs(r2.name)}`);
  console.log('PASS 循环：第 2 圈断点，接续后只跑完第 2 圈剩下的站，不重新算圈数');

  // 3 第 2 站到不了 → 失败记断点；「跳过」从第 3 站继续；最后一站不能跳
  const r3 = await save('接续跳过', [points[2], { x: 100, y: 100, th: 0 }, first]);
  c.tasks.length = 0;
  await go(r3);
  await until(() => c.task?.name === r3.name && c.task.status === 'aborted', '第 2 站失败', 40000);
  await until(() => resumeOf(c, r3.id)?.status === 'aborted', '失败断点');
  assert.equal(resumeOf(c, r3.id).point, 2);
  c.tasks.length = 0;
  await go(r3, { resume: 'skip' });
  await ended(r3.name);
  assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.deepEqual([...new Set(legs(r3.name).map(v => v.split(':')[1]))], ['3']);
  console.log('PASS Nav2 失败记断点，跳过这站从下一站接着跑');

  // 4 改站点 → 断点作废；只改名字不作废；从头出发也清掉断点
  const r4 = await save('接续作废', points);
  await command('initial_pose', { ...points[2], th: Math.PI / 2 });
  await go(r4);
  await until(() => c.task?.point === 2 && c.task.phase === 'follow', 'r4 去第 2 站', 30000);
  await command('cancel');
  await until(() => resumeOf(c, r4.id)?.point === 2, 'r4 断点');
  await command('route_save', { route_id: r4.id, map, name: '接续作废改名', points });
  await until(() => c.routes.find(r => r.id === r4.id)?.name === '接续作废改名', '改名');
  assert.equal(resumeOf(c, r4.id)?.point, 2, '只改名字断点还在');
  await command('route_save', { route_id: r4.id, map, name: '接续作废改名', points: [points[0], { ...points[1], x: points[1].x + .2 }, points[2]] });
  await until(() => resumeOf(c, r4.id) === null, '改了站点断点作废');
  assert.equal((await c.cmd('route_start', { route_id: r4.id, resume: 'continue' })).ok, false);
  console.log('PASS 只改名字断点保留，改了站点断点作废');

  const r5 = await save('从头出发', points);
  await command('initial_pose', { ...points[2], th: Math.PI / 2 });
  await go(r5);
  await until(() => c.task?.point === 2 && c.task.phase === 'follow', 'r5 去第 2 站', 30000);
  await command('cancel');
  await until(() => resumeOf(c, r5.id)?.point === 2, 'r5 断点');
  await go(r5);
  await until(() => resumeOf(c, r5.id) === null, '从头出发清掉旧断点');
  await command('cancel');
  console.log('PASS 从头出发清掉旧断点');
  for (const r of [r1, r2, r3, r4, r5]) await c.cmd('route_delete', { route_id: r.id });
  console.log('ALL PASS');
} finally {
  c.close(); observer.close();
}
