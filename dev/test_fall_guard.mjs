// 摔倒 / 里程计发散保护测试（11-19：.30 建图时摔倒，FAST-LIO 发散，自动保存把好图覆盖成了坏图）。
// 只能对本地假 ROS 运行（会切档位）。用临时数据目录，周期调短、假地图一直在长：
//   D=$(mktemp -d); mkdir -p $D/maps; cp ../data/maps/* $D/maps/
//   GO2W_DATA=$D GO2W_AUTOSAVE_SECONDS=4 GO2W_FAKE_MAP_GROW=1 dev/run.sh
//   GO2W_DATA=$D node dev/test_fall_guard.mjs [ws://127.0.0.1:8080/ws]
import { existsSync, readFileSync, statSync, writeFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const DATA = process.env.GO2W_DATA;
if (!DATA) { console.error('要设置 GO2W_DATA（和 run.sh 用同一个数据目录）'); process.exit(2); }
const MAPS = join(DATA, 'maps');
const FALL = process.env.GO2W_FAKE_FALL || '/tmp/go2w_fake_fall';
const WILD = process.env.GO2W_FAKE_ODOM_WILD || '/tmp/go2w_fake_odom_wild';
const sleep = ms => new Promise(r => setTimeout(r, ms));
for (const f of [FALL, WILD]) { try { unlinkSync(f); } catch {} }

const ws = new WebSocket(URL_); ws.binaryType = 'arraybuffer';
let state = null; const logs = []; const acks = new Map(); const incs = []; let seq = 0;
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
  if (m.t === 'state') state = m;
  if (m.t === 'logs') for (const it of m.items) logs.push(it[2]);
  if (m.t === 'incident') incs.push(m.item);
  if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
const timer = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 500);
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(100); } throw new Error('超时：' + what); };
const results = []; const ok = (name, cond, extra = '') => results.push(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  · ' + extra : ''}`);
const logHas = re => logs.some(t => re.test(t));
const ctrl = () => state.ctrl?.state;
const mtime = f => existsSync(f) ? statSync(f).mtimeMs : 0;

try {
  await new Promise(r => ws.onopen = r);
  await until(() => state, 8000, '首个状态');
  if (state.profiles.mapping !== 'stop') { await cmd('set_mode', { mode: null }); await until(() => state.profiles.mapping === 'stop' && !state.busy, 30000, '先停'); }
  if (ctrl() !== 'Velocity') { await cmd('stand_up'); await until(() => ctrl() === 'Velocity', 15000, '站起'); }
  await cmd('set_mode', { mode: 'mapping' });
  await until(() => state.profiles.mapping === 'run' && !state.busy, 30000, '建图起来');
  await until(() => state.autosave?.count >= 2, 30000, '自动保存两次');
  const name = state.autosave.name, pgm = join(MAPS, `${name}.pgm`);

  // 1. 摔倒：运控意外掉到 Passive → 冻结、记异常
  writeFileSync(FALL, '1');
  await until(() => state.autosave?.frozen, 5000, '摔倒后冻结');
  const F = state.autosave.frozen;
  ok('摔倒 → 建图冻结', /意外掉到 Passive/.test(F.reason) && !F.wild, F.reason);
  ok('冻结时关上了扫描闸门', F.gate === true);
  ok('冻结信息里写着保留哪一份', F.kept === name && F.kept_wall > 0, F.kept);
  ok('日志说明了冻结和建议', logHas(/建图已冻结：.*自动保存已暂停.*续建/));
  await until(() => incs.some(i => i.type === 'fall'), 5000, '意外趴下异常');
  ok('记了一条「意外趴下」异常', incs.some(i => i.type === 'fall' && i.label === '意外趴下'));

  // 2. 冻结期间自动保存不再覆盖
  const c0 = state.autosave.count, m0 = mtime(pgm);
  await sleep(10000);
  ok('冻结 10 s：自动保存次数没变', state.autosave.count === c0, `${c0} → ${state.autosave.count}`);
  ok('冻结 10 s：自动保存文件没被覆盖', mtime(pgm) === m0);

  // 3. 继续建图：趴着时拒绝，站起来后可以
  let r = await cmd('map_resume');
  ok('趴着时「继续建图」被拒绝', !r.ok && /站起来/.test(r.msg), r.msg);
  await cmd('stand_up'); await until(() => ctrl() === 'Velocity', 15000, '再站起');
  r = await cmd('map_resume');
  ok('站起来后「继续建图」成功', r.ok, r.msg);
  await until(() => !state.autosave.frozen, 3000, '解除冻结');
  await until(() => state.autosave.count > c0, 15000, '自动保存恢复');
  ok('解除冻结后自动保存恢复', state.autosave.count > c0);

  // 4. 网页让狗趴下：不算摔倒
  const nFall = incs.filter(i => i.type === 'fall').length;
  await cmd('lie_down'); await until(() => ctrl() === 'Passive', 15000, '趴下');
  await sleep(1500);
  ok('网页让狗趴下：不冻结、不记摔倒', !state.autosave.frozen && incs.filter(i => i.type === 'fall').length === nFall);
  await cmd('stand_up'); await until(() => ctrl() === 'Velocity', 15000, '站起 2');

  // 5. 里程计发散：冻结且不能继续
  writeFileSync(WILD, '1');
  await until(() => state.autosave?.frozen?.wild, 5000, '里程计异常冻结');
  ok('里程计发散 → 冻结（wild）', /里程计/.test(state.autosave.frozen.reason), state.autosave.frozen.reason);
  await until(() => incs.some(i => i.type === 'odom_wild'), 5000, '里程计异常记录');
  ok('记了一条「里程计异常」', incs.some(i => i.type === 'odom_wild' && i.label === '里程计异常'));
  unlinkSync(WILD);
  r = await cmd('map_resume');
  ok('里程计飘过后「继续建图」被拒绝', !r.ok && /飘过/.test(r.msg), r.msg);

  // 6. 冻结时可以手动另存（带提醒）
  r = await cmd('save_map', { name: 'fall_guard_manual' });
  ok('冻结时手动保存可以', r.ok && existsSync(join(MAPS, 'fall_guard_manual.yaml')), r.msg);
  ok('手动保存时提醒了冻结时刻', logHas(/在建图冻结状态下保存 fall_guard_manual.*里程计飘过/));

  // 7. 停止建图：不再「停止前自动保存」；重开后冻结和里程计异常都清掉
  const m1 = mtime(pgm);
  await cmd('set_mode', { mode: null });
  await until(() => state.profiles.mapping === 'stop' && !state.busy, 30000, '建图停下');
  ok('冻结时停止建图：没有停止前自动保存', mtime(pgm) === m1 && logHas(/建图冻结中，停止前不再自动保存：保留/));
  await until(() => incs.filter(i => i.type === 'odom_wild').pop()?.bag?.state === 'saved', 30000, '里程计异常的包');
  ok('异常后马上停止建图：回溯包先存下来了', logHas(/先把异常回溯包存掉/), incs.filter(i => i.type === 'odom_wild').pop()?.bag?.name);
  await cmd('set_mode', { mode: 'mapping' });
  await until(() => state.profiles.mapping === 'run' && !state.busy, 30000, '建图重开');
  await until(() => state.autosave?.count >= 1, 20000, '新一次自动保存');
  ok('重开建图：不再冻结，自动保存正常', !state.autosave.frozen && state.autosave.name !== name);
  await cmd('set_mode', { mode: null });
  await until(() => state.profiles.mapping === 'stop' && !state.busy, 30000, '收尾停建图');
} catch (e) {
  results.push('FAIL  ' + e.message);
} finally {
  for (const f of [FALL, WILD]) { try { unlinkSync(f); } catch {} }
  clearInterval(timer); ws.close();
  console.log(results.join('\n'));
  const bad = results.filter(r => r.startsWith('FAIL')).length;
  console.log(bad ? `\n${bad} 项失败` : `\n全部通过（${results.length} 项）`);
  process.exit(bad ? 1 : 0);
}
