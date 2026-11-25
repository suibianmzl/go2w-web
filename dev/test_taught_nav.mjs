// 「启用示教导航」开关：默认关、开 / 关、导航带不带示教节点、重启后保持、关着时拦住「准备示教」。
//   GO2W_NAV_SOURCE=<含 go2w_taught_route 包的目录> PORT=8091 dev/run.sh  然后  node dev/test_taught_nav.mjs ws://127.0.0.1:8091/ws [after-restart]
// 会切档位（只能对本地假 ROS 运行）。after-restart：重启后端后再跑一次，只检查开关是否保持为开，然后关掉复原。
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws', AFTER = process.argv[3] === 'after-restart';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ws = new WebSocket(URL_); let st = null, seq = 0; const acks = new Map();
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data); if (m.t === 'state') st = m; if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
setInterval(() => ws.readyState === 1 && ws.send('{"t":"ping","ts":0}'), 300);
const cmd = (op, a = {}, ) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return; await sleep(100); } throw new Error('超时：' + what); };
const res = []; const ok = (n, c, x = '') => res.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  · ' + x : ''}`);
const nav = async () => { await cmd('set_mode', { mode: 'navigation' }); await until(() => st.profiles.navigation === 'run' && !st.busy, 30000, '导航起来'); await sleep(1500); };
const stop = async () => { await cmd('set_mode', { mode: null }); await until(() => st.profiles.navigation === 'stop' && !st.busy, 30000, '导航停下'); };
await new Promise(r => ws.onopen = r); await until(() => st, 6000, '首个状态');
try {
  if (AFTER) {
    ok('重启后端后开关保持为开', st.taughtNav?.on === true, JSON.stringify(st.taughtNav));
    const r = await cmd('set_taught_nav', { on: false }); ok('复原：关掉', r.ok, r.msg);
  } else {
    if (st.profiles.navigation === 'run') await stop();
    ok('默认关、后端识别到示教包', st.taughtNav?.on === false && st.taughtNav?.installed === true && st.taughtNav?.forced === null, JSON.stringify(st.taughtNav));
    await nav();
    ok('关着开导航：不带示教节点', st.taughtNavigation === false && st.taughtAvailable === false);
    let r = await cmd('taught_prepare', { map: st.mapCurrent });
    ok('关着时「准备示教」被拦', r.ok === false && /示教导航已关闭/.test(r.msg || ''), r.msg);
    r = await cmd('set_taught_nav', { on: true });
    ok('导航运行中打开：提示下次出发重开导航', r.ok && /下次「出发」/.test(r.msg), r.msg);
    await until(() => st.taughtNav?.on === true, 3000, '状态推送');
    ok('打开后 taughtAvailable=true、节点还没起（taughtMissing）', st.taughtAvailable === true && st.taughtNavigation === false);
    r = await cmd('set_mode', { mode: 'navigation' });
    await until(() => st.profiles.navigation === 'run' && !st.busy && st.taughtNavigation === true, 30000, '重开导航带上示教');
    ok('再点开导航：会强制重开并带上示教节点', st.taughtNavigation === true, r.msg);
    r = await cmd('set_taught_nav', { on: false });
    ok('导航带着示教时关闭：提示重开才停', r.ok && /重开导航才会停/.test(r.msg), r.msg);
    await stop(); await nav();
    ok('重开导航后示教节点没了', st.taughtNavigation === false);
    r = await cmd('set_taught_nav', { on: 'yes' });
    ok('非布尔值按「关」处理', r.ok && st.taughtNav?.on === false || /关闭/.test(r.msg), r.msg);
    r = await cmd('set_taught_nav', { on: true }); ok('打开（留给重启后检查）', r.ok, r.msg);
  }
} catch (e) { ok('异常', false, e.message); }
console.log(res.join('\n')); process.exit(0);
