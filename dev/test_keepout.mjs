// 禁行区（v1.1.8）协议测试：先运行 dev/run.sh，再运行 node dev/test_keepout.mjs [ws://127.0.0.1:8080/ws]
// 只对本地假 ROS 运行：会开导航档位、写 data/maps/<图>.keepout.json 和 keepout/ 掩膜，最后清空禁行区
import assert from 'node:assert/strict';
import { existsSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { homedir } from 'node:os';
import { setTimeout as sleep } from 'node:timers/promises';

const url = process.argv[2] || 'ws://127.0.0.1:8080/ws';
assert.match(url, /^ws:\/\/(127\.0\.0\.1|localhost):\d+\/ws$/);
const DATA = process.env.GO2W_DATA || `${homedir()}/.cache/go2w_web_data`;
const NO_KEEPOUT = process.env.GO2W_FAKE_NO_KEEPOUT || '/tmp/go2w_fake_no_keepout';

async function client(){
  const ws = new WebSocket(url), acks = new Map(), c = { ws, keep: null, state: null };
  let id = 0;
  ws.addEventListener('message', ({ data }) => {
    if (typeof data !== 'string') return;
    const m = JSON.parse(data);
    if (m.t === 'ack') acks.get(m.id)?.(m);
    if (m.t === 'keepout') c.keep = m.items;
    if (m.t === 'state') c.state = m;
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res, { once:true }); ws.addEventListener('error', rej, { once:true }); });
  const timer = setInterval(() => ws.readyState === 1 && ws.send('{"t":"ping","ts":0}'), 350);
  c.close = () => { clearInterval(timer); ws.close(); };
  c.cmd = (op, args = {}, ms = 30000) => new Promise((r, j) => { const k = ++id; const t = setTimeout(() => j(new Error('超时 ' + op)), ms);
    acks.set(k, m => { clearTimeout(t); r(m); }); ws.send(JSON.stringify({ t:'cmd', id:k, op, ...args })); });
  return c;
}
async function until(fn, what, ms = 15000){
  const end = Date.now() + ms;
  while (!fn()){ assert.ok(Date.now() < end, `超时：${what}`); await sleep(50); }
}

rmSync(NO_KEEPOUT, { force:true });
const a = await client(), b = await client();
let map;
try {
  await until(() => a.keep && b.keep && a.state?.maps?.length, '连上就收到禁行区表和地图列表');
  assert.ok(a.state.keepout && 'ready' in a.state.keepout, 'state 里有 keepout 状态');
  if (a.state.profiles.navigation !== 'run'){
    const r = await a.cmd('set_mode', { mode:'navigation', map: a.state.mapChoice || a.state.maps[0].name }, 60000);
    assert.equal(r.ok, true, r.msg);
  }
  await until(() => a.state.profiles.navigation === 'run' && a.state.mapLive, '导航档位运行、地图来自 map_server', 60000);
  map = a.state.mapCurrent;
  await until(() => a.state.keepout.ready && a.state.keepout.live === map, '导航起来后自动加载当前图的掩膜');
  console.log(`PASS 导航启动后自动加载 ${map} 的禁行掩膜`);

  for (const bad of [[{ kind:'line', pts:[[0, 0]] }], [{ kind:'x', pts:[[0, 0], [1, 1]] }], 'nope'])
    assert.equal((await a.cmd('keepout_save', { map, shapes:bad })).ok, false, `拒绝 ${JSON.stringify(bad)}`);
  assert.equal((await a.cmd('keepout_save', { map:'../etc', shapes:[] })).ok, false, '拒绝坏地图名');
  assert.equal((await a.cmd('keepout_save', { map:'no_such_map', shapes:[] })).ok, false, '拒绝不存在的地图');
  console.log('PASS 坏形状、坏地图名被拒绝');

  // 一条横在机器人前面 2 m 的禁行线 + 一个区域
  const line = { kind:'line', pts:[[2, -1], [2, 1]], name:'路牙' }, zone = { kind:'zone', pts:[[4, 4], [5, 4], [5, 5], [4, 5]] };
  let r = await a.cmd('keepout_save', { map, shapes:[line, zone] });
  assert.equal(r.ok, true, r.msg);
  assert.match(r.msg, /导航已生效（2 个）/, r.msg);
  await until(() => b.keep[map]?.length === 2 && b.keep[map][0].name === '路牙', '另一个网页收到禁行区');
  assert.ok(existsSync(`${DATA}/maps/${map}.keepout.json`), '写了 <图>.keepout.json');
  assert.match(readFileSync(`${DATA}/maps/keepout/${map}.yaml`, 'utf8'), /mode: raw/, '掩膜 yaml 是 raw 模式');
  console.log('PASS 保存后热加载进导航、同步到其他网页、文件写对');

  r = await a.cmd('goal', { x:2.1, y:0, th:0 });
  assert.equal(r.ok, false); assert.match(r.msg, /路牙/, r.msg);
  r = await a.cmd('goal', { x:4.5, y:4.5, th:0 });
  assert.equal(r.ok, false); assert.match(r.msg, /第 2 个禁行区域/, r.msg);
  const saved = await a.cmd('route_save', { map, name:'禁行测试', points:[{ x:0, y:0, th:0 }, { x:2.05, y:0.3, th:0 }] });
  assert.equal(saved.ok, true, saved.msg);
  r = await a.cmd('route_start', { route_id:saved.data.id });
  assert.equal(r.ok, false); assert.match(r.msg, /第 2 站.*路牙/, r.msg);
  await a.cmd('route_delete', { route_id:saved.data.id });
  console.log('PASS 目标点 / 任务站点落在禁行范围里出发前就拒绝，并说出是哪条');

  // 旧导航镜像：没有 filter_mask_server
  writeFileSync(NO_KEEPOUT, '');
  await until(() => a.state.keepout.ready === false && a.state.keepout.live === null, '服务消失');
  r = await a.cmd('keepout_save', { map, shapes:[line] });
  assert.equal(r.ok, true, r.msg); assert.match(r.msg, /更新导航镜像/, r.msg);
  rmSync(NO_KEEPOUT);
  await until(() => a.state.keepout.ready && a.state.keepout.live === map, '服务回来后自动补加载');
  console.log('PASS 旧导航镜像照样能存并提示要更新；服务回来自动补加载');

  r = await a.cmd('keepout_save', { map, shapes:[] });
  assert.equal(r.ok, true, r.msg); assert.match(r.msg, /清空/, r.msg);
  await until(() => !(map in b.keep), '清空后其他网页的表里没有这张图');
  assert.ok(!existsSync(`${DATA}/maps/${map}.keepout.json`), '清空删 json');
  assert.equal((await a.cmd('goal', { x:2.1, y:0, th:0 })).msg.includes('禁行'), false, '清空后不再拦目标');
  await a.cmd('cancel', {});
  console.log('PASS 清空禁行区');
  console.log('\n全部通过');
} finally {
  rmSync(NO_KEEPOUT, { force:true });
  if (map) await a.cmd('keepout_save', { map, shapes:[] }).catch(() => {});
  a.close(); b.close();
}
