// 示教任务页：只请求后端采集状态，不把普通里程轨迹当作示教，也不在自检/准备阶段发速度。
import { $, $$ } from './util.js';
import { toast, confirmBox, formBox } from './ui.js';
import { captureIsOpen, storeTaughtDocument, taughtStartBlocker, taughtProcessingSummary } from './taught_geometry.js';

const CAPTURE_STATE = {recording:'正在采集',paused:'已暂停',interrupted:'采集中断',processing:'平滑与校验中',finished:'已保存',failed:'校验未通过',abandoned:'草稿已放弃'};
const CHECK_STATE = {idle:'尚未自检',preparing:'准备中',standing:'正在自动站起',settling:'等待站稳',loading:'加载地图中',localizing:'定位中',ready:'自检通过，可开始采集',failed:'自检未通过'};
const MODES = {replace:'整条替换',append:'末尾追加',replace_segment:'替换选中区间'};
const editorUrl = (map, capture) => `/taught-editor${map ? `?map=${encodeURIComponent(map)}${capture ? `&capture=${encodeURIComponent(capture)}` : ''}` : ''}`;

export function setupTaught(store, src, setTool){
  const sel = $('#taughtMapSel'), mode = $('#taughtMode');
  let mapsKey = '', rangeKey = '', checksKey = '', request = 0, busy = false, finishing = false, pickFirst = null, wasLinked = false;
  const doc = () => store.taught?.map === store.taughtMap ? store.taught : null;
  const currentCapture = () => doc()?.capture;
  const range = () => ({start:Number($('#taughtRangeStart').value)-1,end:Number($('#taughtRangeEnd').value)-1});
  function message(text, error = false){ const el = $('#taughtMessage'); el.hidden = !text; el.textContent = text; el.classList.toggle('warn',error); }
  function accept(data){ if (data?.map) storeTaughtDocument(store,data); }
  async function command(name, arg){
    if (busy) return {ok:false,msg:'上一个示教操作尚未完成'};
    if (!src.can(name)) return {ok:false,msg:'当前数据源不支持示教操作'};
    const map = store.taughtMap; busy = true; finishing = name === 'taughtFinish'; message(''); render();
    try {
      const r = await Promise.resolve(src.cmd[name](arg));
      if (map !== store.taughtMap) return r;
      accept(r?.data);
      if (r?.ok === false) message(r.msg || '操作失败，原参考轨迹与草稿均保留',true);
      else if (r?.msg) message(r.msg);
      return r;
    } catch (e){ const r = {ok:false,msg:e.message || '示教请求失败'}; message(r.msg,true); return r; }
    finally { busy = false; finishing = false; render(); }
  }
  async function load(map){
    if (!map) return;
    const seq = ++request;
    store.taughtMap = map; store.taught = store.taughtByMap[map] || null;
    pickFirst = null; rangeKey = ''; message(''); render();
    try {
      const r = await Promise.resolve(src.cmd.taughtGet(map));
      // 用户可快速换选地图；迟到的旧请求只能进缓存，不能把当前面板改回上一张图。
      if (seq !== request || map !== store.taughtMap){ if (r?.data && map !== store.taughtMap) storeTaughtDocument(store,r.data); return; }
      if (r?.data) storeTaughtDocument(store,r.data);
      if (r?.ok === false) message(r.msg || '读取示教轨迹失败',true);
    } catch (e){ if (seq === request) message(e.message || '读取失败',true); }
    render();
  }
  function syncRange(){
    const d = doc(), r = range();
    store.taughtRange = mode.value === 'replace_segment' && d ? {map:d.map,...r} : null;
  }
  sel.onchange = () => load(sel.value);
  $('#taughtRefresh').onclick = () => load(store.taughtMap || sel.value);
  mode.onchange = () => { pickFirst = null; syncRange(); render(); };
  $('#taughtRangeStart').oninput = $('#taughtRangeEnd').oninput = () => { syncRange(); render(); };
  $('#taughtPickRange').onclick = () => {
    if (store.taughtMap !== store.mapCurrent) return toast('先加载所选地图，才能在中央地图选择端点','warn');
    pickFirst = null; setTool('taught'); toast('沿原轨迹点选起点，再选终点');
  };
  store.onTaughtPoint = index => {
    if (mode.value !== 'replace_segment' || store.taughtMap !== store.mapCurrent) return;
    if (pickFirst == null){ pickFirst = index; $('#taughtRangeStart').value = index+1; toast(`已选第 ${index+1} 点，再选择终点`); }
    else {
      $('#taughtRangeStart').value = Math.min(pickFirst,index)+1; $('#taughtRangeEnd').value = Math.max(pickFirst,index)+1;
      pickFirst = null; setTool('pan');
    }
    syncRange(); render();
  };
  $('#taughtPrepare').onclick = () => {
    const map = store.taughtMap; if (!map || busy) return;
    formBox({title:'准备示教采集',ok:'开始准备',rows:[['地图',map]],
      body:'趴着或站着都可以准备。趴着时会自动站起，等待站稳后加载地图并定位；自检通过后可开始采集。之后请明确点击「开始采集」，再手动遥控走路线。',
      build:f => {
        const label = document.createElement('label'); label.className = 'modal-chk';
        label.innerHTML = '<input type="checkbox" data-still><span>机器人周围已清空，允许自动站起；准备期间不碰机器人、不操作遥控</span>'; f.appendChild(label);
      },valid:f => f.querySelector('[data-still]').checked,
      onOk:() => command('taughtPrepare',map)});
  };
  $('#taughtCheck').onclick = () => command('taughtCheck',store.taughtMap);
  $('#taughtStart').onclick = () => {
    const d = doc(), why = taughtStartBlocker(store,d,mode.value,range());
    if (why) return toast(why,'warn');
    const p = {map:d.map,mode:mode.value,expected_revision:d.revision,map_token:d.map_token};
    if (mode.value === 'replace_segment') Object.assign(p,{start_index:range().start,end_index:range().end});
    confirmBox({title:'开始示教采集',ok:'开始采集',body:`${d.map} · ${MODES[mode.value]}。${mode.value === 'append' ? '请从旧轨迹末端开始。' : mode.value === 'replace_segment' ? '请从选中起点开始，并走到选中终点。' : '新轨迹校验保存通过后才替换原轨迹。'}开始后请用遥控操作机器人。`,
      onOk:() => command('taughtStart',p)});
  };
  function stopTeleop(){ setTool('pan'); if (src.can('teleop')) src.cmd.teleop(null); }
  async function pause(){
    const c = currentCapture(); if (!c?.session_id) return;
    stopTeleop();
    const r = await command('taughtPause',c.session_id);
    if (store.armed && src.can('arm')) await Promise.resolve(src.cmd.arm(false));
    return r;
  }
  $('#taughtTeleop').onclick = () => setTool('teleop');
  $('#taughtPause').onclick = pause;
  $('#taughtResume').onclick = () => {
    const c = currentCapture(); if (!c?.session_id) return;
    confirmBox({title:'继续采集',ok:'重新核对并继续',body:'保持静止并靠近上次断点（0.2 米内）。后端将重新核对定位与短连接；通过后恢复采样。随后手动进入遥控。',onOk:() => command('taughtResume',c.session_id)});
  };
  $('#taughtFinish').onclick = () => {
    const c = currentCapture(); if (!c?.session_id) return;
    stopTeleop();
    confirmBox({title:'结束并平滑保存',ok:'结束并平滑保存',body:'停止采样后自动加密并平滑路线，再检查完整机器人足迹、连续性与地图版本。通过后更新参考轨迹并显示处理结果；未通过会保留草稿。',onOk:() => command('taughtFinish',c.session_id)});
  };
  $('#taughtAbandon').onclick = () => {
    const c = currentCapture(); if (!c?.session_id) return;
    stopTeleop();
    confirmBox({title:'放弃本次采集草稿？',ok:'放弃草稿',danger:true,body:'本次草稿不再用于更新参考轨迹。已经保存的参考轨迹保留。',onOk:() => { stopTeleop(); command('taughtAbandon',c.session_id); }});
  };

  function enter(){
    const map = store.taughtMap || store.mapCurrent || store.maps[0]?.name;
    if (map) load(map);
  }
  function leave(next){
    const c = currentCapture();
    if (busy || c?.state === 'processing'){ toast('示教操作正在处理，请等结果后切换','warn'); return false; }
    if (!store.link.ok){ stopTeleop(); return true; } // 所有者 WS 断线由后端中断采集；重连后只读状态，不自动恢复。
    if (c?.state === 'recording'){
      stopTeleop();
      confirmBox({title:'暂停采集后切换任务？',ok:'暂停并切换',body:'将停止遥控、上锁并暂停采样。草稿保留，回到示教后需人工恢复。',onOk:async () => { const r = await pause(); if (r?.ok) next(); }});
      return false;
    }
    stopTeleop(); return true;
  }
  function render(){
    const names = store.maps.map(m=>m.name), key = names.join('\n');
    if (key !== mapsKey){
      mapsKey = key; const keep = store.taughtMap || sel.value || store.mapCurrent;
      sel.replaceChildren(...names.map(n=>new Option(n,n)));
      if (!names.length) sel.appendChild(new Option('没有已保存的地图',''));
      if (names.includes(keep)) sel.value = keep;
    }
    if (!store.taughtMap && names.length){ store.taughtMap = names.includes(store.mapCurrent) ? store.mapCurrent : names[0]; sel.value = store.taughtMap; }
    if (store.taughtMap && sel.value !== store.taughtMap && names.includes(store.taughtMap)) sel.value = store.taughtMap;
    const d = doc(), c = d?.capture, open = captureIsOpen(c), recording = c?.state === 'recording', processing = c?.state === 'processing';
    const reconnected = store.link.ok && !wasLinked; wasLinked = store.link.ok;
    if (store.uiTask === 'taught' && reconnected && store.taughtMap){ const map = store.taughtMap; queueMicrotask(()=>load(map)); }
    const link = !!store.link.ok, ready = d?.check?.ok === true && d.check.state === 'ready';
    const checkBusy = ['preparing','standing','settling','loading','localizing'].includes(d?.check?.state);
    const stateText = !link && recording ? '连接中断，等待核对' : c && open ? CAPTURE_STATE[c.state] : d ? d.state === 'ready' ? '参考可用' : d.state === 'invalid' ? '参考无效' : '尚无轨迹' : '未读取';
    const pill = $('#taughtState'); pill.textContent = stateText; pill.dataset.s = !link || c?.state === 'failed' || d?.state === 'invalid' ? 'warn' : recording ? 'run' : ready ? 'ok' : '';
    $('#taughtMapHint').textContent = !names.length ? '先在建图任务中保存地图，再回来采集。' : store.taughtMap !== store.mapCurrent ? `机器人当前使用 ${store.mapCurrent || '（未选图）'}；点击「准备示教」后再采集。` : `中央地图：${store.mapCurrent}${store.source === 'mock' ? '（纯前端模拟）' : ''}`;
    $('#taughtReference').textContent = d ? `${d.points?.length || 0} 点 / ${Number(d.length || 0).toFixed(2)} m` : '--';
    $('#taughtSamples').textContent = c ? `${c.sample_count ?? c.points?.length ?? 0} 点 / ${Number(c.length || 0).toFixed(2)} m` : '0 点 / 0.00 m';
    const summary = taughtProcessingSummary(d?.processing), processingNote = $('#taughtProcessing');
    processingNote.hidden = !finishing && !processing && !summary;
    processingNote.textContent = finishing || processing ? '正在加密采样、平滑路线并检查完整机器人足迹，请等处理结果。' : summary ? `已保存参考的处理结果：${summary}` : '';
    processingNote.setAttribute('aria-busy',String(finishing || processing));
    $('#taughtCheckNote').textContent = !link ? '连接断开。采样不会自动恢复；重连后读取状态并人工继续。' : d?.check?.message || CHECK_STATE[d?.check?.state || 'idle'];
    $('#taughtCheckNote').classList.toggle('warn',!link || d?.check?.state === 'failed');
    const checks = Array.isArray(d?.check?.checks) ? d.check.checks : [], ck = JSON.stringify(checks);
    if (ck !== checksKey){ checksKey = ck; $('#taughtChecks').replaceChildren(...checks.map(item=>{
      const li = document.createElement('li'), status = item.ok === true ? '通过' : item.ok === false ? '未通过' : '待确认';
      li.dataset.s = item.ok === true ? 'ok' : 'warn'; li.textContent = `${status} · ${item.label || item.name || item.key || '检查项'}${item.message || item.msg ? `：${item.message || item.msg}` : ''}`; return li;
    })); }
    const rk = d ? `${d.map}:${d.revision}:${d.points?.length || 0}` : '';
    if (rk !== rangeKey){ rangeKey = rk; const n = d?.points?.length || 0;
      $('#taughtRangeStart').max = Math.max(1,n-1); $('#taughtRangeEnd').max = Math.max(2,n);
      $('#taughtRangeStart').value = 1; $('#taughtRangeEnd').value = Math.max(2,n); syncRange(); }
    $('#taughtRangeRow').hidden = mode.value !== 'replace_segment';
    $('#taughtModeHint').textContent = mode.value === 'append' ? '从旧末端接入，原参考保留；新采样通过检查后追加。' : mode.value === 'replace_segment' ? '采集起止位置都需要靠近选定端点；后端保护区间外的参考。' : '旧参考一直保留，直到新轨迹校验保存通过。';
    const why = taughtStartBlocker(store,d,mode.value,range());
    $('#taughtStart').disabled = busy || !!why; $('#taughtStartBlocker').textContent = busy ? '正在处理示教请求…' : why;
    const locked = busy || open;
    sel.disabled = locked || !names.length; mode.disabled = locked;
    $('#taughtRefresh').disabled = busy || !link || !store.taughtMap;
    // 中断后导航栈可能也已停止；同图草稿允许重新加载定位，仍保持 session/选图/模式锁，绝不新建或自动恢复采样。
    const recoverPrepare = ['paused','interrupted'].includes(c?.state) && c.map === store.taughtMap;
    $('#taughtPrepare').disabled = busy || !link || !store.taughtMap || (open && !recoverPrepare) || checkBusy;
    $('#taughtCheck').disabled = busy || !link || !d || recording || processing || checkBusy;
    for (const id of ['taughtRangeStart','taughtRangeEnd','taughtPickRange']) $('#'+id).disabled = locked || !d?.points?.length;
    $('#taughtCapture').hidden = !c;
    if (c){
      $('#taughtCaptureTitle').textContent = `${CAPTURE_STATE[c.state] || c.state} · ${MODES[c.mode] || ''}`;
      $('#taughtCaptureId').textContent = c.session_id ? c.session_id.slice(-8) : '';
      $('#taughtCaptureNote').textContent = c.message || (recording ? '先点击「键盘遥控」再走路线。暂停与断线均保留原始断口。' : '草稿已保留。继续前保持静止、回到断点附近，并重新核对定位。');
      $('#taughtTeleop').disabled = busy || !link || !recording;
      $('#taughtPause').hidden = !recording; $('#taughtPause').disabled = busy || !link;
      $('#taughtResume').hidden = !['paused','interrupted'].includes(c.state);
      // can_resume 来自后端的断点/版本检查；缺失时不猜测可恢复。点击后后端还会再次自检。
      $('#taughtResume').disabled = busy || !link || c.can_resume !== true;
      $('#taughtFinish').disabled = busy || !link || !open || processing || !(c.sample_count ?? c.points?.length);
      $('#taughtAbandon').disabled = busy || !link || !open || processing;
      // 校验/CAS 失败的原始点仍可离线修复；恢复入口按草稿状态显式判断，不依赖采样会话是否仍开放。
      $('#taughtRecover').href = editorUrl(d.map,c.session_id);
      $('#taughtRecover').hidden = store.source === 'mock' || !c.session_id || !['paused','interrupted','failed'].includes(c.state);
    }
    $('#taughtEditorLink').href = editorUrl(store.taughtMap);
    $('#taughtEditorLink').setAttribute('aria-disabled',!store.taughtMap ? 'true' : 'false');
    if (d?.message && !$('#taughtMessage').textContent) message(d.message,d.state === 'invalid');
  }
  window.addEventListener('beforeunload',e => { if (currentCapture()?.state === 'recording'){ e.preventDefault(); e.returnValue = ''; } });
  return {render,enter,leave};
}
