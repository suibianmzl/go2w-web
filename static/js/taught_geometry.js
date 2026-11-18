// 主工作区与离线编辑器共享的纯几何；这里只做显示/编辑，不替代后端的足迹、平滑和接入校验。
export const captureIsOpen = c => !!c && ['recording','paused','interrupted','processing','failed'].includes(c.state);

export function storeTaughtDocument(store, doc){
  if (!doc || typeof doc.map !== 'string' || !doc.map) return;
  const old = store.taughtByMap[doc.map];
  if (old?.map_token === doc.map_token && old.revision > doc.revision) return;
  store.taughtByMap[doc.map] = doc;
  if (doc.map === (store.taughtMap || store.mapCurrent)) store.taught = doc;
}

export function cleanTaughtPoints(points){
  if (!Array.isArray(points)) throw new Error('轨迹点必须是数组');
  if (points.length > 200000) throw new Error('轨迹点过多，请分段处理');
  return points.map((p, i) => {
    if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) throw new Error(`第 ${i+1} 点坐标无效`);
    if (p.yaw != null && !Number.isFinite(p.yaw)) throw new Error(`第 ${i+1} 点朝向无效`);
    return { x:p.x,y:p.y,yaw:p.yaw ?? 0,s:Number.isFinite(p.s) ? p.s : 0,continuous_id:p.continuous_id ?? 0 };
  });
}

export function continuousRuns(points = []){
  const runs = []; let run = null, last = null;
  for (const p of points){
    if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)){ run = null; last = null; continue; }
    if (!run || p.continuous_id !== last.continuous_id){ run = []; runs.push(run); }
    run.push(p); last = p;
  }
  return runs;
}

export function taughtLength(points = []){
  let length = 0;
  for (const run of continuousRuns(points)) for (let i=1;i<run.length;i++) length += Math.hypot(run[i].x-run[i-1].x,run[i].y-run[i-1].y);
  return length;
}

export function rebuildTaughtPoints(points){
  const out = cleanTaughtPoints(points); let s = 0;
  for (let i=0;i<out.length;i++){
    const p = out[i], prev = out[i-1], next = out[i+1];
    if (prev && prev.continuous_id === p.continuous_id) s += Math.hypot(p.x-prev.x,p.y-prev.y);
    if (next && next.continuous_id === p.continuous_id) p.yaw = Math.atan2(next.y-p.y,next.x-p.x);
    else if (prev && prev.continuous_id === p.continuous_id) p.yaw = prev.yaw;
    p.s = s;
  }
  return out;
}

// 显示旋转只作用在相机上：先把世界点减去视角中心，再旋转 angle，最后将 y 向上转换为像素 y 向下。
// 保存的 x/y/yaw 始终处于 map 世界坐标；绝不能把屏幕旋转后的点写回路线文件。
export function taughtWorldToScreen(p, width, height, view, angle = 0){
  const c = Math.cos(angle), s = Math.sin(angle), dx = p.x-view.cx, dy = p.y-view.cy;
  return { x:width/2+(c*dx-s*dy)*view.s,y:height/2-(s*dx+c*dy)*view.s };
}

export function taughtScreenToWorld(px, py, width, height, view, angle = 0){
  const c = Math.cos(angle), s = Math.sin(angle), x = (px-width/2)/view.s, y = -(py-height/2)/view.s;
  return { x:view.cx+c*x+s*y,y:view.cy-s*x+c*y };
}

export function taughtInMap(p, g){
  if (!g || !(g.res > 0)) return false;
  // origin.yaw 是栅格自身在 map 中的方向，与用户旋转画面的 angle 是两个独立角度。
  // 先平移回栅格原点，再用 -origin.yaw 反旋转，才能判断旋转地图的真实边界。
  const c = Math.cos(g.oyaw || 0), s = Math.sin(g.oyaw || 0), dx = p.x-g.ox, dy = p.y-g.oy;
  const x = (c*dx+s*dy)/g.res, y = (-s*dx+c*dy)/g.res;
  return x >= 0 && y >= 0 && x < g.w && y < g.h;
}

export function readTaughtGridFrame(buffer){
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 5) throw new Error('地图二进制数据不完整');
  const dv = new DataView(buffer), length = dv.getUint32(1,true);
  if (dv.getUint8(0) !== 1 || !length || length > 65536 || 5+length >= buffer.byteLength) throw new Error('地图数据头无效');
  let meta;
  try { meta = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,5,length))); }
  catch { throw new Error('地图元数据无法解析'); }
  if (!meta || !Number.isInteger(meta.w) || !Number.isInteger(meta.h) || meta.w < 1 || meta.h < 1 || meta.w*meta.h > 20000000 ||
      !Number.isFinite(meta.res) || meta.res <= 0 || !Number.isFinite(meta.ox) || !Number.isFinite(meta.oy) || !Number.isFinite(meta.oyaw ?? 0)) throw new Error('地图尺寸、分辨率或原点无效');
  return {meta:{...meta,oyaw:meta.oyaw ?? 0},payload:new Uint8Array(buffer,5+length)};
}

export function createTaughtHistory(initial = []){
  let steps = [cleanTaughtPoints(initial)], at = 0;
  const copy = p => p.map(v => ({...v}));
  return {
    get points(){ return copy(steps[at]); },
    get canUndo(){ return at > 0; }, get canRedo(){ return at < steps.length-1; },
    reset(points){ steps = [cleanTaughtPoints(points)]; at = 0; return this.points; },
    commit(points){ const p = cleanTaughtPoints(points); if (JSON.stringify(p) === JSON.stringify(steps[at])) return this.points;
      steps = steps.slice(0,at+1); steps.push(p); if (steps.length > 80) steps.shift(); at = steps.length-1; return this.points; },
    undo(){ if (at > 0) at--; return this.points; }, redo(){ if (at < steps.length-1) at++; return this.points; },
  };
}

export function taughtStartBlocker(store, doc, mode, range = {}){
  if (!store.link?.ok) return '没有连上后端';
  if (store.estop) return '急停中，请先处理急停';
  if (store.profiles?.mapping !== 'stop') return '先停止建图再采集';
  if (!doc?.map || doc.map !== store.mapCurrent) return '先加载所选地图并定位';
  if (captureIsOpen(doc.capture)) return '已有采集草稿，请继续、结束或放弃它';
  if (store.task?.type !== 'idle' && store.task?.status === 'active') return '先取消正在运行的导航任务';
  // 自检唯一权威是后端。未知、缺失和仅有 state=ready 都不能当作通过，前端不凭 AMCL 数字补造 ok。
  if (doc.check?.ok !== true || doc.check?.state !== 'ready') return '先完成定位自检；未知项不算通过';
  if (!doc.map_token || !Number.isInteger(doc.revision) || doc.revision < 0) return '地图版本信息不完整，请重新读取';
  if (!['replace','append','replace_segment'].includes(mode)) return '请选择采集方式';
  if (mode !== 'replace' && (doc.points?.length || 0) < 2) return '这张地图还没有可接入的参考轨迹';
  if (mode !== 'replace' && (doc.state !== 'ready' || doc.executable !== true)) return '当前参考未通过校验，请整条替换或在编辑器修复';
  if (mode === 'replace_segment' && (!Number.isInteger(range.start) || !Number.isInteger(range.end) || range.start < 0 || range.end >= doc.points.length || range.start >= range.end)) return '请选择有效区间：起点序号必须小于终点';
  return '';
}
