// 闸门参数服务不回应时（真机 11-20 负载 8–10）：普通建图照常能存图；续建中不行。
//   dev/run.sh（PORT=8091）然后 node dev/test_save_gate_hang.mjs ws://127.0.0.1:8091/ws   会切到建图
import fs from 'fs';
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws', HANG = '/tmp/go2w_fake_gate_hang';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ws = new WebSocket(URL_); let state = null, seq = 0; const acks = new Map();
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data); if (m.t === 'state') state = m; if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
setInterval(() => ws.readyState === 1 && ws.send('{"t":"ping","ts":0}'), 300);
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return; await sleep(100); } throw new Error('超时：' + what); };
const res = []; const ok = (n, c, x = '') => res.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  · ' + x : ''}`);
await new Promise(r => ws.onopen = r); await until(() => state, 6000, '首个状态');
try {
  if (state.profiles.mapping !== 'run') { await cmd('set_mode', { mode: 'mapping' }); await until(() => state.profiles.mapping === 'run' && !state.busy, 25000, '建图起来'); }
  await sleep(3000);
  fs.writeFileSync(HANG, '');
  let t0 = Date.now(); let r = await cmd('save_map', { name: 'probe name!' });
  ok('闸门不回应、没在续建：过了闸门检查（走到名字检查）', /只能用字母/.test(r.msg), `${((Date.now()-t0)/1000).toFixed(1)} s · ${r.msg}`);
  const nm = 'gatehang_' + Date.now().toString().slice(-6);
  r = await cmd('save_map', { name: nm }); ok('闸门不回应时真的能存图', r.ok, r.msg);
  fs.unlinkSync(HANG);
  t0 = Date.now(); r = await cmd('save_map', { name: 'probe name!' });
  ok('闸门正常：秒回、走到名字检查', /只能用字母/.test(r.msg) && Date.now() - t0 < 2000, `${((Date.now()-t0)/1000).toFixed(1)} s`);
} catch (e) { ok('异常', false, e.message); } finally { try { fs.unlinkSync(HANG); } catch {} }
console.log(res.join('\n')); process.exit(0);
