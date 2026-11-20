// 设完初始位姿 / 自动定位成功后，匹配率不能留着旧位姿下的低值（出发流程会误报「匹配率 7%」）：
//   GO2W_FAKE_START=2,-0.5,0 dev/run.sh      然后   node dev/test_match_reset.mjs
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ws = new WebSocket(URL_); let state = null, seq = 0; const acks = new Map(); const log = [];
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
  if (m.t === 'state') { state = m; log.push([Date.now(), m.loc?.match ?? null]); } if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 300);
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return; await sleep(100); } throw new Error('超时：' + what); };
const res = []; const ok = (n, c, x = '') => res.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  · ' + x : ''}`);
await new Promise(r => ws.onopen = r); await until(() => state, 6000, '首个状态');
try {
  if (state.profiles.navigation !== 'run') { await cmd('set_mode', { mode: 'navigation' }); await until(() => state.profiles.navigation === 'run' && !state.busy, 25000, '导航起来'); }
  await until(() => state.autoLoc?.state && state.autoLoc.state !== 'run', 40000, '启动后自动定位结束').catch(() => null);
  // 故意给一个错的位姿，让匹配率掉下去
  let r = await cmd('initial_pose', { x: -3, y: 4, th: 2.0 }); ok('给错误位姿', r.ok, r.msg);
  await until(() => (state.loc?.match ?? 100) < 40, 8000, '匹配率掉到 40% 以下');
  ok('错误位姿下匹配率低', state.loc.match < 40, `${state.loc.match}%`);
  const t0 = Date.now(); r = await cmd('auto_localize'); const t1 = Date.now();
  ok('自动定位成功', r.ok, r.msg);
  await until(() => (state.loc?.match ?? 0) >= 60, 4000, '新匹配率 ≥ 60%');
  const after = log.filter(([t]) => t > t1);
  const low = after.filter(([, m]) => m != null && m < 60 && log.find(([t]) => t > t1 && t <= log.at(-1)[0]));
  const firstHigh = after.find(([, m]) => m != null && m >= 60);
  ok('成功后推给网页的匹配率没有旧的低值（只有空或新值）', after.filter(([t, m]) => m != null && m < 60).length === 0,
     after.slice(0, 8).map(([t, m]) => `${t - t1}ms:${m}`).join(' '));
  ok('4 s 内拿到新匹配率', !!firstHigh, firstHigh ? `${firstHigh[0] - t1} ms → ${firstHigh[1]}%` : '');
} catch (e) { ok('异常', false, e.message); }
console.log(res.join('\n')); console.log(res.every(x => x.startsWith('PASS')) ? 'ALL OK' : 'SOME FAILED'); process.exit(0);
