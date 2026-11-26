// 站点多段播报（11-25）：到站按顺序播几段、离开前播完再走。只能对 dev/run.sh 的假 ROS 服务运行：node dev/test_route_audio_multi.mjs [ws://127.0.0.1:8080/ws]
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
    if (m.t === 'audio'){   // 推送是 latest-wins，两段之间的 cur = null 可能被合并掉：靠 last.ts 变化也算上一段结束
      c.audio = m;
      const prev = c.heard.at(-1);
      if (prev && !prev.done && (!m.cur || m.cur.clip !== prev.clip || m.last?.ts !== prev.lastTs)){
        Object.assign(prev, { done:true, t1:Date.now(), end:c.pose && { ...c.pose }, ok:m.last?.clip === prev.clip && m.last.ts !== prev.lastTs ? m.last.ok : undefined });
      }
      if (m.cur && (!c.heard.length || c.heard.at(-1).done))
        c.heard.push({ clip:m.cur.clip, who:m.cur.who, done:false, pose:c.pose && { ...c.pose }, waited:m.cur.phase === 'wait' ? m.cur.wait : 0, t0:Date.now(), lastTs:m.last?.ts });
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

import { rmSync, readFileSync } from 'node:fs';
const BASE = url.replace('ws', 'http').replace(/\/ws$/, '');
function wav(sec){
  const n = Math.round(sec * 16000), b = Buffer.alloc(44 + n * 2);
  b.write('RIFF', 0); b.writeUInt32LE(36 + n * 2, 4); b.write('WAVEfmt ', 8); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(16000, 24); b.writeUInt32LE(32000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) b.writeInt16LE(Math.round(8000 * Math.sin(i * .2)), 44 + i * 2);
  return b;
}
rmSync('/tmp/go2w_fake_audio_off', { force:true });
const c = await client();
const far = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
// 11-26 main：停留 / 离开前播报的站进入经过半径（ROUTE_THROUGH_PASS 1.0 m）就原地停下，不一定开到站点上；
// 所以「在这站播完」= 播的过程中狗没动，且停在站点 1.1 m 以内
const stayed = (h, p) => h.pose && h.end && far(h.pose, h.end) < .15 && far(h.end, p) < 1.1;
try {
  await until(() => c.state?.mapCurrent === map && c.pose?.frame === 'map' && c.audio?.avail, '地图、定位、喇叭', 20000);
  const cmd = async (op, a = {}) => { const r = await c.cmd(op, a); assert.equal(r.ok, true, `${op}: ${r.msg}`); return r; };
  await until(() => c.routes, '任务列表');
  for (const r of c.routes.filter(r => /^(多段-|等待-|格式$|等待格式$)/.test(r.name))) await c.cmd('route_delete', { route_id:r.id });   // 上次没跑完留下的
  for (const x of c.audio.items) await c.cmd('audio_delete', { clip:x.id });
  const up = async (name, sec) => { const r = await (await fetch(`${BASE}/api/audio?name=${encodeURIComponent(name)}`, { method:'POST', headers:{ 'X-Go2w':'1' }, body:wav(sec) })).json(); assert.ok(r.ok, r.msg); return r.item.id; };
  const A = await up('到达点位', 1), B = await up('开始巡逻', 1.2), C = await up('巡逻结束', 1), L = await up('长讲解', 8);
  const save = async (name, pts, opts = {}) => (await cmd('route_save', { map, name, points:pts, opts:{ loop:false, loops:1, vmax:.45, wmax:1.2, through:true, ...opts } })).data;
  const ended = (name, ms = 60000) => until(() => c.task?.name === name && c.task.status !== 'active', `${name} 结束`, ms);
  const quiet = () => until(() => !c.audio.cur, '播完', 30000);
  const go = async (r, from = { ...points[2], th:Math.PI / 2 }) => { await cmd('initial_pose', from); await cmd('arm', { on:true }); await quiet(); c.heard.length = 0; c.tasks.length = 0; c.logs = []; await cmd('route_start', { route_id:r.id }); };
  const names = () => c.heard.map(h => c.audio.items.find(x => x.id === h.clip)?.name);
  const made = [];

  // 校验
  for (const [key, bad] of [['audio', [A, 'xx']], ['audioLeave', ['ABCDEF012345']], ['audio', [A, A, A, A, A, A]], ['audioLeave', 7]]){
    const r = await c.cmd('route_save', { map, name:'坏', points:[first, { ...points[1], [key]:bad }] });
    assert.equal(r.ok, false, `拒绝 ${key}=${JSON.stringify(bad)}`);
  }
  let r0 = await c.cmd('route_save', { map, name:'不存在', points:[first, { ...points[1], audioLeave:'0123456789ab' }] });
  assert.ok(!r0.ok && /第 2 站/.test(r0.msg), r0.msg);
  const z = await save('格式', [{ ...first, audio:[A] }, { ...points[1], audio:[A, B], audioLeave:[] }, { ...points[2], audioLeave:[C] }]);
  assert.equal(z.points[0].audio, A, '一段存字符串'); assert.deepEqual(z.points[1].audio, [A, B], '几段存列表');
  assert.ok(!('audioLeave' in z.points[1]), '空列表不存'); assert.equal(z.points[2].audioLeave, C);
  await cmd('route_delete', { route_id:z.id });
  console.log('PASS 校验：列表里有坏 id / 超过 5 段 / 音频不存在拒绝；一段存字符串、几段存列表');

  for (const bad of [{ id:A, wait:-1 }, { id:A, wait:601 }, { id:A, wait:'3' }, { id:A, wait:true }, { wait:3 }]){
    const r = await c.cmd('route_save', { map, name:'坏', points:[first, { ...points[1], audio:[bad] }] });
    assert.equal(r.ok, false, `拒绝 ${JSON.stringify(bad)}`);
  }
  const w = await save('等待格式', [{ ...first, audio:[{ id:A, wait:0 }] }, { ...points[1], audio:[A, { id:B, wait:2.6 }] }, { ...points[2], audioLeave:{ id:C, wait:4 } }]);
  assert.equal(w.points[0].audio, A, 'wait 0 = 字符串'); assert.deepEqual(w.points[1].audio, [A, { id:B, wait:3 }], '秒数取整');
  assert.deepEqual(w.points[2].audioLeave, [{ id:C, wait:4 }], '一段带等待存列表');
  await cmd('route_delete', { route_id:w.id });
  console.log('PASS 等待秒数：负数 / 超 600 / 非数字拒绝，0 不存，取整');

  // 每段前的等待：到站「到达点位」→ 等 3 s →「开始巡逻」；停留 1 s 后先等 2 s 再「巡逻结束」，播完才走
  const wp = [{ ...points[0] }, { ...points[1], audio:[A, { id:B, wait:3 }], dwell:1, audioLeave:[{ id:C, wait:2 }] }, { ...points[2] }];
  for (const through of [true, false]){
    const r = await save(`等待-${through ? '连续' : '逐站'}`, wp, { through }); made.push(r);
    await go(r); await until(() => c.audio.cur?.phase === 'wait', '等待中', 40000);
    assert.equal(c.audio.cur.clip, B); assert.equal(c.audio.cur.wait, 3);
    await ended(r.name, 90000); assert.equal(c.task.status, 'succeeded', c.task.message); await quiet();
    assert.deepEqual(names(), ['到达点位', '开始巡逻', '巡逻结束']); assert.ok(c.heard.every(h => h.ok), '每段都播完');
    assert.deepEqual(c.heard.map(h => h.waited), [0, 3, 2]);
    const gap = c.heard[1].t0 - c.heard[0].t1;
    assert.ok(gap < 500, `「开始巡逻」的等待紧接着上一段开始（${gap} ms）`);
    assert.ok(c.heard[1].t1 - c.heard[1].t0 >= 4100, `等 3 s + 播 1.2 s（实际 ${c.heard[1].t1 - c.heard[1].t0} ms）`);
    for (const h of c.heard) assert.ok(stayed(h, points[1]), `${h.who} 播完时还在第 2 站`);
    console.log(`PASS ${through ? '连续通过' : '一站一站走'}：段前等待生效（等 3 s / 2 s），都在第 2 站播完才走`);
  }
  const wk = await save('等待-取消', [{ ...points[0] }, { ...points[1], audioLeave:{ id:C, wait:30 } }, points[2]]); made.push(wk);
  await go(wk); await until(() => c.audio.cur?.phase === 'wait' && c.task?.phase === 'speak', '离开前等待', 40000);
  await cmd('cancel'); await until(() => !c.audio.cur, '取消后撤掉等待', 1500); await sleep(500);
  assert.equal(c.task.status, 'canceled'); assert.ok(!c.audio.cur);
  console.log('PASS 等待中取消：马上撤掉，不再播');

  // 用户场景：到站「到达点位」+「开始巡逻」，停留，离开前「巡逻结束」播完再走
  const pts = [{ ...points[0] }, { ...points[1], audio:[A, B], dwell:3, audioLeave:C }, { ...points[2] }];
  for (const through of [true, false]){
    const r = await save(`多段-${through ? '连续' : '逐站'}`, pts, { through }); made.push(r);
    await go(r); await until(() => c.task?.phase === 'dwell', '停留', 40000);
    assert.equal(c.task.pointAudio, '到达点位、开始巡逻'); assert.equal(c.task.pointAudioLeave, '巡逻结束');
    await until(() => c.task?.phase === 'speak', '离开前播报', 10000);
    await ended(r.name); assert.equal(c.task.status, 'succeeded', c.task.message); await quiet();
    assert.deepEqual(names(), ['到达点位', '开始巡逻', '巡逻结束']);
    assert.ok(c.heard.every(h => h.ok), '每段都播完');
    for (const h of c.heard) assert.ok(stayed(h, pts[1]), `${h.who} 在第 2 站播、播完时还在第 2 站`);
    assert.match(c.heard[2].who, /离开前/);
    console.log(`PASS ${through ? '连续通过' : '一站一站走'}：到站按顺序播 2 段，停留后播「巡逻结束」，播完才走`);
  }

  // 没设停留只设离开前：连续通过也要在这站停下播完
  const nd = await save('多段-不停留', [{ ...points[0] }, { ...points[1], audio:A, audioLeave:[C, B] }, { ...points[2] }]); made.push(nd);
  await go(nd); await ended(nd.name); assert.equal(c.task.status, 'succeeded', c.task.message); await quiet();
  assert.deepEqual(names(), ['到达点位', '巡逻结束', '开始巡逻']);
  for (const h of c.heard) assert.ok(stayed(h, points[1]), `${h.who} 播完时还在第 2 站`);
  console.log('PASS 不停留只配离开前播报：停下，到站 + 离开前 3 段播完再走');

  // 到站的比停留长：离开前的等它播完，不打断
  const lg = await save('多段-长', [{ ...points[0] }, { ...points[1], audio:L, dwell:2, audioLeave:C }, { ...points[2] }]); made.push(lg);
  await go(lg); await ended(lg.name); assert.equal(c.task.status, 'succeeded', c.task.message); await quiet();
  assert.deepEqual(names(), ['长讲解', '巡逻结束']); assert.ok(c.heard.every(h => h.ok), '长讲解没被打断');
  assert.ok(stayed(c.heard[1], points[1]), '都播完才走');
  console.log('PASS 到站的音频比停留长：等它播完再播离开前的，再走');

  // 11-26 用户场景：只配到站播 + 段间延时，停留比「播报 + 延时」短：等播完再走，不边走边播
  const gd = [{ ...points[0] }, { ...points[1], audio:[A, { id:B, wait:3 }], dwell:1 }, { ...points[2] }];
  for (const through of [true, false]){
    const r = await save(`等待-停留短-${through ? '连续' : '逐站'}`, gd, { through }); made.push(r);
    await go(r); await until(() => c.task?.phase === 'speak', '停留结束后等播完', 40000);
    await ended(r.name, 90000); assert.equal(c.task.status, 'succeeded', c.task.message); await quiet();
    assert.deepEqual(names(), ['到达点位', '开始巡逻']); assert.ok(c.heard.every(h => h.ok), '每段都播完');
    assert.deepEqual(c.heard.map(h => h.waited), [0, 3]);
    for (const h of c.heard) assert.ok(stayed(h, points[1]), `${h.who} 播完时还在第 2 站`);
    assert.ok(c.logs.some(x => /到站播报还没播完，播完再走/.test(x)), '日志说明多等了');
    console.log(`PASS ${through ? '连续通过' : '一站一站走'}：停留 1 s 比播报 + 延时短，等播完才走`);
  }
  // 停留够长：不多等（不进入「播报中」）
  const gl = await save('等待-停留够', [{ ...points[0] }, { ...points[1], audio:[A, { id:B, wait:1 }], dwell:10 }, { ...points[2] }]); made.push(gl);
  await go(gl); await ended(gl.name, 90000); assert.equal(c.task.status, 'succeeded', c.task.message);
  assert.ok(!c.tasks.some(t => t.phase === 'speak'), '停留够长时不进入播报中');
  assert.deepEqual(names(), ['到达点位', '开始巡逻']);
  console.log('PASS 停留比播报 + 延时长：照常停够就走');

  // 终点配离开前播报：到终点播完，任务成功
  const tm = await save('多段-终点', [{ ...points[0] }, points[1], { ...points[2], audio:[A, B], audioLeave:C }]); made.push(tm);
  await go(tm, { ...first, th:-Math.PI / 2 }); await ended(tm.name); assert.equal(c.task.status, 'succeeded', c.task.message); await quiet();
  assert.deepEqual(names(), ['到达点位', '开始巡逻', '巡逻结束']); console.log('PASS 终点：到站 2 段 + 离开前 1 段都播');

  // 离开前播报中取消：马上停
  const k = await save('多段-取消', [{ ...points[0] }, { ...points[1], audioLeave:L }, points[2]]); made.push(k);
  await go(k); await until(() => c.task?.phase === 'speak' && c.audio.cur?.clip === L, '离开前长讲解', 40000);
  const at = { ...c.pose };
  await cmd('cancel'); await until(() => !c.audio.cur, '取消后停播', 1500); await sleep(800);
  assert.equal(c.task.status, 'canceled'); assert.ok(far(c.pose, at) < .1, '取消后没走');
  console.log('PASS 离开前播报中取消：停播、不走');

  const d = await c.cmd('audio_delete', { clip:C }); assert.ok(!d.ok && /第 2 站/.test(d.msg), d.msg);
  console.log('PASS 只在「离开前」用的音频也不能删：' + d.msg);

  for (const x of made) await c.cmd('route_delete', { route_id:x.id });
  for (const x of [A, B, C, L]) await cmd('audio_delete', { clip:x }); console.log('PASS 清理');
} finally { c.close(); }
