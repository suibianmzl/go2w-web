// 控制台和手机录制页共用：一次操作、一条录包请求、一段语音。
const duration = value => Number.isFinite(+value) ? Math.max(1, Math.min(20, Math.round(+value))) : 5;

export function recordingShortcut(action, key='Enter'){
  return e => {
    if (e.key.toLowerCase() !== key.toLowerCase() || !e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || e.repeat || e.isComposing) return;
    if (e.target?.closest?.('input,select,textarea,[contenteditable]:not([contenteditable="false"]),[role="dialog"],.scrim')) return;
    e.preventDefault(); action();
  };
}

export function createRollbackCapture(store, src, notify = () => {}){
  let seconds = 5;
  try { const saved = localStorage.getItem('go2w.voiceSeconds'); if (saved !== null) seconds = duration(saved); } catch {}
  const state = { seconds, busy:false, recording:false, remaining:0, message:'', kind:'', pending:null };
  const listeners = new Set();
  const emit = () => listeners.forEach(fn => fn(state));
  const message = (text, kind = 'ok') => { state.message=text;state.kind=kind;emit();notify(text,kind); };
  let cancelVoice = null, cancelled = false;

  async function recover(requestId, attempts=1){
    for(let i=0;i<attempts;i++){
      try{
        const response=await fetch(`/api/recordings?request_id=${encodeURIComponent(requestId)}`,{cache:'no-store',signal:AbortSignal.timeout(2000)});
        if(response.ok){const item=(await response.json()).items[0];if(item)return {ok:true,data:{name:item.name,duration_s:item.dur,requested_s:30}};}
      }catch{}
      if(i+1<attempts)await new Promise(resolve=>setTimeout(resolve,1000));
    }
    return {ok:false,uncertain:true,msg:'录包结果尚未确认；语音已保留，请恢复连接后重试核对'};
  }

  async function recordVoice(){
    if (!globalThis.isSecureContext) throw Error('语音未录制：手机局域网访问需要受信任的 HTTPS');
    if (!navigator.mediaDevices?.getUserMedia || !globalThis.MediaRecorder) throw Error('语音未录制：浏览器不支持麦克风录音');
    let expired = false, permissionTimer;
    const permission = navigator.mediaDevices.getUserMedia({audio:true}).then(stream => {
      if (expired || cancelled){ stream.getTracks().forEach(t=>t.stop()); throw Error('录音请求已结束'); }
      return stream;
    });
    let stream;
    try {
      stream = await Promise.race([permission, new Promise((_,reject) => {
        permissionTimer=setTimeout(()=>{expired=true;reject(Error('等待麦克风授权超时，语音未录制'));},10000);
      })]);
    } catch (e){
      if (e.name === 'NotAllowedError') throw Error('语音未录制：请在浏览器中允许麦克风权限');
      throw e;
    } finally { clearTimeout(permissionTimer); }
    return new Promise((resolve,reject) => {
      let recorder, timer, ticker, finished=false;
      const chunks=[], started=performance.now(), wanted=state.seconds;
      const release = () => {
        clearTimeout(timer);clearInterval(ticker);stream.getTracks().forEach(t=>t.stop());
        document.removeEventListener('visibilitychange', hidden);window.removeEventListener('pagehide', stop);
        cancelVoice=null;state.recording=false;state.remaining=0;emit();
      };
      const fail = e => { if (finished) return;finished=true;release();reject(e); };
      const stop = () => { if (recorder?.state === 'recording') recorder.stop(); };
      const hidden = () => { if (document.hidden) stop(); };
      try {
        const mimeType=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(t=>MediaRecorder.isTypeSupported(t));
        if (!mimeType) throw Error('语音未录制：浏览器没有可用的录音格式');
        recorder=new MediaRecorder(stream,{mimeType,audioBitsPerSecond:64000});
        recorder.ondataavailable=e=>{ if(e.data.size) chunks.push(e.data); };
        recorder.onerror=()=>fail(Error('麦克风录音中断'));
        recorder.onstop=()=>{
          if(finished)return;finished=true;
          const elapsed=Math.max(.001,Math.min(20,(performance.now()-started)/1000));
          const blob=new Blob(chunks,{type:recorder.mimeType});release();
          if(cancelled) resolve(null);
          else if(!blob.size) reject(Error('麦克风没有录到声音'));
          else resolve({blob,duration:elapsed});
        };
        stream.getTracks().forEach(t=>{t.onended=stop;});
        cancelVoice=stop;
        recorder.start();state.recording=true;state.remaining=wanted;emit();
        ticker=setInterval(()=>{state.remaining=Math.max(0,Math.ceil(wanted-(performance.now()-started)/1000));emit();},250);
        timer=setTimeout(stop,wanted*1000);
        document.addEventListener('visibilitychange',hidden);window.addEventListener('pagehide',stop);
      } catch(e){fail(e);}
    });
  }

  async function upload(){
    const pending=state.pending;
    const response=await fetch(`/api/recordings/${encodeURIComponent(pending.name)}/audio?duration=${pending.duration}&request_id=${encodeURIComponent(pending.requestId)}`,{
      method:'POST',headers:{'Content-Type':pending.blob.type},body:pending.blob,
      signal:AbortSignal.timeout(15000),
    });
    if(!response.ok) throw Error((await response.text()).slice(0,160) || `HTTP ${response.status}`);
    state.pending=null;
  }

  async function markUnavailable(name, reason, requestId){
    if(src.name==='mock')return;
    const response=await fetch(`/api/recordings/${encodeURIComponent(name)}/audio-status`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state:'unavailable',reason,request_id:requestId}),
      signal:AbortSignal.timeout(15000),
    });
    if(!response.ok)throw Error((await response.text()).slice(0,160) || `HTTP ${response.status}`);
  }

  const api = {
    state,
    subscribe(fn){listeners.add(fn);fn(state);return()=>listeners.delete(fn);},
    setSeconds(value){if(state.busy)return;state.seconds=duration(value);try{localStorage.setItem('go2w.voiceSeconds',state.seconds);}catch{}emit();},
    async save(){
      if(state.busy || state.pending)return;
      if(!store.link.ok || !store.rollback.run || store.rollback.saving){message(!store.link.ok?'没有连上后端':store.rollback.saving?'正在保存上一段，请稍等':'回溯录制未运行，请先在控制台启动建图、定位或导航','err');return;}
      state.busy=true;state.message='正在收集点击后10秒的数据，并申请麦克风…';state.kind='';cancelled=false;emit();
      let result, voice, voiceError;
      const requestId=globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      try {
        // 先发送保存请求，首次麦克风授权的等待不改变录包窗口。
        const bag=Promise.resolve(src.cmd.saveRollback(requestId,state.seconds)).catch(e=>({ok:false,uncertain:true,msg:e.message})).then(r=>{
          if(!r?.ok && !r?.uncertain){cancelled=true;cancelVoice?.();}return r;
        });
        const audio=recordVoice().catch(e=>{voiceError=e.message;return null;});
        [result,voice]=await Promise.all([bag,audio]);
        if(voice)state.pending={...voice,name:result?.ok ? result.data?.name : null,requestId};
        else if(result?.uncertain)state.pending={name:null,requestId,unavailableReason:voiceError || '语音未录制'};
        if(result?.uncertain){state.message='连接中断，正在按本次请求核对录包…';emit();result=await recover(requestId,15);if(result.ok && state.pending)state.pending.name=result.data.name;}
        if(!result?.ok){message(`${result?.msg || '录包未完成，请检查结果列表'}${voice?'；语音可先下载到本机':''}`,'err');return;}
        const actual=result.data?.duration_s, wanted=result.data?.requested_s ?? 30;
        const partial=Number.isFinite(actual) && actual<wanted-1;
        const saved=partial?`数据包仅覆盖 ${actual.toFixed(1)} 秒（目标 ${wanted} 秒，缓存可能不足）`:'数据包已保存';
        if(!voice){
          try{await markUnavailable(result.data.name,voiceError || '语音未录制',requestId);state.pending=null;message(`${saved}；${voiceError || '语音未录制'}`,'warn');}
          catch(e){state.pending={name:result.data.name,requestId,unavailableReason:voiceError || '语音未录制'};message(`${saved}；${voiceError || '语音未录制'}；状态同步失败，可重试后上传整包`,'warn');}
          return;
        }
        if(!state.pending.name){message('数据包已保存，但后端未返回包名；语音可下载，请更新后端','warn');return;}
        if(src.name==='mock'){state.pending=null;message('已保存前20秒＋后10秒（模拟），语音不写入机器人');return;}
        state.message='数据包已保存，正在保存语音…';emit();
        try { await upload();message(partial?`${saved}；语音备注已保存`:'数据包与语音备注已保存',partial?'warn':'ok'); }
        catch(e){message(`数据包已保存，语音上传失败：${e.message}；可重试或下载`,'warn');}
      } catch(e){message(e.message,'err');}
      finally {state.busy=false;emit();}
      return result;
    },
    async retry(){
      if(state.busy || !state.pending)return;
      state.busy=true;emit();
      try{
        if(!state.pending.name){const result=await recover(state.pending.requestId);if(!result.ok)throw Error(result.msg);state.pending.name=result.data.name;}
        if(state.pending.unavailableReason){await markUnavailable(state.pending.name,state.pending.unavailableReason,state.pending.requestId);state.pending=null;message('已同步本次没有录音的原因，可上传数据包','warn');}
        else{await upload();message('语音备注已保存');}
      }catch(e){message(`语音上传失败：${e.message}`,'warn');}
      finally{state.busy=false;emit();}
    },
    async discard(){
      if(state.busy || !state.pending)return;
      state.busy=true;emit();
      try{
        if(!state.pending.name){const result=await recover(state.pending.requestId);if(!result.ok)throw Error(result.msg);state.pending.name=result.data.name;}
        await markUnavailable(state.pending.name,'用户结束本次语音处理，未随包保存录音',state.pending.requestId);state.pending=null;message('已结束语音处理，匹配信息会注明本包无录音','warn');
      }catch(e){state.pending=null;message('已结束本地语音处理；服务端状态未确认，请在结果列表核对，并按需结束待处理语音','warn');}
      finally{state.busy=false;emit();}
    },
  };
  return api;
}

