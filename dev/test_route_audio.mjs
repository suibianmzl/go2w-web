// 站点播报音频（11-25）。只能对 dev/run.sh 的假 ROS 服务运行：node dev/test_route_audio.mjs [ws://127.0.0.1:8080/ws]
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
  const c = { ws, tasks, ping: true, heard: [] };
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
    if (m.t === 'audio'){ c.audio = m; if (m.cur && (c.heard.at(-1)?.clip !== m.cur.clip || c.heard.at(-1).done)) c.heard.push({ clip:m.cur.clip, who:m.cur.who, done:false, pose:c.pose && { ...c.pose } }); if (!m.cur && c.heard.length) c.heard.at(-1).done = true; }
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



import { writeFileSync, rmSync, readFileSync } from 'node:fs';
const BASE = url.replace('ws', 'http').replace(/\/ws$/, '');
const OFF = '/tmp/go2w_fake_audio_off', log = () => JSON.parse(readFileSync('/tmp/go2w_fake_audio.log'));
function wav(sec){
  const n = Math.round(sec * 16000), b = Buffer.alloc(44 + n * 2);
  b.write('RIFF', 0); b.writeUInt32LE(36 + n * 2, 4); b.write('WAVEfmt ', 8); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(16000, 24); b.writeUInt32LE(32000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) b.writeInt16LE(Math.round(8000 * Math.sin(i * .2)), 44 + i * 2);
  return b;
}
rmSync(OFF, { force:true });
const c = await client();
try {
  await until(() => c.state?.mapCurrent === map && c.pose?.frame === 'map' && c.audio?.avail, '地图、定位、喇叭', 20000);
  const cmd = async (op, a = {}) => { const r = await c.cmd(op, a); assert.equal(r.ok, true, `${op}: ${r.msg}`); return r; };
  for (const x of c.audio.items) await c.cmd('audio_delete', { clip:x.id });
  const up = async (name, sec) => { const r = await (await fetch(`${BASE}/api/audio?name=${encodeURIComponent(name)}`, { method:'POST', headers:{ 'X-Go2w':'1' }, body:wav(sec) })).json(); assert.ok(r.ok, r.msg); return r.item.id; };
  const A = await up('入口讲解', 1), B = await up('走廊讲解', 1.2), L = await up('长讲解', 12);
  const save = async (name, pts, opts = {}, route_id) => (await cmd('route_save', { map, name, points:pts, route_id, opts:{ loop:false, loops:1, vmax:.45, wmax:1.2, through:true, ...opts } })).data;
  const ended = (name, ms = 60000) => until(() => c.task?.name === name && c.task.status !== 'active', `${name} 结束`, ms);
  const quiet = () => until(() => !c.audio.cur, '播完', 20000);
  const go = async (r, from = { ...points[2], th:Math.PI / 2 }) => { await cmd('initial_pose', from); await cmd('arm', { on:true }); c.heard.length = 0; c.tasks.length = 0; c.logs = []; await cmd('route_start', { route_id:r.id }); };
  const names = () => c.heard.map(h => c.audio.items.find(x => x.id === h.clip)?.name);
  const made = [];

  for (const bad of ['xx', '../../a', 5, true, {}, ['a'], 'ABCDEF012345']) assert.equal((await c.cmd('route_save', { map, name:'坏', points:[first, { ...points[1], audio:bad }] })).ok, false, `拒绝 ${JSON.stringify(bad)}`);
  let r0 = await c.cmd('route_save', { map, name:'不存在', points:[first, { ...points[1], audio:'0123456789ab' }] });
  assert.ok(!r0.ok && /第 2 站/.test(r0.msg), r0.msg);
  const z = await save('空值', [{ ...first, audio:'' }, { ...points[1], audio:null }]);
  assert.ok(!z.points.some(p => 'audio' in p), '空值不存'); await cmd('route_delete', { route_id:z.id });
  console.log('PASS 校验：格式不对 / 音频不存在拒绝保存，空 = 不播报');

  const pts = [{ ...points[0], audio:A }, { ...points[1], audio:B, dwell:2 }, { ...points[2], audio:A }];
  const r = await save('播报-连续', pts); made.push(r);
  assert.deepEqual(r.points.map(p => p.audio), [A, B, A]); assert.equal(c.routes.find(x => x.id === r.id).points[1].audio, B);
  await go(r); await until(() => c.task?.phase === 'dwell', '停留', 40000); assert.equal(c.task.pointAudio, '走廊讲解');
  await ended(r.name); assert.equal(c.task.status, 'succeeded', c.task.message); await quiet();
  assert.deepEqual(names(), ['入口讲解', '走廊讲解', '入口讲解']); assert.match(c.heard[1].who, /走廊/); assert.equal(c.audio.last.ok, true);
  assert.ok(Math.hypot(c.heard[1].pose.x - pts[1].x, c.heard[1].pose.y - pts[1].y) < .5, '在第 2 站播');
  console.log('PASS 连续通过：途经 / 停留 / 终点各播一次，正常跑完最后一站播完');

  const s = await save('播报-逐站', pts, { through:false }); made.push(s);
  await go(s); await ended(s.name); assert.equal(c.task.status, 'succeeded', c.task.message); await quiet();
  assert.deepEqual(names(), ['入口讲解', '走廊讲解', '入口讲解']); console.log('PASS 一站一站走：同样各播一次');

  const q = await save('播报-起点', [{ ...points[0], audio:B }, points[1], points[2]]); made.push(q);
  await go(q, { ...first, th:-Math.PI / 2 }); await until(() => c.heard.length, '起点播', 8000); await ended(q.name); await quiet();
  assert.deepEqual(names(), ['走廊讲解']); console.log('PASS 已在第 1 站附近出发：第 1 站照样播');

  const lp = await save('播报-循环', [{ ...points[0], audio:A }, points[1], points[2]], { loop:true, loops:2 }); made.push(lp);
  await cmd('initial_pose', { ...points[2], th:Math.PI / 2 }); await cmd('arm', { on:true }); c.heard.length = 0;
  await cmd('route_start', { route_id:lp.id, round_trip:true, loops:2 }); await ended(lp.name, 120000); assert.equal(c.task.status, 'succeeded', c.task.message); await quiet();
  assert.deepEqual(names(), ['入口讲解', '入口讲解', '入口讲解']); console.log('PASS 循环 2 圈：出发、每次回到 1 号点都播（3 次）');

  const k = await save('播报-取消', [{ ...points[0] }, { ...points[1], audio:L, dwell:20 }, points[2]]); made.push(k);
  await go(k); await until(() => c.audio.cur?.phase === 'play', '播长讲解', 40000);
  await cmd('cancel'); await until(() => !c.audio.cur, '取消后停播', 1500); assert.equal(log().at(-1), 4002); assert.equal(c.audio.last.msg, '被停止');
  await go(k); await until(() => c.audio.cur, '再播', 40000); await cmd('estop'); await until(() => !c.audio.cur, '急停后停播', 1500); await cmd('release_estop');
  console.log('PASS 取消、急停：任务播的那段马上停');
  await go(k); await until(() => c.audio.cur?.clip === L, '再播', 40000); await cmd('audio_play', { clip:A }); await until(() => c.audio.cur?.clip === A, '手动打断');
  await cmd('cancel'); await sleep(400); assert.ok(c.audio.cur?.clip === A || c.audio.last.clip === A && c.audio.last.ok, '手动播的不该被任务停掉'); await quiet();
  console.log('PASS 手动播的不会被任务取消停掉');

  const d = await c.cmd('audio_delete', { clip:B }); assert.ok(!d.ok && /播报-连续.*第 2 站/.test(d.msg), d.msg); console.log('PASS 在用的音频不能删：' + d.msg);

  writeFileSync(OFF, '1'); await until(() => !c.audio.avail, '喇叭离线', 4000);
  await go(r); await ended(r.name); assert.equal(c.task.status, 'succeeded', c.task.message); assert.equal(c.heard.length, 0);
  assert.ok(c.logs.some(m => /播报没播/.test(m)), '日志说明'); rmSync(OFF); await until(() => c.audio.avail, '恢复', 4000);
  console.log('PASS 喇叭不可用：任务照跑，日志写明没播');

  for (const x of made) await c.cmd('route_delete', { route_id:x.id });
  for (const x of [A, B, L]) await cmd('audio_delete', { clip:x }); console.log('PASS 任务删了之后音频能删');
} finally { rmSync(OFF, { force:true }); c.close(); }
