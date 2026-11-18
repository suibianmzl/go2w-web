// 独立离线编辑器：只使用 /api/taught HTTP 文件接口。这里不导入实时 source/actions，不建立 WS，
// 所以打开地图 B、旋转画面或拖动路线点都不可能改变机器人正在使用的地图 A。
import { $, $$, clamp, gridToCanvas } from './util.js';
import { confirmBox } from './ui.js';
import { cleanTaughtPoints, rebuildTaughtPoints, continuousRuns, taughtLength, createTaughtHistory,
  taughtWorldToScreen, taughtScreenToWorld, taughtInMap, readTaughtGridFrame } from './taught_geometry.js';

const params = new URLSearchParams(location.search), initialMap = params.get('map'), recoveryId = params.get('capture');
const cv = $('#editorCanvas'), ctx = cv.getContext('2d'), stage = $('#editorStage'), mapSel = $('#editorMap');
const history = createTaughtHistory();
const view = {cx:0,cy:0,s:40};
let mapName = '', doc = null, asset = null, points = [], selected = new Set(), anchor = null, tool = 'select';
let baseline = '[]', angle = 0, loading = false, saving = false, version = 0, abort = null, drag = null;
let W = 1, H = 1, DPR = 1, frame = false, pointListKey = '';
const names = [];
const dirty = () => JSON.stringify(points) !== baseline;
const current = () => selected.size ? selected.values().next().value : -1;
const canEdit = () => !!asset && !!doc && !loading && !saving;
const apiPath = name => `/api/taught/maps/${encodeURIComponent(name)}`;
const screen = p => taughtWorldToScreen(p,W,H,view,angle);
const world = (x,y) => taughtScreenToWorld(x,y,W,H,view,angle);
const local = e => { const r = cv.getBoundingClientRect(); return {x:e.clientX-r.left,y:e.clientY-r.top}; };

function message(text, state = ''){ const el = $('#editorMessage'); el.textContent = text; el.dataset.s = state; }
async function jsonResponse(response){
  let value;
  try { value = await response.json(); } catch { throw new Error(`服务返回的数据无法解析（HTTP ${response.status}）`); }
  if (!response.ok && value?.ok !== false) throw new Error(value?.msg || `请求失败（HTTP ${response.status}）`);
  return value;
}
async function loadGrid(response){
  if (!response.ok) throw new Error(`底图读取失败（HTTP ${response.status}）`);
  const {meta,payload} = readTaughtGridFrame(await response.arrayBuffer());
  if (typeof DecompressionStream === 'undefined') throw new Error('浏览器不支持底图解压，请使用新版 Chrome');
  let grid;
  try {
    const stream = new Blob([payload]).stream().pipeThrough(new DecompressionStream('deflate'));
    grid = new Uint8Array(await new Response(stream).arrayBuffer());
  } catch { throw new Error('底图压缩数据损坏，请重新读取'); }
  if (grid.length !== meta.w*meta.h) throw new Error('底图格子数量与尺寸不一致');
  return {...meta,grid,img:gridToCanvas(grid,meta.w,meta.h)};
}
function validateDoc(value, expectedMap){
  if (!value || value.ok === false) throw new Error(value?.msg || '示教文档读取失败');
  if (value.map !== expectedMap) throw new Error('返回的地图与选择不符，请重新读取');
  if (!Number.isInteger(value.revision) || value.revision < 0 || typeof value.map_token !== 'string' || !value.map_token) throw new Error('地图版本信息缺失，无法安全保存');
  cleanTaughtPoints(value.points);
  return value;
}
function askDiscard(next){
  if (!dirty()) return next();
  confirmBox({title:'放弃未保存的路线修改？',body:'当前修改还没有写入地图文件。可以先取消并保存，或导出草稿后再切换。',ok:'放弃修改并读取',danger:true,onOk:next});
}

