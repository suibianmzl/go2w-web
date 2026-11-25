// 存图顺手记停车点：建新图 → 保存 → 狗不挪 → 以新图为基准「从停车点开始」续建 → 收工另存也带停车点。
// 只能对本地假 ROS 运行（会切档位、写数据目录）：
//   GO2W_DATA=<数据> dev/run.sh <数据>/maps/lab_20261021.yaml
//   GO2W_DATA=<数据> node dev/test_save_parking.mjs [ws://127.0.0.1:8080/ws]
import { writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const DATA = process.env.GO2W_DATA || homedir() + '/.cache/go2w_web_data';
// 假机器人瞬移（真实坐标）：建图档没有导航，用 go2w_fake_sim 的 GO2W_FAKE_MOVE 文件。挑一块离原点 2 m 的空地，转 34°，
// 免得停车点碰巧等于原点、测不出坐标系问题
const SPOT = [2.03, 0.07, 0.6];
const moveTo = async p => { writeFileSync(process.env.GO2W_FAKE_MOVE || '/tmp/go2w_fake_move', p.join(',')); await new Promise(r => setTimeout(r, 2500)); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
function client(){
  const ws = new WebSocket(URL_); ws.binaryType = 'arraybuffer';
  const c = { ws, state: null, pose: null, acks: new Map(), seq: 0 };
  ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
    if (m.t === 'state') c.state = m; if (m.t === 'pose') c.pose = m;
    if (m.t === 'ack') { c.acks.get(m.id)?.(m); c.acks.delete(m.id); } };
  c.timer = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 500);
  c.cmd = (op, a = {}) => new Promise(r => { const id = ++c.seq; c.acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
  c.open = new Promise(r => ws.onopen = r);
  c.until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(100); } throw new Error('超时：' + what); };
  return c;
}
const results = []; const ok = (name, cond, extra = '') => results.push(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  · ' + extra : ''}`);
const c = client(); await c.open; await c.until(() => c.state && c.pose, 8000, '首个状态');
const prof = k => c.state.profiles[k];
async function mode(m){
  await c.cmd('set_mode', { mode: m });
  await c.until(() => prof(m) === 'run' && !c.state.busy, 20000, `${m} 起来`); await sleep(1500);
}
const tag = String(Date.now() % 1e6), A = 'lab_sp_' + tag, B = 'lab_sp_' + tag + '_c';
try {
  await mode('mapping');
  await moveTo(SPOT);
  let r = await c.cmd('save_map', { name: A });
  ok('全新建图保存：提示里写明已记停车点', r.ok && /已把狗现在的位置记成这张图的停车点/.test(r.msg), r.msg);
  // 真机 save_map.sh 会连 posegraph 一起存；假 slam_toolbox 只查文件在不在
  for (const ext of ['.posegraph', '.data']) writeFileSync(`${DATA}/maps/${A}${ext}`, '');
  r = await c.cmd('cont_preflight', { base: A });
  const park = r.data?.parking;
  ok('续建预检能看到新图的停车点', Array.isArray(park) && park.length === 3, JSON.stringify(r.data?.parking ?? r.msg));
  if (park) ok('停车点 = 存图时的位置（不是原点）', Math.hypot(park[0] - SPOT[0], park[1] - SPOT[1]) < 0.1 && Math.abs(park[2] - SPOT[2]) < 0.05,
               `park ${park.map(v => v.toFixed(2))}`);
  // 重开建图档 = 停下后再接着建：狗没挪
  await mode('navigation'); await mode('mapping');
  r = await c.cmd('cont_start', { base: A, name: B, source: 'parking' });
  ok('狗没挪：以新图为基准从停车点开始续建，自检通过', r.ok && r.data?.source === 'parking' && r.data?.check?.ok, r.msg);
  if (r.ok){
    r = await c.cmd('cont_finish');
    ok('续建收工另存：也顺手记停车点', r.ok && /停车点/.test(r.msg), r.msg);
    for (const ext of ['.posegraph', '.data']) writeFileSync(`${DATA}/maps/${B}${ext}`, '');
    r = await c.cmd('cont_preflight', { base: B });
    ok('续建出来的新图也有停车点', Array.isArray(r.data?.parking), JSON.stringify(r.data?.parking ?? r.msg));
  }
} catch (e) { results.push('FAIL  ' + e.message); }
for (const m of [A, B]) await c.cmd('cont_parking', { base: m, clear: true });
await c.cmd('map_delete', { names: [A, B] });
console.log(results.join('\n'));
const fails = results.filter(x => x.startsWith('FAIL')).length;
console.log(fails ? `\n${fails} 项失败` : `\n全部通过（${results.length} 项）`);
clearInterval(c.timer); c.ws.close(); process.exit(fails ? 1 : 0);
