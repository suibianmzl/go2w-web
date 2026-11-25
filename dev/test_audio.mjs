// 播报（server/audio.py）协议测试，只能对本地假 ROS 运行：dev/run.sh 之后
//   node dev/test_audio.mjs [http://127.0.0.1:8080] [locked]
// locked：后端带 GO2W_AUDIO_LOCAL_EDIT=0 启动，测 127.0.0.1（云端网关）只能播 / 停
import fs from 'node:fs';
const BASE = process.argv[2] || 'http://127.0.0.1:8080', LOCKED = process.argv[3] === 'locked';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const res = []; const ok = (n, c, x = '') => res.push(`${c ? 'PASS' : 'FAIL'}  ${n}${x ? '  · ' + x : ''}`);
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return Date.now() - t0; await sleep(20); } throw new Error('超时：' + what); };
const LOG = '/tmp/go2w_fake_audio.log', WAV = '/tmp/go2w_fake_audio.wav', OFF = '/tmp/go2w_fake_audio_off', FAIL = '/tmp/go2w_fake_audio_fail';
const rm = f => { try { fs.unlinkSync(f); } catch {} };
const log = () => { try { return JSON.parse(fs.readFileSync(LOG)); } catch { return []; } };
function wav(sec, { rate = 16000, ch = 1, junk = false } = {}){
  const n = Math.round(sec * rate) * ch, pcm = Buffer.alloc(n * 2);
  for (let i = 0; i < n; i++) pcm.writeInt16LE(Math.round(9000 * Math.sin(i * 0.2) + 500 * Math.sin(i * 7.7)), i * 2);
  const h = Buffer.alloc(36); h.write('RIFF', 0); h.writeUInt32LE(36 + pcm.length + (junk ? 12 : 0), 4); h.write('WAVEfmt ', 8); h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20); h.writeUInt16LE(ch, 22); h.writeUInt32LE(rate, 24); h.writeUInt32LE(rate * ch * 2, 28); h.writeUInt16LE(ch * 2, 32); h.writeUInt16LE(16, 34);
  const j = Buffer.alloc(junk ? 12 : 0); if (junk){ j.write('FLLR', 0); j.writeUInt32LE(4, 4); }
  const d = Buffer.alloc(8); d.write('data', 0); d.writeUInt32LE(pcm.length, 4);
  return { file: Buffer.concat([h, j, d, pcm]), pcm };
}
const up = async (name, body, headers = { 'X-Go2w': '1' }) => { const r = await fetch(`${BASE}/api/audio?name=${encodeURIComponent(name)}`, { method: 'POST', headers, body }); return { status: r.status, ...(await r.json()) }; };
const ws = new WebSocket(BASE.replace('http', 'ws') + '/ws'); let A = null, task = null, seq = 0; const acks = new Map();
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data); if (m.t === 'audio') A = m; if (m.t === 'ack'){ acks.get(m.id)?.(m); acks.delete(m.id); } };
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
await new Promise(r => ws.onopen = r);
const beat = setInterval(() => ws.send('{"t":"ping","ts":0}'), 300);
rm(OFF); rm(FAIL);
try {
  await until(() => A?.avail, 4000, '喇叭在线');
  ok('连上就推播报状态、喇叭在线', true, `vol ${A.vol} 音频 ${A.items.length}`);
  if (LOCKED){
    const before = A.items.length;
    let r = await up('云端上传', wav(1).file); ok('本机入口不能上传', r.status === 403 && A.items.length === before, r.msg);
    for (const [op, a] of [['audio_volume', { v: 1 }], ['audio_rename', { clip: A.items[0].id, name: 'x' }], ['audio_delete', { clip: A.items[0].id }]]){
      r = await cmd(op, a); ok(`本机入口拒绝 ${op}`, !r.ok, r.msg); }
    r = await cmd('audio_play', { clip: A.items[0].id }); ok('本机入口能播', r.ok, r.msg);
    await until(() => A.cur, 2000, '在播'); r = await cmd('audio_stop'); ok('本机入口能停', r.ok, r.msg);
  } else {
    for (const x of [...A.items]) await cmd('audio_delete', { clip: x.id });
    let r, t;
    await until(() => A.vol === 5, 8000, '读到音量'); ok('自动读到音量', true);
    r = await up('坏', Buffer.from('hello')); ok('不是 WAV 被拒', r.status === 400, r.msg);
    r = await up('44k', wav(1, { rate: 44100 }).file); ok('采样率不对被拒', r.status === 400, r.msg);
    r = await up('双声道', wav(1, { ch: 2 }).file); ok('双声道被拒', r.status === 400, r.msg);
    r = await up('太短', wav(0.05).file); ok('太短被拒', r.status === 400, r.msg);
    r = await up('太长', wav(61).file); ok('61 s 被拒', r.status === 400, r.msg);
    r = await up('超大', Buffer.alloc(4 << 20)); ok('超大请求 413', r.status === 413, r.msg);
    r = await up('没头', wav(1).file, {}); ok('没有 X-Go2w 头被拒', r.status === 403, r.msg);
    r = await up('<b>', wav(1).file); ok('名字带 < > 被拒', r.status === 400, r.msg);
    r = await up('  ', wav(1).file); ok('空名字被拒', r.status === 400, r.msg);
    const a = wav(2, { junk: true }); r = await up('请注意避让', a.file); ok('带多余块的 WAV 能上传', r.ok, r.msg); const idA = r.item.id;
    r = await up('请注意避让', a.file); ok('重名被拒', r.status === 400, r.msg);
    r = await up('长音频', wav(59).file); ok('59 s 能上传', r.ok, r.msg); const idB = r.item.id;
    await until(() => A.items.length === 2, 2000, '列表推送');

    rm(WAV); let n0 = log().length;
    r = await cmd('audio_play', { clip: idA }); ok('播放', r.ok, r.msg);
    await until(() => A.cur?.phase === 'play', 4000, '进入播放');
    await until(() => !A.cur, 6000, '播完');
    let L = log().slice(n0); ok('4001 → 4003… → 4002', L[0] === 4001 && L.at(-1) === 4002 && L.slice(1, -1).every(x => x === 4003), `${L.length - 2} 块`);
    const got = fs.readFileSync(WAV); ok('狗收到的 WAV：44 字节头 + 原样 PCM', got.length === 44 + a.pcm.length && got.subarray(44).equals(a.pcm) && got.toString('latin1', 36, 40) === 'data');
    ok('结果：播完', A.last?.ok && A.last.clip === idA, A.last?.msg);

    n0 = log().length; await cmd('audio_play', { clip: idB }); await until(() => A.cur?.phase === 'send', 2000, '上传'); await sleep(300);
    r = await cmd('audio_stop'); await until(() => !A.cur, 1000, '停'); L = log().slice(n0);
    ok('上传中停止：退出喊话、不再发块', r.ok && L.at(-1) === 4002, A.last.msg); const len = log().length; await sleep(400); ok('停止后没有后续请求', log().length === len);
    n0 = log().length; await cmd('audio_play', { clip: idB }); await sleep(200); r = await cmd('audio_play', { clip: idA });
    await until(() => A.cur?.clip === idA && A.cur.phase === 'play', 4000, '换播'); L = log().slice(n0);
    ok('新播报打断旧的：先 4002 再 4001', r.ok && L.indexOf(4002) > 0 && L[L.indexOf(4002) + 1] === 4001 && L.filter(x => x === 4001).length === 2);
    r = await cmd('audio_delete', { clip: idA }); ok('正在播的不能删', !r.ok, r.msg);
    r = await cmd('audio_stop'); await until(() => !A.cur, 1000, '停');
    r = await cmd('audio_stop'); ok('没在播时停止', r.ok, r.msg);
    // 5 个播放同时发：只剩最后一个在播，每个 4001 都有 4002
    n0 = log().length; await Promise.all([idA, idB, idA, idB, idA].map(c => cmd('audio_play', { clip: c }))); await sleep(300); await cmd('audio_stop'); await sleep(100);
    L = log().slice(n0); ok('连点 5 次：进 / 出喊话成对', L.filter(x => x === 4001).length === L.filter(x => x === 4002).length && L.at(-1) === 4002, `${L.filter(x => x === 4001).length} 次`);

    for (const code of [4001, 4003]){
      fs.writeFileSync(FAIL, String(code)); n0 = log().length; t = A.last.ts; await cmd('audio_play', { clip: idA });
      await until(() => A.last.ts !== t && !A.cur, 4000, '失败结束'); L = log().slice(n0);
      ok(`${code} 出错：报失败并退出喊话`, !A.last.ok && L.at(-1) === 4002, A.last.msg); }
    rm(FAIL);
    r = await cmd('audio_volume', { v: 8 }); await until(() => A.vol === 8, 1000, '音量推送'); ok('设音量', r.ok, r.msg);
    for (const v of [11, -1, 3.5, true, '5', null]){ r = await cmd('audio_volume', { v }); if (r.ok) ok(`音量 ${v} 应被拒`, false); }
    fs.writeFileSync(FAIL, '1003'); r = await cmd('audio_volume', { v: 2 }); ok('设音量失败会报', !r.ok, r.msg); rm(FAIL);
    await cmd('audio_volume', { v: 5 });
    r = await cmd('audio_rename', { clip: idA, name: ' 请 让行 ' }); await until(() => A.items.some(x => x.name === '请 让行'), 1000, '改名推送'); ok('改名', r.ok, r.msg);
    r = await cmd('audio_rename', { clip: idA, name: '长音频' }); ok('改成重名被拒', !r.ok, r.msg);
    r = await cmd('audio_play', { clip: '../../etc/passwd' }); ok('乱写 clip 被拒', !r.ok, r.msg);
    r = await cmd('audio_play', { clip: { a: 1 } }); ok('clip 类型不对被拒', !r.ok, r.msg);
    r = await cmd('audio_xx'); ok('未知操作被拒', !r.ok, r.msg);
    fs.writeFileSync(OFF, ''); await until(() => !A.avail, 3000, '喇叭离线'); r = await cmd('audio_play', { clip: idA }); ok('喇叭服务不在：拒绝播放', !r.ok, r.msg);
    rm(OFF); await until(() => A.avail, 3000, '喇叭恢复');
    r = await cmd('audio_delete', { clip: idB }); await until(() => A.items.length === 1, 1000, '删除推送'); ok('删除', r.ok, r.msg);
  }
} catch (e) { ok('异常', false, e.message); }
clearInterval(beat); ws.close(); rm(OFF); rm(FAIL);
console.log(res.join('\n')); const bad = res.filter(x => x.startsWith('FAIL')).length; console.log(`${res.length - bad}/${res.length}`); process.exit(bad ? 1 : 0);
