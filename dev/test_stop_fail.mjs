// 停档位报错（11-17 真机：FAST-LIO 退得慢，supervisor 抛 "navigation did not stop after SIGINT"）后，
// 界面不能一直卡在「停止中」。只能对本地假 ROS 运行（会切档位）：
//   dev/run.sh            另开终端：node dev/test_stop_fail.mjs [ws://127.0.0.1:8080/ws]
import { writeFileSync, unlinkSync } from 'node:fs';
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const FAIL = process.env.GO2W_FAKE_STOP_FAIL || '/tmp/go2w_fake_stop_fail';
const sleep = ms => new Promise(r => setTimeout(r, ms));
try { unlinkSync(FAIL); } catch {}

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

try {
  await new Promise(r => ws.onopen = r);
  await until(() => state, 8000, '首个状态');
  if (state.profiles.mapping !== 'stop' || state.profiles.navigation !== 'stop') {
    await cmd('set_mode', { mode: null });
    await until(() => ['mapping', 'localization', 'navigation'].every(m => state.profiles[m] === 'stop') && !state.busy, 30000, '先停干净');
  }
  await cmd('set_mode', { mode: 'mapping' });
  await until(() => state.profiles.mapping === 'run' && !state.busy, 30000, '建图起来');

  writeFileSync(FAIL, '1');
  const n0 = logs.length;
  await cmd('set_mode', { mode: null });
  await until(() => logs.slice(n0).some(t => /启停失败：RuntimeError/.test(t)), 15000, '停止报错');
  ok('停止报错写进日志', true);
  await until(() => !state.busy, 5000, 'busy 清掉');
  let stuck = true;
  try { await until(() => state.profiles.mapping === 'stop', 8000, '建图回到 stop'); stuck = false; } catch {}
  ok('报错后不再卡在「停止中」', !stuck, `mapping=${state.profiles.mapping}`);
  unlinkSync(FAIL);

  // 之后还能正常开、正常停
  await cmd('set_mode', { mode: 'mapping' });
  await until(() => state.profiles.mapping === 'run' && !state.busy, 30000, '再次建图');
  await cmd('set_mode', { mode: null });
  await until(() => state.profiles.mapping === 'stop' && !state.busy, 30000, '再次停下');
  ok('之后能正常开、停', true);
} catch (e) {
  ok(String(e.message || e), false);
} finally {
  try { unlinkSync(FAIL); } catch {}
  clearInterval(timer); ws.close();
  console.log(results.join('\n'));
  process.exit(results.some(r => r.startsWith('FAIL')) ? 1 : 0);
}