async function loadMap(name, {recover = false} = {}){
  if (!name || saving) return;
  abort?.abort(); abort = new AbortController(); const seq = ++version;
  loading = true; mapName = name; mapSel.value = name; doc = null; asset = null;
  points = history.reset([]); baseline = '[]'; selected.clear(); anchor = null; drag = null; angle = 0;
  message('正在读取地图与参考轨迹…'); render();
  try {
    const suffix = recover && recoveryId ? `?capture=${encodeURIComponent(recoveryId)}` : '';
    // 文档与栅格互相独立地读取；全部验证成功后一次性切换，防止旧底图叠上新轨迹。
    const [value,grid] = await Promise.all([
      fetch(apiPath(name)+suffix,{signal:abort.signal,cache:'no-store'}).then(jsonResponse),
      fetch(apiPath(name)+'/grid',{signal:abort.signal,cache:'no-store'}).then(loadGrid),
    ]);
    if (seq !== version) return;
    doc = validateDoc(value,name); asset = grid;
    points = history.reset(doc.points); baseline = JSON.stringify(points);
    if (recover && Array.isArray(value.draft_points) && value.draft_points.length){
      points = history.commit(cleanTaughtPoints(value.draft_points));
      message(`已恢复 ${points.length} 个草稿点。编辑完成后需重新校验保存，已保存参考仍保留。`);
    } else message(points.length ? '参考轨迹已读取。选择点即可拖动，或填写坐标。' : '这张地图尚无示教轨迹。选择「绘制 / 加点」，沿可通行区域点出路线。');
    selected = points.length ? new Set([0]) : new Set(); anchor = points.length ? 0 : null;
    tool = points.length ? 'select' : 'add'; fit();
    const url = new URL(location.href); url.searchParams.set('map',name);
    if (!recover) url.searchParams.delete('capture');
    window.history.replaceState(null,'',url);
  } catch(e){
    if (seq !== version || e.name === 'AbortError') return;
    doc = null; asset = null; message(e.message || '地图读取失败','error');
  } finally { if (seq === version){ loading = false; render(); } }
}