export function setupRecordingControls(capture, root=document){
  const get=id=>root.querySelector(`#${id}`);
  const seconds=get('voiceSeconds'), status=get('voiceStatus'), retry=get('voiceRetry'), download=get('voiceDownload'), discard=get('voiceDiscard');
  if(!seconds)return;
  seconds.onchange=()=>capture.setSeconds(seconds.value);
  retry.onclick=()=>capture.retry();discard.onclick=()=>capture.discard();
  let blob=null,url=null;
  capture.subscribe(state=>{
    seconds.value=state.seconds;seconds.disabled=state.busy;
    status.textContent=state.recording?`正在录语音 · 剩余 ${state.remaining} 秒` : state.message;
    status.dataset.kind=state.kind;
    retry.hidden=!state.pending?.requestId;retry.disabled=state.busy;
    download.hidden=!state.pending?.blob;discard.hidden=!state.pending;discard.disabled=state.busy;
    if(blob!==state.pending?.blob){
      if(url)URL.revokeObjectURL(url);blob=state.pending?.blob;url=blob?URL.createObjectURL(blob):null;
      if(url){download.href=url;download.download=`${state.pending.name || 'voice-note'}.${blob.type.startsWith('audio/mp4')?'m4a':blob.type.startsWith('audio/ogg')?'ogg':'webm'}`;}
      else download.removeAttribute('href');
    }
  });
}
