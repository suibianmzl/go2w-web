// 建图自动保存测试（11-17）：定时覆盖同一份、出问题时立刻存、停止建图前存、只留最近 3 次。
// 只能对本地假 ROS 运行（会切档位）。用临时数据目录，并把周期调短、让假地图一直在长：
//   D=$(mktemp -d); mkdir -p $D/maps; cp ../data/maps/* $D/maps/
//   GO2W_DATA=$D GO2W_AUTOSAVE_SECONDS=4 GO2W_FAKE_MAP_GROW=1 dev/run.sh
//   GO2W_DATA=$D node dev/test_autosave.mjs [ws://127.0.0.1:8080/ws]
import { existsSync, readFileSync, readdirSync, writeFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const DATA = process.env.GO2W_DATA;
if (!DATA) { console.error('要设置 GO2W_DATA（和 run.sh 用同一个数据目录）'); process.exit(2); }
const MAPS = join(DATA, 'maps');
const ODOM_OFF = process.env.GO2W_FAKE_ODOM_OFF || '/tmp/go2w_fake_odom_off';
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 3 份更早的自动保存：第一次自动保存后应删掉最早那份（只留最近 3 次，含这一次）
const OLD = ['autosave_0101_000000', 'autosave_0101_000001', 'autosave_0101_000002'];
for (const n of OLD){
  writeFileSync(join(MAPS, `${n}.yaml`), `image: ${n}.pgm\nmode: trinary\nresolution: 0.05\norigin: [0, 0, 0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n`);
  writeFileSync(join(MAPS, `${n}.pgm`), Buffer.concat([Buffer.from('P5\n2 2\n255\n'), Buffer.from([254, 254, 0, 205])]));
}
try { unlinkSync(ODOM_OFF); } catch {}

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

try {
  await new Promise(r => ws.onopen = r);
  await until(() => state, 8000, '首个状态');
  await cmd('set_mode', { mode: 'mapping' });
  await until(() => state.profiles.mapping === 'run' && !state.busy, 30000, '建图起来');

  // 1. 第一次自动保存：四个文件都在，地图列表里有，日志说明了周期
  await until(() => state.autosave?.count >= 1, 20000, '第一次自动保存');
  const name = state.autosave.name;
  const files = ['.yaml', '.pgm', '.posegraph', '.data'].map(x => join(MAPS, name + x));
  ok('第一次自动保存：yaml/pgm/posegraph/data 都写出来', files.every(existsSync), name);
  ok('地图列表里有自动保存', state.maps.some(m => m.name === name));
  ok('yaml 是 trinary + free 0.196', /mode: trinary/.test(readFileSync(files[0], 'utf8')) && /free_thresh: 0.196/.test(readFileSync(files[0], 'utf8')));
  ok('日志写了自动保存位置和周期', logHas(new RegExp(`建图自动保存：data/maps/${name}.*每 \\d+ s`)));
  ok('临时目录里没有残留', !readdirSync(join(MAPS, '.autosave_tmp')).length);

  // 2. 只留最近 3 次：最早的那份被删掉，另外两份留着
  ok('只留最近 3 次：删掉最早的', !existsSync(join(MAPS, `${OLD[0]}.yaml`)) && !existsSync(join(MAPS, `${OLD[0]}.pgm`)));
  ok('只留最近 3 次：较新的留着', existsSync(join(MAPS, `${OLD[1]}.yaml`)) && existsSync(join(MAPS, `${OLD[2]}.yaml`)));

  // 3. 定时覆盖同一份（假地图一直在长）
  const c1 = state.autosave.count;
  await until(() => state.autosave.count >= c1 + 2, 20000, '定时再存两次');
  ok('定时保存：覆盖同一个名字', state.autosave.name === name && readdirSync(MAPS).filter(f => f.startsWith('autosave_') && f.endsWith('.yaml') && !f.includes('_prev')).length === 3,
     `第 ${state.autosave.count} 次`);
  // 11-19：覆盖前把上一份挪成 <名字>_prev（yaml 的 image 跟着改），坏图写进来时还能退一步
  const prevYaml = join(MAPS, `${name}_prev.yaml`);
  ok('覆盖前留一份 _prev，四件套齐、yaml 指向自己的 pgm',
     ['.yaml', '.pgm', '.posegraph', '.data'].every(x => existsSync(join(MAPS, `${name}_prev${x}`)))
     && new RegExp(`^image: ${name}_prev\\.pgm$`, 'm').test(readFileSync(prevYaml, 'utf8')));
  ok('定时保存不刷日志', logs.filter(t => /建图自动保存：/.test(t)).length === 1);

  // 4. 里程计出问题（slam 还活着）：马上存一次，日志写明原因
  writeFileSync(ODOM_OFF, '1');
  await until(() => logHas(/建图自动保存（建图出问题（里程计不可用））/), 15000, '出问题时自动保存');
  ok('里程计出问题：马上存并写明原因', true);
  unlinkSync(ODOM_OFF);
  await sleep(1500);

  // 5. 停止建图：停之前先存
  await sleep(2500);   // 让假地图再长一点，确保有没存过的内容
  await cmd('set_mode', { mode: null });
  await until(() => state.profiles.mapping === 'stop' && !state.busy, 30000, '建图停下');
  ok('停止建图前自动保存', logHas(/建图自动保存（停止建图前）/));
  const order = logs.findIndex(t => /建图自动保存（停止建图前）/.test(t)) < logs.findIndex(t => /已停止建图/.test(t));
  ok('先存再停', order);
  ok('停下后地图还在', files.every(existsSync));
} catch (e) {
  ok(String(e.message || e), false);
} finally {
  try { unlinkSync(ODOM_OFF); } catch {}
  clearInterval(timer); ws.close();
}
for (const r of results) console.log(r);
const fails = results.filter(x => x.startsWith('FAIL')).length;
console.log(fails ? `\n${fails} 项失败` : `\n全部通过（${results.length} 项）`);
process.exit(fails ? 1 : 0);
