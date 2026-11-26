// 播报（11-25，后端 server/audio.py）：底部抽屉「播报」标签。上传的音频在浏览器里转成 16 kHz 单声道 WAV（扩展坞上没有 ffmpeg），
// 存在扩展坞 data/audio，播的时候由后端分块传给狗本体的喇叭。新的播报打断旧的。
import { $ } from './util.js';
import { toast, confirmBox, formBox } from './ui.js';

const RATE = 16000, PEAK = 0.89, GAIN_MAX = 8;   // 统一音量：峰值拉到 -1 dB，最多放大 8 倍

// 文件 → 16 kHz 单声道 16 bit WAV
async function toWav(file){
  const ctx = new OfflineAudioContext(1, 1, RATE);          // decodeAudioData 会顺便重采样到 16 kHz；不需要 https
  const buf = await ctx.decodeAudioData(await file.arrayBuffer());
  const n = buf.length, mix = new Float32Array(n);
  for (let c = 0; c < buf.numberOfChannels; c++){ const d = buf.getChannelData(c); for (let i = 0; i < n; i++) mix[i] += d[i] / buf.numberOfChannels; }
  let peak = 0; for (let i = 0; i < n; i++) peak = Math.max(peak, Math.abs(mix[i]));
  if (peak < 1e-4) throw new Error('这段音频没有声音');
  const g = Math.min(GAIN_MAX, PEAK / peak), v = new DataView(new ArrayBuffer(44 + n * 2));
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); str(8, 'WAVEfmt '); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, RATE, true); v.setUint32(28, RATE * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, mix[i] * g)) * 32767, true);
  return { wav: new Blob([v.buffer], { type: 'audio/wav' }), dur: n / RATE };
}

