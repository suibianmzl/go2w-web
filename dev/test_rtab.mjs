// RTAB 影子库测试（11-20）：建图自动同步录、存图记关联、暂停 / 继续、停档位先存库、
// 局部重扫（对位 → 续录 → 统计跨会话约束）、编辑图继承库、意外退出、关掉自动录后手动录。
// 只能对本地假 ROS 运行（会切档位）。用临时数据目录：
//   D=$(mktemp -d); mkdir -p $D/maps; cp ../go2w/data/maps/lab_20261021.* $D/maps/
//   GO2W_DATA=$D GO2W_AUTOSAVE_SECONDS=4 GO2W_FAKE_MAP_GROW=1 dev/run.sh
//   GO2W_DATA=$D node dev/test_rtab.mjs [ws://127.0.0.1:8080/ws]
import { existsSync, readFileSync, readdirSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const DATA = process.env.GO2W_DATA;
if (!DATA) { console.error('要设置 GO2W_DATA（和 run.sh 用同一个数据目录）'); process.exit(2); }
const MAPS = join(DATA, 'maps'), RT = join(DATA, 'rtabmap');
const CRASH = process.env.GO2W_FAKE_RTAB_CRASH || '/tmp/go2w_fake_rtab_crash';
const sleep = ms => new Promise(r => setTimeout(r, ms));
try { unlinkSync(CRASH); } catch {}

const ws = new WebSocket(URL_); ws.binaryType = 'arraybuffer';
let state = null; const logs = []; const acks = new Map(); let seq = 0;
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
  if (m.t === 'state') state = m;
  if (m.t === 'logs') for (const it of m.items) logs.push(it[2]);
  if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
const timer = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 500);
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(100); } throw new Error('超时：' + what); };
const results = []; const ok = (name, cond, extra = '') => results.push(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  · ' + extra : ''}`);
const logHas = re => logs.some(t => re.test(t));
const links = () => { try { return JSON.parse(readFileSync(join(RT, 'links.json'), 'utf8')); } catch { return {}; } };
const R = () => state.rtab || {};
const idle = () => !state.busy && !state.mapBusy;

try {
  await new Promise(r => ws.onopen = r);
  await until(() => state, 8000, '首个状态');
  ok('镜像有 RTAB：avail，默认建图自动录', R().avail === true && R().auto === true, JSON.stringify(R()));

  // 1. 建图档起来后自动开录
  await cmd('set_mode', { mode: 'mapping' });
  await until(() => state.profiles.mapping === 'run' && idle(), 30000, '建图起来');
  await until(() => R().cur?.phase === 'recording', 15000, '自动开录');
  const db = R().cur.db;
  ok('建图自动开录 rtab_月日_时分秒.db', R().cur.kind === 'map' && /^rtab_\d{4}_\d{6}\.db$/.test(db), db);
  ok('session.json 记下在录的会话', JSON.parse(readFileSync(join(RT, 'session.json'), 'utf8')).db === db);
  ok('日志写了库的位置', logHas(new RegExp(`开始同步录 RTAB 库 data/rtabmap/${db}`)));
  const dup = await cmd('rtab_start');
  ok('已经在录时不能再开一个', dup.ok === false && /正在建图同步录制/.test(dup.msg), dup.msg);

  // 2. 存图（手动 + 自动）都记关联
  const s1 = await cmd('save_map', { name: 'rtab_t1' });
  ok('手动保存成功', s1.ok, s1.msg);
  ok('手动保存的图关联到这个库', links().rtab_t1 === db, JSON.stringify(links()));
  // 模拟网页编辑出来的图（第 7 步用；地图列表在下面的自动保存后刷新）
  for (const ext of ['.yaml', '.pgm']) copyFileSync(join(MAPS, 'rtab_t1' + ext), join(MAPS, 'rtab_t1_ed' + ext));
  writeFileSync(join(MAPS, 'rtab_t1_ed.yaml'), readFileSync(join(MAPS, 'rtab_t1.yaml'), 'utf8').replace('rtab_t1.pgm', 'rtab_t1_ed.pgm'));
  writeFileSync(join(MAPS, 'rtab_t1_ed.meta.json'), JSON.stringify({ schema: 1, name: 'rtab_t1_ed', parent: 'rtab_t1', tool: 'web-edit' }));
  await until(() => state.autosave?.count >= 1, 20000, '自动保存');
  ok('自动保存的图也关联', links()[state.autosave.name] === db, state.autosave.name);

  // 3. 暂停 / 继续
  const p = await cmd('rtab_pause');
  await until(() => R().cur?.phase !== 'recording', 3000, '暂停状态');
  ok('暂停', p.ok && R().cur.phase === 'paused', p.msg);
  const r = await cmd('rtab_resume');
  await until(() => R().cur?.phase !== 'paused', 3000, '继续状态');
  ok('继续', r.ok && R().cur.phase === 'recording', r.msg);
  await sleep(2500);

  // 4. 停档位：先存 RTAB 库再停建图
  await cmd('set_mode', { mode: null });
  await until(() => state.profiles.mapping === 'stop' && idle(), 30000, '建图停下');
  const iSaved = logs.findIndex(t => /RTAB 库 rtab_.*已保存/.test(t)), iStop = logs.findIndex(t => /已停止建图/.test(t));
  ok('停建图前先存 RTAB 库', iSaved >= 0 && iSaved < iStop, `${iSaved} < ${iStop}`);
  ok('停完：没有在录，上一次结果 = 1 轮', !R().cur && R().last?.db === db && R().last.summary?.sessions.length === 1, JSON.stringify(R().last));
  ok('库文件在', existsSync(join(RT, db)));
  ok('session.json 清掉', !existsSync(join(RT, 'session.json')));

  // 5. 局部重扫：没开定位时拒绝
  const no = await cmd('rtab_continue', { map: 'rtab_t1' });
  ok('没开定位 / 导航时拒绝局部重扫', no.ok === false && /先用 rtab_t1 启动导航/.test(no.msg), no.msg);

  // 6. 用 rtab_t1 开定位 → 局部重扫
  await cmd('set_mode', { mode: 'localization', map: 'rtab_t1' });
  await until(() => state.profiles.localization === 'run' && idle() && state.mapCurrent === 'rtab_t1', 30000, '定位起来');
  await until(() => R().mapDb === db, 5000, '当前图的库');
  ok('当前图显示对应的库', R().map === 'rtab_t1' && R().mapDb === db);
  await sleep(2500);   // 等假 AMCL 发出第一条 /amcl_pose
  const c = await cmd('rtab_continue', { map: 'rtab_t1' });
  ok('开始局部重扫', c.ok && /正在和旧库对位/.test(c.msg), c.msg);
  ok('重扫前备份了库', readdirSync(RT).some(f => f.startsWith(db + '.bak-')));
  await until(() => R().cur?.kind === 'continue', 3000, '对位状态');
  ok('对位中', R().cur.phase === 'localizing', R().cur.phase);
  await until(() => R().cur?.phase === 'recording', 10000, '对上旧库');
  ok('对上旧库后开始录新的一轮', logHas(/局部重扫已和旧库对上/));
  await sleep(2500);
  const st = await cmd('rtab_stop');
  ok('停止局部重扫', st.ok && /可以做局部替换/.test(st.msg), st.msg);
  await until(() => !R().cur, 3000, '停止状态');
  ok('结果：2 轮 + 跨会话约束', R().last?.summary?.sessions.length === 2 && R().last.summary.cross > 0, JSON.stringify(R().last?.summary));

  // 7. 网页编辑出来的图继承库（坐标系不变）
  const lm = await cmd('load_map', { name: 'rtab_t1_ed' });
  await until(() => state.mapCurrent === 'rtab_t1_ed' && idle(), 15000, '换到编辑图');
  ok('编辑图沿 parent 继承库', R().mapDb === db, `${lm.msg} · ${R().mapDb}`);

  // 8. 意外退出
  await sleep(2500);
  const c2 = await cmd('rtab_continue', {});
  ok('当前图（编辑图）也能局部重扫', c2.ok, c2.msg);
  await until(() => R().cur?.phase === 'recording', 10000, '第二次对上');
  writeFileSync(CRASH, '1');
  await until(() => !R().cur, 5000, '发现进程没了');
  unlinkSync(CRASH);
  ok('意外退出：报错并记进上一次结果', logHas(/rtabmap 意外退出了/) && R().last?.ok === false, R().last?.msg);

  // 9. 关掉自动录：建图时不录，可以手动开
  const a = await cmd('rtab_auto', { on: false });
  await until(() => R().auto === false, 3000, '设置状态');
  ok('关掉自动录', a.ok && JSON.parse(readFileSync(join(RT, 'settings.json'), 'utf8')).auto === false, a.msg);
  await cmd('set_mode', { mode: 'mapping' });
  await until(() => state.profiles.mapping === 'run' && idle(), 30000, '建图再起来');
  await sleep(4000);
  ok('关掉后建图不自动录', !R().cur && !R().pending);
  const m = await cmd('rtab_start');
  await until(() => R().cur, 3000, '手动开录状态');
  ok('手动开录', m.ok && R().cur.kind === 'map', m.msg);
  await cmd('rtab_auto', { on: true });
  await cmd('set_mode', { mode: null });
  await until(() => state.profiles.mapping === 'stop' && idle(), 30000, '建图停下');
  ok('停档位时手动开的也存好', !R().cur && /^rtab_/.test(R().last?.db || '') && R().last.db !== db);
} catch (e) {
  results.push('FAIL  ' + e.message);
}
clearInterval(timer); ws.close();
for (const l of results) console.log(l);
const fails = results.filter(l => l.startsWith('FAIL')).length;
console.log(`\n${results.length - fails}/${results.length} 通过`);
process.exit(fails ? 1 : 0);
