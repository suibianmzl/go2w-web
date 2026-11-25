// 地图显示名（改名，11-23）协议测试：先运行 dev/run.sh，再运行 node dev/test_map_labels.mjs [ws://127.0.0.1:8080/ws]
// 只改 data/map_labels.json，不动地图文件。只对本地假 ROS 运行
import assert from 'node:assert/strict';
import { setTimeout as sleep } from 'node:timers/promises';

const url = process.argv[2] || 'ws://127.0.0.1:8080/ws';
assert.match(url, /^ws:\/\/(127\.0\.0\.1|localhost):\d+\/ws$/);

async function client(){
  const ws = new WebSocket(url), acks = new Map(), c = { ws, labels: null, state: null };
  let id = 0;
  ws.addEventListener('message', ({ data }) => {
    if (typeof data !== 'string') return;
    const m = JSON.parse(data);
    if (m.t === 'ack') acks.get(m.id)?.(m);
    if (m.t === 'map_labels') c.labels = m.items;
    if (m.t === 'state') c.state = m;
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res, { once:true }); ws.addEventListener('error', rej, { once:true }); });
  const timer = setInterval(() => ws.readyState === 1 && ws.send('{"t":"ping","ts":0}'), 400);
  c.close = () => { clearInterval(timer); ws.close(); };
  c.cmd = (op, args = {}) => new Promise(r => { const k = ++id; acks.set(k, r); ws.send(JSON.stringify({ t:'cmd', id:k, op, ...args })); });
  return c;
}
async function until(fn, what, ms = 8000){
  const end = Date.now() + ms;
  while (!fn()){ assert.ok(Date.now() < end, `超时：${what}`); await sleep(50); }
}

const a = await client(), b = await client();
try {
  await until(() => a.labels && b.labels && a.state?.maps?.length >= 2, '连上就收到显示名表和地图列表');
  const before = { ...a.labels };
  const [M1, M2] = a.state.maps.map(m => m.name);

  let r = await a.cmd('map_label', { name:M1, label:'  一楼   实验室 ' });
  assert.equal(r.ok, true, r.msg);
  await until(() => b.labels[M1] === '一楼 实验室', '另一个网页收到新显示名（空白已整理）');
  console.log('PASS 中文显示名保存并同步到其他网页');

  const c = await client();
  await until(() => c.labels, 'c 收到显示名表');
  assert.equal(c.labels[M1], '一楼 实验室');
  c.close();
  console.log('PASS 新打开的网页拿到已保存的显示名');

  r = await a.cmd('map_label', { name:M2, label:'一楼 实验室' });
  assert.equal(r.ok, false, '重名应拒绝'); assert.match(r.msg, /已经是/);
  r = await a.cmd('map_label', { name:M2, label:M1 });
  assert.equal(r.ok, false, '用别的地图文件名应拒绝');
  for (const bad of [{ name:M1, label:'x'.repeat(33) }, { name:M1, label:'<b>' }, { name:M1, label:'a/b' }, { name:M1, label:'a\u0001' },
                     { name:M1, label:3 }, { name:'../x', label:'y' }, { name:'nope_map_x', label:'y' }, { label:'y' }]){
    r = await a.cmd('map_label', bad);
    assert.equal(r.ok, false, `应拒绝 ${JSON.stringify(bad)}`);
  }
  assert.equal(a.labels[M1], '一楼 实验室');
  console.log('PASS 拒绝重名、超长、特殊字符、不存在的地图');

  r = await a.cmd('map_label', { name:M1, label:M1 });
  assert.equal(r.ok, true, r.msg);
  await until(() => !(M1 in a.labels), '改回文件名 = 删掉显示名');
  r = await a.cmd('map_label', { name:M1, label:'临时' });
  r = await a.cmd('map_label', { name:M1, label:'' });
  await until(() => !(M1 in a.labels), '留空 = 删掉显示名');
  console.log('PASS 留空或填文件名恢复显示文件名');

  // 删除地图时显示名一起删（存一张图再删）
  const prof = k => a.state.profiles[k];
  r = await a.cmd('set_mode', { mode:'mapping' });
  assert.equal(r.ok, true, r.msg);
  await until(() => prof('mapping') === 'run' && !a.state.busy, '建图起来', 15000);
  const name = `label_test_${Date.now() % 100000}`;
  r = await a.cmd('save_map', { name });
  assert.equal(r.ok, true, r.msg);
  await until(() => a.state.maps.some(m => m.name === name), '新图出现在列表');
  r = await a.cmd('map_label', { name, label:'要删的图' });
  assert.equal(r.ok, true, r.msg);
  r = await a.cmd('set_mode', { mode:null });
  assert.equal(r.ok, true, r.msg);
  await until(() => prof('mapping') === 'stop' && !a.state.busy, '停下建图', 15000);
  r = await a.cmd('map_delete', { names:[name] });
  assert.equal(r.ok, true, r.msg);
  await until(() => !(name in a.labels), '删图后显示名也删了');
  console.log('PASS 删除地图时显示名一起删');

  for (const [k, v] of Object.entries(before)) await a.cmd('map_label', { name:k, label:v });
} finally {
  a.close(); b.close();
}