export function setupAudio(store, src){
  const exec = p => Promise.resolve(p).then(r => { if (r && r.ok === false) toast(r.msg || '操作失败', 'err'); else if (r?.msg) toast(r.msg); return r; });
  const fmt = s => s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} 分 ${String(Math.round(s % 60)).padStart(2, '0')} 秒`;
  const set = (el, t) => { if (el.textContent !== t) el.textContent = t; };
  let busy = false, volDrag = false, key = '', opened = false;

  $('#auUp').onclick = () => $('#auFile').click();
  $('#auFile').onchange = async e => {
    const f = e.target.files[0]; e.target.value = '';
    if (!f || busy) return;
    busy = true;
    try {
      let a;
      try { a = await toWav(f); } catch (x) { return toast(x?.message?.includes('没有声音') ? x.message : `「${f.name}」解不开：换 mp3 / wav 试试`, 'err'); }
      const max = store.audio?.limit || 60;
      if (a.dur > max) return toast(`时长 ${fmt(a.dur)}，最多 ${max} 秒`, 'err');
      const name = await new Promise(res => formBox({ title: '上传音频', ok: '上传', rows: [['时长', fmt(a.dur)]],
        build: box => { const i = document.createElement('input'); i.className = 'modal-select'; i.maxLength = 32;
          i.setAttribute('aria-label', '音频名字'); i.value = f.name.replace(/\.[^.]+$/, '').slice(0, 32); box.append(i); },
        valid: box => !!box.querySelector('input').value.trim(),
        onOk: box => res(box.querySelector('input').value.trim()), onCancel: () => res(null) }));
      if (name) await exec(src.cmd.audioUpload(name, a.wav, a.dur));
    } finally { busy = false; }
  };
  $('#auStop').onclick = () => exec(src.cmd.audioStop());
  const vol = $('#auVol');
  vol.oninput = () => { volDrag = true; set($('#auVolTxt'), vol.value); };
  vol.onchange = () => exec(src.cmd.audioVolume(+vol.value)).finally(() => { volDrag = false; });
  $('#auList').onclick = e => {
    const b = e.target.closest('button[data-a]'); if (!b) return;
    const x = store.audio?.items.find(i => i.id === b.dataset.id); if (!x) return;
    if (b.dataset.a === 'play') exec(src.cmd.audioPlay(x.id));
    if (b.dataset.a === 'ren') formBox({ title: '改名', ok: '保存',
      build: box => { const i = document.createElement('input'); i.className = 'modal-select'; i.maxLength = 32; i.value = x.name; i.setAttribute('aria-label', '音频名字'); box.append(i); },
      valid: box => !!box.querySelector('input').value.trim(),
      onOk: box => exec(src.cmd.audioRename(x.id, box.querySelector('input').value.trim())) });
    if (b.dataset.a === 'del') confirmBox({ title: '删除音频', ok: '删除', danger: true, body: `删除「${x.name}」？删了不能恢复。`, onOk: () => exec(src.cmd.audioDelete(x.id)) });
  };

  function list(A){
    const k = JSON.stringify([A.items, A.avail, A.cur?.clip]); if (k === key) return; key = k;
    const box = $('#auList'); box.textContent = '';
    if (!A.items.length){ const d = document.createElement('div'); d.className = 'empty'; d.textContent = '还没有音频，点「上传音频」加一段（mp3 / wav 等，最长 60 秒）'; box.append(d); return; }
    for (const x of A.items){
      const row = document.createElement('div'); row.className = 'mrow au' + (A.cur?.clip === x.id ? ' on' : '');
      const n = document.createElement('span'); n.className = 'mn'; n.textContent = n.title = x.name;
      const t = document.createElement('span'); t.className = 'mm'; t.textContent = fmt(x.dur);
      row.append(n, t);
      for (const [a, txt, title] of [['play', '播报', '在狗的喇叭上播'], ['ren', '改名', '改名'], ['del', '删除', '删除']]){
        const b = document.createElement('button'); b.className = 'btn sm' + (a === 'play' ? ' pri' : ' ghost'); b.dataset.a = a; b.dataset.id = x.id;
        b.textContent = txt; b.title = `${title}：${x.name}`; b.disabled = a === 'play' ? !A.avail : a === 'del' && A.cur?.clip === x.id; row.append(b);
      }
      box.append(row);
    }
  }
  return function render(){
    const show = !$('#pAudio').hidden;
    if (show && !opened) src.cmd.audioRefresh();      // 每次打开标签重读一次音量（别处可能改过）
    opened = show;
    if (!show) return;
    const A = store.audio, linked = store.link.ok;
    if (!A){ set($('#auNow'), linked ? '后端还没有播报功能（要更新后端）' : '没有连上后端'); $('#auUp').disabled = $('#auStop').disabled = vol.disabled = true; return; }
    list(A);
    const c = A.cur, el = c ? (performance.now() - A.rx) / 1000 + c.elapsed : 0;
    const L = A.last;
    set($('#auNow'), !linked ? '没有连上后端' : c ? (c.phase === 'send' ? `正在传给狗：${c.name}` : c.phase === 'wait' ? `等 ${Math.max(0, Math.ceil((c.wait || 0) - el))} s 后播：${c.name}` : `正在播：${c.name}（${Math.min(el, c.dur).toFixed(0)} / ${c.dur.toFixed(0)} s）`)
      : !A.avail ? `喇叭不可用：${A.why}` : L ? `空闲 · 上次「${L.name}」${L.ok ? '播完' : L.msg}` : '空闲');
    $('#auNow').dataset.s = c ? 'run' : !linked || !A.avail || (L && !L.ok && L.msg !== '被停止') ? 'warn' : '';
    $('#auUp').disabled = !linked || busy; $('#auStop').disabled = !linked || !c;
    vol.disabled = !linked || !A.avail;
    if (!volDrag){ if (A.vol != null && +vol.value !== A.vol) vol.value = A.vol; set($('#auVolTxt'), A.vol == null ? '--' : String(A.vol)); }
  };
}
