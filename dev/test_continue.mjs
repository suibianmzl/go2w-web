// 地图续建协议测试：停车点只在可信坐标系里记、手动起点的校验、自检能拦住给错的起点。
// 只能对本地假 ROS 运行（会切档位）。准备：数据目录里要有带 .posegraph/.data 的 lab_20261116
// （假 slam_toolbox 只查文件在不在，空文件就行），假机器人站在图里的空地上：
//   GO2W_FAKE_START=11.67,0.93,0 GO2W_DATA=<数据> dev/run.sh <数据>/maps/lab_20261116.yaml   （地图要作为参数传，run.sh 会覆盖 GO2W_FAKE_MAP）
//   node dev/test_continue.mjs [ws://127.0.0.1:8080/ws]
import { writeFileSync } from 'node:fs';
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
// 假机器人瞬移（真实坐标）：建图档没有导航，用 go2w_fake_sim 的 GO2W_FAKE_MOVE 文件
const FAR = [11.42, -2.58, 1.0];   // 基准图里离起点约 3.5 m 的空地
const moveTo = async p => { writeFileSync(process.env.GO2W_FAKE_MOVE || '/tmp/go2w_fake_move', p.join(',')); await new Promise(r => setTimeout(r, 2500)); };
const BASE = 'lab_20261116', TRUE = [11.67, 0.93, 0];
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
  await c.until(() => prof(m) === 'run' && !c.state.busy, 20000, `${m} 起来`); await sleep(800);
}
try {
  // 1. 导航档：加载的是基准图且定好位才能记停车点
  if (prof('navigation') !== 'run') await mode('navigation');
  await c.until(() => c.state.mapCurrent === BASE, 8000, '读到当前地图');
  let r = await c.cmd('cont_parking', { base: 'lab_20261021' });
  ok('导航档：不是当前地图的停车点拒绝', !r.ok, r.msg);
  await c.cmd('initial_pose', { x: TRUE[0], y: TRUE[1], th: TRUE[2] });
  await c.until(() => (c.state.loc?.match ?? 0) >= 70, 15000, '匹配率上来');
  r = await c.cmd('cont_parking', { base: BASE });
  ok('导航档：定好位后记停车点', r.ok, r.msg);
  r = await c.cmd('cont_parking', { base: BASE, clear: true }); ok('删除停车点', r.ok, r.msg);

  // 2. 建图档、还没续建：坐标系是新建的，必须拒绝（这就是要修的坑）
  await mode('mapping');
  r = await c.cmd('cont_parking', { base: BASE });
  ok('建图档未续建：拒绝记停车点', !r.ok && /新建/.test(r.msg), r.msg);

  // 3. 手动起点的输入校验
  for (const [why, pose] of [['少一个数', [1, 2]], ['字符串', ['1', 2, 0]], ['布尔', [true, 2, 0]]]) {
    r = await c.cmd('cont_start', { base: BASE, name: 'lab_c_bad', pose, source: 'manual' });
    ok(`手动起点拒绝：${why}`, !r.ok, r.msg);
  }
  r = await c.cmd('cont_start', { base: BASE, name: 'lab_c_bad', pose: [500, 500, 0], source: 'manual' });
  ok('手动起点在基准图外：拒绝', !r.ok && /覆盖范围/.test(r.msg), r.msg);
  r = await c.cmd('cont_start', { base: BASE, name: 'lab_c_bad', pose: TRUE, source: 'bogus' });
  ok('未知起点来源：拒绝', !r.ok, r.msg);

  // 4. 手动给错的起点（朝向偏 13°，超过自检门限 8°；平移偏差的分辨率是 0.05 m，卡门限测不稳）：
  //    能开始，但自检必须拦下（开到别处再量也一样拦下，并换算出正确起点），且停车点不能记
  r = await c.cmd('cont_start', { base: BASE, name: 'lab_c_off', pose: [TRUE[0], TRUE[1], TRUE[2] + 13 * Math.PI / 180], source: 'manual' });
  ok('手动起点（朝向偏 13°）：开始', r.ok && r.data?.source === 'manual' && !!r.data?.warn, r.msg);
  r = await c.cmd('cont_parking', { base: BASE });
  ok('续建未自检：拒绝记停车点', !r.ok && /自检/.test(r.msg), r.msg);
  r = await c.cmd('cont_check');
  ok('自检拦下朝向偏 13° 的起点', !r.ok && /偏了|对不上/.test(r.msg), r.msg);
  await moveTo(FAR);
  r = await c.cmd('cont_check');
  const fx = r.data?.start_fix;
  ok('开到别处自检：照样拦下并换算出起点', !r.ok && /偏了|对不上/.test(r.msg) && !!fx
     && Math.hypot(fx[0] - TRUE[0], fx[1] - TRUE[1]) < 0.2 && Math.abs(fx[2] - TRUE[2]) < 3 * Math.PI / 180, `${r.msg} · start_fix ${JSON.stringify(fx)}`);
  await moveTo(TRUE);
  r = await c.cmd('cont_finish'); ok('自检没过：拒绝保存', !r.ok, r.msg);
  r = await c.cmd('cont_abort'); ok('放弃', r.ok, r.msg);

  // 5. 手动给对的起点：自检通过，之后可以记停车点
  r = await c.cmd('cont_start', { base: BASE, name: 'lab_c_ok', pose: TRUE, source: 'manual' });
  ok('手动起点（正确）：开始', r.ok && r.data?.source === 'manual', r.msg);
  r = await c.cmd('cont_check'); ok('自检通过', r.ok, r.msg);
  await moveTo(FAR);
  r = await c.cmd('cont_check'); ok('开走 3.5 m 后自检：不再要求回起点', r.ok && r.data?.at === 'current', r.msg);
  await moveTo(TRUE);
  r = await c.cmd('cont_parking', { base: 'lab_20261021' });
  ok('续建中：别的图的停车点拒绝', !r.ok, r.msg);
  r = await c.cmd('cont_parking', { base: BASE });
  ok('续建自检通过后：记停车点', r.ok, r.msg);
  r = await c.cmd('cont_parking', { base: BASE, clear: true });
  r = await c.cmd('cont_abort'); ok('放弃', r.ok, r.msg);

  // 6. 不给起点：没有停车点 → 自动定位（在磁盘上的基准图上做）
  r = await c.cmd('cont_start', { base: BASE, name: 'lab_c_auto' });
  ok('自动起点：开始（或失败时指向手动入口）', r.ok ? r.data?.source === 'globloc' : /手动起点/.test(r.msg), r.msg);
  if (r.ok) await c.cmd('cont_abort');
} catch (e) { results.push('FAIL  ' + e.message); }
console.log(results.join('\n'));
const fails = results.filter(x => x.startsWith('FAIL')).length;
console.log(fails ? `\n${fails} 项失败` : `\n全部通过（${results.length} 项）`);
clearInterval(c.timer); c.ws.close(); process.exit(fails ? 1 : 0);