function resize(){
  const r = stage.getBoundingClientRect(); W = Math.max(1,r.width); H = Math.max(1,r.height); DPR = window.devicePixelRatio || 1;
  cv.width = Math.round(W*DPR); cv.height = Math.round(H*DPR); queueDraw();
}
function fit(){
  if (!asset) return;
  const g = asset, c = Math.cos(g.oyaw), s = Math.sin(g.oyaw), ca = Math.cos(angle), sa = Math.sin(angle);
  const corners = [[0,0],[g.w*g.res,0],[0,g.h*g.res],[g.w*g.res,g.h*g.res]].map(([x,y])=>{
    const wx = g.ox+c*x-s*y, wy = g.oy+s*x+c*y;
    return {x:ca*wx-sa*wy,y:sa*wx+ca*wy};
  });
  const xs = corners.map(p=>p.x), ys = corners.map(p=>p.y), left = Math.min(...xs), right = Math.max(...xs), bottom = Math.min(...ys), top = Math.max(...ys);
  // 上下预留给地图标题与工具提示；相机中心仍用世界坐标保存，显示角度改变不改点数据。
  view.s = Math.max(.01,Math.min((W-50)/Math.max(.1,right-left),(H-170)/Math.max(.1,top-bottom))*.94);
  const x = (left+right)/2, y = (bottom+top)/2;
  view.cx = ca*x+sa*y; view.cy = -sa*x+ca*y; queueDraw();
}
function setAngle(value){ angle = Math.atan2(Math.sin(value),Math.cos(value)); fit(); render(); }
function queueDraw(){ if (!frame){ frame = true; requestAnimationFrame(()=>{ frame = false; draw(); }); } }
function drawRoute(list,color,dash,width){
  ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash); ctx.lineJoin = ctx.lineCap = 'round';
  for (const run of continuousRuns(list)){
    ctx.beginPath(); run.forEach((p,i)=>{ const a=screen(p); i ? ctx.lineTo(a.x,a.y) : ctx.moveTo(a.x,a.y); }); ctx.stroke();
    if (run.length === 1){ const a=screen(run[0]); ctx.beginPath(); ctx.arc(a.x,a.y,3,0,Math.PI*2); ctx.fill(); }
  }
  ctx.setLineDash([]);
}
function draw(){
  ctx.setTransform(DPR,0,0,DPR,0,0); ctx.clearRect(0,0,W,H);
  if (!asset) return;
  const g = asset, origin = screen({x:g.ox,y:g.oy});
  // OccupancyGrid 的第 0 行贴近 origin，而 canvas 图片第 0 行在上方；gridToCanvas 已翻行。
  // 原点平移后先旋转 -(origin.yaw + 显示角)，再向负像素 y 方向画，才能与 map 世界点严格重合。
  ctx.save(); ctx.translate(origin.x,origin.y); ctx.rotate(-(g.oyaw+angle)); ctx.imageSmoothingEnabled = false;
  ctx.drawImage(g.img,0,-g.h*g.res*view.s,g.w*g.res*view.s,g.h*g.res*view.s); ctx.restore();
  if ($('#editorShowReference').checked) drawRoute(doc.points,'#f3b13f',[7,5],2);
  drawRoute(points,'#38e1dc',[],2.7);
  const step = points.length > 1600 ? Math.ceil(points.length/1600) : 1;
  for (let i=0;i<points.length;i++){
    const isSelected = selected.has(i); if (i%step && !isSelected && i !== points.length-1) continue;
    const p = screen(points[i]); if (p.x < -12 || p.y < -12 || p.x > W+12 || p.y > H+12) continue;
    ctx.beginPath(); ctx.fillStyle = isSelected ? '#ffe4a6' : '#0b282e'; ctx.strokeStyle = isSelected ? '#ffe4a6' : '#38e1dc'; ctx.lineWidth = 1.5;
    ctx.arc(p.x,p.y,isSelected ? 5.5 : 3.3,0,Math.PI*2); ctx.fill(); ctx.stroke();
    if (isSelected && selected.size <= 12){
      const text = String(i+1); ctx.font = '12px ui-monospace,monospace'; const w = ctx.measureText(text).width+10;
      ctx.fillStyle = '#09101a'; ctx.fillRect(p.x+8,p.y-23,w,18); ctx.fillStyle = '#ffe4a6'; ctx.fillText(text,p.x+13,p.y-10);
    }
  }
}
function choose(index, range = false){
  if (!Number.isInteger(index) || index < 0 || index >= points.length) return;
  if (range && anchor != null){ selected = new Set(); for (let i=Math.min(anchor,index);i<=Math.max(anchor,index);i++) selected.add(i); }
  else { selected = new Set([index]); anchor = index; }
  render();
}
function commit(next, selection = selected){
  points = history.commit(rebuildTaughtPoints(next));
  selected = new Set([...selection].filter(i=>i >= 0 && i < points.length));
  if (!selected.size && points.length) selected.add(Math.min(Math.max(0,anchor || 0),points.length-1));
  anchor = current() < 0 ? null : current(); render();
}
function changeTool(value){ tool = value; render(); cv.focus({preventScroll:true}); }
function insertAt(index, point){
  const next = points.map(p=>({...p})), previous = points[index];
  next.splice(index+1,0,{...point,yaw:previous?.yaw || 0,s:0,continuous_id:previous?.continuous_id ?? 0});
  commit(next,new Set([index+1]));
}
function deleteSelected(){
  if (!canEdit() || !selected.size) return;
  const at = current(), next = points.filter((_,i)=>!selected.has(i));
  commit(next,new Set(next.length ? [Math.min(at,next.length-1)] : []));
}
function nearestPoint(px,py){
  let index = -1, distance = 15;
  points.forEach((p,i)=>{ const a=screen(p), d=Math.hypot(px-a.x,py-a.y); if (d < distance){distance=d;index=i;} });
  return index;
}
function nearestSegment(px,py){
  let index = -1, distance = 22;
  for (let i=0;i<points.length-1;i++){
    if (points[i].continuous_id !== points[i+1].continuous_id) continue;
    const a = screen(points[i]), b = screen(points[i+1]), dx = b.x-a.x, dy = b.y-a.y;
    const t = clamp(((px-a.x)*dx+(py-a.y)*dy)/(dx*dx+dy*dy || 1),0,1), d = Math.hypot(px-a.x-t*dx,py-a.y-t*dy);
    if (d < distance){distance=d;index=i;}
  }
  return index;
}
function translated(source,indices,dx,dy){
  const next = source.map(p=>({...p}));
  for (const i of indices){ next[i].x += dx; next[i].y += dy; if (!taughtInMap(next[i],asset)) return null; }
  return next;
}

