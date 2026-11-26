// 播放器放在列表外，上传进度重画行时仍能继续播放。
export function setupBagVoice(store, { mock = false, onChange = () => {} } = {}){
  const get = id => document.getElementById(id), history = get('bagHistory');
  const box = get('bagVoicePlayback'), audio = get('bagVoiceAudio'), message = get('bagVoiceMessage');
  let voices = new Map(), current = null, loading = false, lastKey = '', loadError = '';
  const changed = () => onChange();
  function close(){
    audio.pause(); audio.removeAttribute('src'); audio.load();
    current = null; box.hidden = true; message.textContent = ''; changed();
  }
  async function refresh(){
    if (loading || !history.open || document.hidden) return;
    loading = true;
    try {
      let items;
      if (mock) items = store.bags;
      else {
        const response = await fetch('/api/recordings', { cache:'no-store', signal:AbortSignal.timeout(10000) });
        if (!response.ok) throw Error(`HTTP ${response.status}`);
        items = (await response.json()).items;
      }
      const records = items.map(item => [item.name, { audio:item.audio || null,
        state:item.voice_state || 'not_recorded', reason:item.voice_reason || '' }]);
      const key = JSON.stringify(records);
      if (key !== lastKey || loadError){
        voices = new Map(records); lastKey = key; loadError = '';
        if (current && !voices.get(current)?.audio) close();
        changed();
      }
    } catch(e){
      loadError = `录音状态读取失败：${e.message}`; changed();
    } finally { loading = false; }
  }
  function info(name){
    const voice = voices.get(name);
    if (!voice) return { label:loadError ? '录音状态未知' : '检查录音…', title:loadError || '正在检查这个包是否保存了录音', ready:false };
    if (voice.audio){
      const duration = voice.audio.duration_s;
      return { label:duration ? `录音 ${duration} s` : '有录音', title:'播放录制时的语音备注', ready:true };
    }
    return voice.state === 'pending'
      ? { label:'录音待保存', title:'本次语音尚未保存完成', ready:false }
      : { label:'无录音', title:voice.reason || '这个包没有保存语音备注', ready:false };
  }
  async function play(name){
    const voice = voices.get(name)?.audio;
    if (!voice) return;
    if (current === name && !audio.paused){ audio.pause(); return; }
    if (current !== name || audio.getAttribute('src') !== voice.url){
      audio.pause(); current = name; audio.src = voice.url;
    }
    box.hidden = false; get('bagVoiceName').textContent = name;
    message.textContent = '';
    try { await audio.play(); }
    catch(e){
      if (e.name !== 'AbortError' && current === name)
        message.textContent = '无法播放这段录音，请检查音频文件或稍后重试';
    }
    changed();
  }
  for (const event of ['play','pause','ended']) audio.addEventListener(event, changed);
  audio.addEventListener('error', () => {
    if (current && audio.error){ message.textContent = '无法播放这段录音，请检查音频文件或稍后重试'; changed(); }
  });
  get('bagVoiceClose').onclick = close;
  history.addEventListener('toggle', () => { if (history.open) refresh(); else audio.pause(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(refresh, 3000);
  return { info, play, isPlaying:name => current === name && !audio.paused };
}
