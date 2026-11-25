// 禁行区（v1.1.8，11-23）：禁行线 / 禁行区域的几何和绘制。形状是世界坐标（map 坐标系，米）：
//   { kind: 'line' | 'zone', pts: [[x, y], ...], name? }
// 后端 server/keepout.py 按同样的形状渲染 KeepoutFilter 掩膜；BAND 和那边的 INSCRIBED 一致：
// 离线这么近的地方代价已经接近障碍，地图上画成半透明的带子，让人看出狗实际要离多远
export const BAND = 0.26;
export const COL = '#ff4b5c';

export function segments(s){
  const p = s.pts, out = [];
  for (let i = 0; i + 1 < p.length; i++) out.push([p[i], p[i + 1]]);
  if (s.kind === 'zone' && p.length > 2) out.push([p[p.length - 1], p[0]]);
  return out;
}
export function segDist(x, y, [ax, ay], [bx, by]){
  const dx = bx - ax, dy = by - ay, L2 = dx*dx + dy*dy;
  const t = L2 > 0 ? Math.min(1, Math.max(0, ((x - ax)*dx + (y - ay)*dy) / L2)) : 0;
  return Math.hypot(x - ax - t*dx, y - ay - t*dy);
}
export function inside(pts, x, y){
  let c = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++){
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && x < xi + (y - yi) * (xj - xi) / (yj - yi)) c = !c;
  }
  return c;
}
// (x, y) 离哪个形状最近（r 米以内，区域内部算 0）→ 下标，没有 → -1
export function pick(shapes, x, y, r){
  let best = -1, bd = r;
  shapes.forEach((s, k) => {
    let d = s.kind === 'zone' && inside(s.pts, x, y) ? 0 : Infinity;
    for (const [a, b] of segments(s)) d = Math.min(d, segDist(x, y, a, b));
    if (d <= bd){ bd = d; best = k; }
  });
  return best;
}
export function length(s){ return segments(s).reduce((t, [a, b]) => t + Math.hypot(b[0] - a[0], b[1] - a[1]), 0); }
export function area(pts){
  let a = 0; for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) a += (pts[j][0] + pts[i][0]) * (pts[j][1] - pts[i][1]);
  return Math.abs(a) / 2;
}
export function describe(s, k){
  const what = s.kind === 'line' ? '禁行线' : '禁行区域';
  const size = s.kind === 'line' ? `${length(s).toFixed(1)} m` : `${area(s.pts).toFixed(1)} m²`;
  return { title: s.name || `${what} ${k + 1}`, size, what };
}
// 落在禁行范围里的点：后端出发前也会查（keepout_blocker），这里给网页提前提示用
export function blocked(shapes, x, y){ return shapes?.length ? pick(shapes, x, y, BAND) : -1; }

// 在地图坐标系（ctx 已经按显示方向旋转好）里画。hi：高亮的下标；scale：像素 / 米
export function draw(ctx, sx, sy, shapes, scale, { hi = -1, dim = false } = {}){
  if (!shapes?.length) return;
  ctx.save(); ctx.lineJoin = 'round'; ctx.lineCap = 'round';
  shapes.forEach((s, k) => {
    const on = k === hi, path = () => {
      ctx.beginPath(); s.pts.forEach(([x, y], i) => i ? ctx.lineTo(sx(x), sy(y)) : ctx.moveTo(sx(x), sy(y)));
      if (s.kind === 'zone') ctx.closePath();
    };
    path();
    if (s.kind === 'zone'){ ctx.fillStyle = `rgba(255,75,92,${dim ? .1 : on ? .32 : .2})`; ctx.fill(); }
    ctx.strokeStyle = `rgba(255,75,92,${dim ? .08 : on ? .3 : .16})`; ctx.lineWidth = Math.max(4, 2 * BAND * scale); ctx.stroke();
    ctx.strokeStyle = on ? '#ffd0d5' : COL; ctx.globalAlpha = dim ? .5 : 1; ctx.lineWidth = on ? 3.5 : 2.5;
    ctx.setLineDash(s.kind === 'zone' ? [] : [10, 5]); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
  });
  ctx.restore();
}
// 正在画的那条：已点的点 + 跟着鼠标的一段
export function drawDraft(ctx, sx, sy, draft, hover){
  if (!draft?.pts.length) return;
  const pts = hover ? [...draft.pts, [hover.x, hover.y]] : draft.pts;
  ctx.save(); ctx.strokeStyle = '#ffe08a'; ctx.fillStyle = '#ffe08a'; ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
  ctx.beginPath(); pts.forEach(([x, y], i) => i ? ctx.lineTo(sx(x), sy(y)) : ctx.moveTo(sx(x), sy(y)));
  if (draft.kind === 'zone' && pts.length > 2) ctx.closePath();
  ctx.stroke(); ctx.setLineDash([]);
  draft.pts.forEach(([x, y], i) => { ctx.beginPath(); ctx.arc(sx(x), sy(y), i ? 3.5 : 5, 0, 7); ctx.fill(); });
  ctx.restore();
}