function render(){
  const active = canEdit(), index = current(), p = points[index], changed = dirty();
  mapSel.disabled = loading || saving || !names.length;
  $('#editorReload').disabled = loading || saving || !mapName;
  $('#editorNew').disabled = !active;
  $('#editorMeta').textContent = asset && doc ? `${asset.w} × ${asset.h} · ${Number(asset.res.toPrecision(6))} m / 格 · 修订 ${doc.revision}` : loading ? '正在读取…' : '底图尚未加载';
  $('#editorMapTitle').textContent = mapName || '尚未选图';
  $('#editorSummary').textContent = asset ? `${points.length} 点 / ${taughtLength(points).toFixed(2)} m${selected.size ? ` · 选中 ${selected.size} 点` : ''}` : loading ? '正在读取地图…' : '无参考轨迹';
  $('#editorCount').textContent = `${points.length} 点`;
  $('#editorDirty').textContent = changed ? '有未保存修改' : '未修改'; $('#editorDirty').dataset.dirty = String(changed);
  $('#editorUndo').disabled = !active || !history.canUndo; $('#editorRedo').disabled = !active || !history.canRedo;
  $('#editorSave').disabled = !active || !changed || points.length < 2;
  $('#editorSave').textContent = saving ? '正在校验保存…' : doc?.revision ? '校验并保存修订' : '校验并创建参考';
  $('#editorExport').disabled = !points.length || loading;
  for (const id of ['editorIndex','editorX','editorY','editorApply','editorDelete']) $('#'+id).disabled = !active || !p;
  $('#editorPrev').disabled = !active || index <= 0; $('#editorNext').disabled = !active || index < 0 || index >= points.length-1;
  $('#editorInsert').disabled = !active || !p || !points[index+1] || p.continuous_id !== points[index+1].continuous_id;
  $('#editorIndex').max = points.length; $('#editorRangeStart').max = $('#editorRangeEnd').max = points.length;
  for (const id of ['editorRangeStart','editorRangeEnd','editorRangeSelect']) $('#'+id).disabled = !active || !points.length;
  const setValue = (id,value) => { const el = $('#'+id); if (document.activeElement !== el) el.value = value; };
  setValue('editorIndex',index >= 0 ? index+1 : ''); setValue('editorX',p ? +p.x.toFixed(4) : ''); setValue('editorY',p ? +p.y.toFixed(4) : '');
  if (selected.size){ let last = 0; for (const i of selected) last = i; setValue('editorRangeStart',current()+1); setValue('editorRangeEnd',last+1); }
  $$('#editorTools [data-editor-tool]').forEach(b=>{ b.disabled = !active; b.classList.toggle('on',b.dataset.editorTool === tool); b.setAttribute('aria-pressed',String(b.dataset.editorTool === tool)); });
  for (const id of ['editorFit','editorRotateLeft','editorRotateRight','editorRotateReset']) $('#'+id).disabled = !asset || loading;
  $('#editorAngle').textContent = `显示 ${Math.round(angle*180/Math.PI)}°`;
  const hints = {select:'点击选点并拖动 · Shift + 点击选择区间 · 方向键移动选中点',add:'在地图上依次点击绘制 · 新点接在路线末尾',insert:'点击已有连续线段附近插入点',pan:'拖动平移地图 · 滚轮缩放'};
  $('#editorToolHint').textContent = hints[tool]; cv.style.cursor = tool === 'pan' ? 'grab' : tool === 'select' ? 'default' : 'crosshair';
  const empty = $('#editorEmpty'); empty.hidden = !!asset && !!points.length;
  empty.querySelector('b').textContent = loading ? '正在读取地图' : asset ? '在地图上绘制一条路线' : '加载一张已保存地图';
  empty.querySelector('span').textContent = asset ? '点击「绘制 / 加点」，再沿通行区域依次点击。' : '没有示教轨迹时，可以直接绘制并保存。';
  const near = index >= 0 ? Math.max(0,index-3) : 0, list = points.slice(near,near+7);
  const key = JSON.stringify([near,list,[...selected]]);
  if (key !== pointListKey){
    pointListKey = key; $('#editorPoints').replaceChildren(...list.map((point,k)=>{
      const i = near+k, b = document.createElement('button'); b.type = 'button'; b.dataset.point = i;
      b.setAttribute('aria-pressed',String(selected.has(i))); b.disabled = !active;
      const n = document.createElement('span'), xy = document.createElement('span'); n.textContent = `${i+1}`; xy.textContent = `${point.x.toFixed(2)}, ${point.y.toFixed(2)} m`; b.append(n,xy); return b;
    }));
  } else $$('#editorPoints button').forEach(b=>b.disabled = !active);
  queueDraw();
}

mapSel.onchange = () => { const next = mapSel.value; mapSel.value = mapName; if (next !== mapName) askDiscard(()=>loadMap(next)); };
$('#editorReload').onclick = () => askDiscard(()=>loadMap(mapName));
$('#editorNew').onclick = () => {
  const go = () => { commit([],new Set()); changeTool('add'); message('依次点击地图绘制新路线，保存通过后更新参考。原参考可用琥珀虚线对照。'); };
  if (!points.length) return go();
  confirmBox({title:'绘制一条新轨迹？',ok:'开始绘制',body:'清空当前编辑点，从头绘制。原参考文件保留，只有校验保存通过后才会更新；本次清空也可以撤销。',onOk:go});
};
$$('#editorTools [data-editor-tool]').forEach(b=>b.onclick=()=>changeTool(b.dataset.editorTool));
function undo(){ if (!canEdit()) return; points=history.undo(); selected=points.length ? new Set([Math.min(Math.max(0,current()),points.length-1)]) : new Set(); anchor=current(); render(); }
function redo(){ if (!canEdit()) return; points=history.redo(); selected=points.length ? new Set([Math.min(Math.max(0,current()),points.length-1)]) : new Set(); anchor=current(); render(); }
$('#editorUndo').onclick = undo; $('#editorRedo').onclick = redo;
$('#editorPrev').onclick = () => choose(current()-1); $('#editorNext').onclick = () => choose(current()+1);
$('#editorIndex').onchange = e => choose(Number(e.target.value)-1);
$('#editorPoints').onclick = e => {const b=e.target.closest('[data-point]');if(b)choose(+b.dataset.point,e.shiftKey);};
$('#editorApply').onclick = () => {
  if (!canEdit() || current() < 0) return;
  const x = $('#editorX').value, y = $('#editorY').value, p = {x:Number(x),y:Number(y)};
  if (!x.trim() || !y.trim() || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return message('请输入有效的 x、y 坐标。','error');
  if (!taughtInMap(p,asset)) return message('坐标超出当前地图范围，请调整。','error');
  const next = points.map(v=>({...v})); Object.assign(next[current()],p); commit(next); message('坐标已应用到草稿，保存后更新参考。');
};
$('#editorRangeSelect').onclick = () => {
  const a = Number($('#editorRangeStart').value)-1, b = Number($('#editorRangeEnd').value)-1;
  if (!Number.isInteger(a) || !Number.isInteger(b) || a < 0 || b >= points.length || a > b) return message('请输入有效区间，起点序号应小于或等于终点。','error');
  anchor=a; choose(b,true);
};
$('#editorInsert').onclick = () => {
  const i = current(), a=points[i], b=points[i+1]; if (!canEdit() || !a || !b || a.continuous_id !== b.continuous_id) return;
  insertAt(i,{x:(a.x+b.x)/2,y:(a.y+b.y)/2});
};
$('#editorDelete').onclick = deleteSelected;
$('#editorFit').onclick = fit; $('#editorRotateLeft').onclick = () => setAngle(angle+Math.PI/2);
$('#editorRotateRight').onclick = () => setAngle(angle-Math.PI/2); $('#editorRotateReset').onclick = () => setAngle(0);
$('#editorShowReference').onchange = queueDraw;
$('#editorExport').onclick = () => {
  const data = {map:mapName,expected_revision:doc?.revision,map_token:doc?.map_token,points,source:'offline_editor_draft'};
  const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})), link=document.createElement('a');
  link.href=url;link.download=`${mapName || 'taught'}-draft.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
$('#editorSave').onclick = async () => {
  if (!canEdit() || !dirty() || points.length < 2) return;
  const name = mapName, seq = version, snapshot = cleanTaughtPoints(points), controller = new AbortController();
  const timer = setTimeout(()=>controller.abort(),125000); saving=true;message('正在校验完整路线，期间请保留此页…');render();
  try {
    const r = await fetch(apiPath(name),{method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,
      body:JSON.stringify({expected_revision:doc.revision,map_token:doc.map_token,points:snapshot})}).then(jsonResponse);
    if (seq !== version || name !== mapName) return;
    // 版本冲突、碰撞或服务异常均不得重置 history/baseline，用户草稿和撤销链原样保留。
    if (r?.ok !== true) return message(`${r?.msg || '保存未通过'}。当前草稿已保留；可继续修改、导出，或核对最新版本后重新读取。`,'error');
    const updated = validateDoc(r.data,name);
    doc=updated;points=history.reset(updated.points);baseline=JSON.stringify(points);
    selected=points.length ? new Set([0]) : new Set();anchor=current();
    const url=new URL(location.href);url.searchParams.delete('capture');window.history.replaceState(null,'',url);
    message(`已保存 ${name} 的参考轨迹（修订 ${doc.revision}，${points.length} 点 / ${Number(doc.length || taughtLength(points)).toFixed(2)} m）。`,'ok');
  } catch(e){message(`${e.name === 'AbortError' ? '等待保存结果超时，请重新读取核对实际版本' : e.message || '保存失败'}。当前草稿已保留。`,'error');}
  finally{clearTimeout(timer);saving=false;render();}
};

cv.addEventListener('contextmenu',e=>e.preventDefault());
cv.addEventListener('pointerdown',e=>{
  if (!canEdit() || ![0,1,2].includes(e.button)) return;
  e.preventDefault();cv.focus({preventScroll:true});cv.setPointerCapture(e.pointerId);
  const p=local(e), at=world(p.x,p.y);
  if (tool === 'pan' || e.button !== 0){drag={kind:'pan',start:p,cx:view.cx,cy:view.cy};cv.style.cursor='grabbing';return;}
  if (tool === 'add'){
    if (!taughtInMap(at,asset)) return message('请在当前地图范围内绘制。','error');
    if (points.length && Math.hypot(at.x-points.at(-1).x,at.y-points.at(-1).y) < .005) return;
    insertAt(points.length-1,at);return;
  }
  if (tool === 'insert'){
    const i=nearestSegment(p.x,p.y);if(i<0)return message('请点击已有连续线段附近；断口处不能插入伪造连接。','error');
    if(!taughtInMap(at,asset))return;insertAt(i,at);return;
  }
  const i=nearestPoint(p.x,p.y);
  if(i<0){drag={kind:'pan',start:p,cx:view.cx,cy:view.cy};cv.style.cursor='grabbing';return;}
  if(e.shiftKey){choose(i,true);return;}
  if(!selected.has(i))choose(i);
  drag={kind:'point',start:at,before:points.map(v=>({...v})),indices:[...selected],moved:false};
});
cv.addEventListener('pointermove',e=>{
  const p=local(e),at=world(p.x,p.y);$('#editorCursor').textContent=`x ${at.x.toFixed(2)} / y ${at.y.toFixed(2)} m`;
  if(!drag)return;
  if(drag.kind==='pan'){
    const dx=(p.x-drag.start.x)/view.s,dy=-(p.y-drag.start.y)/view.s,c=Math.cos(angle),s=Math.sin(angle);
    view.cx=drag.cx-c*dx-s*dy;view.cy=drag.cy+s*dx-c*dy;queueDraw();return;
  }
  const next=translated(drag.before,drag.indices,at.x-drag.start.x,at.y-drag.start.y);
  if(next){points=next;drag.moved=true;queueDraw();}
});
function endDrag(cancel = false){
  if(!drag)return;const d=drag;drag=null;
  if(d.kind==='point'){if(cancel)points=d.before;else if(d.moved)points=history.commit(rebuildTaughtPoints(points));}
  render();
}
cv.addEventListener('pointerup',()=>endDrag());cv.addEventListener('pointercancel',()=>endDrag(true));
cv.addEventListener('lostpointercapture',()=>endDrag());
cv.addEventListener('wheel',e=>{
  if(!asset)return;e.preventDefault();const p=local(e),before=world(p.x,p.y);
  view.s=clamp(view.s*Math.exp(-e.deltaY*.0015),.1,1200);const after=world(p.x,p.y);
  view.cx+=before.x-after.x;view.cy+=before.y-after.y;queueDraw();
},{passive:false});
window.addEventListener('keydown',e=>{
  if(e.target.closest('input,select,textarea') || e.target.closest('.scrim'))return;
  const k=e.key.toLowerCase(),mod=e.ctrlKey||e.metaKey;
  if(mod&&k==='z'){e.preventDefault();e.shiftKey?redo():undo();return;}
  if(mod&&k==='y'){e.preventDefault();redo();return;}
  if(!canEdit())return;
  if(k==='delete'||k==='backspace'){e.preventDefault();deleteSelected();return;}
  if(k==='escape'){endDrag(true);changeTool('select');return;}
  if(mod&&k==='a'){e.preventDefault();selected=new Set(points.map((_,i)=>i));anchor=0;render();return;}
  const direction={arrowleft:[-1,0],arrowright:[1,0],arrowup:[0,-1],arrowdown:[0,1]}[k];
  if(direction && selected.size){
    e.preventDefault();const step=e.shiftKey?.01:.05,at=world(W/2+direction[0]*step*view.s,H/2+direction[1]*step*view.s);
    const next=translated(points,selected,at.x-view.cx,at.y-view.cy);if(next)commit(next);else message('选中点将超出地图边界，请换个方向。','error');
  }
});
window.addEventListener('beforeunload',e=>{if(dirty()||saving){e.preventDefault();e.returnValue='';}});
new ResizeObserver(()=>{resize();if(asset)fit();}).observe(stage);
resize();render();
try{
  const result=await fetch('/api/taught/maps',{cache:'no-store'}).then(jsonResponse);
  if(!Array.isArray(result?.maps))throw new Error('地图列表格式无效');
  for(const map of result.maps){const name=typeof map==='string'?map:map?.name;if(typeof name==='string'&&name&&!names.includes(name))names.push(name);}
  mapSel.replaceChildren(...names.map(n=>new Option(n,n)));
  if(!names.length){mapSel.appendChild(new Option('没有已保存的地图',''));message('data/maps 中没有可编辑地图。先在导航控制台保存地图，再重新打开此页。');render();}
  else if(initialMap&&!names.includes(initialMap)){mapSel.insertBefore(new Option('请选择地图',''),mapSel.firstChild);mapSel.value='';message(`没有找到地图 ${initialMap}，请从列表重新选择。`,'error');render();}
  else await loadMap(initialMap || names[0],{recover:!!recoveryId});
}catch(e){message(e.message || '无法读取地图列表','error');mapSel.replaceChildren(new Option('地图列表不可用',''));render();}
