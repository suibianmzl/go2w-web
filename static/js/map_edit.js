// 地图编辑（v1.0.2，11-18）：去噪 / 画笔 / 框选裁剪，在网页里改，另存为新地图（后端 server/mapedit.py）。
// 只编辑已保存的地图（store.mapCurrent），建图运行时不能编辑。保存时只发「改了哪些格」和底图的 CRC32，
// 后端拿硬盘上的原图核对后在原图上改，所以存下来的就是这里看到的样子；原图永远不动。
import { $, $$, paintCell, gridToCanvas } from './util.js';
import { toast, confirmBox, formBox } from './ui.js';
import * as KO from './keepout.js';

const FREE = 0, OCC = 100, UNK = 255;
const PAINT = { free: FREE, occ: OCC, unknown: UNK };
const TOOL_HINT = {
  pan: '拖动平移地图 · 滚轮缩放',
  free: '按住拖动，把噪点、杂物擦成空地',
  occ: '按住拖动画墙（只补真实存在的墙；想禁止通行请用上面的「禁行区」页）',
  unknown: '按住拖动，把伸到楼外的射线擦成未知',
  crop: '拖一个框：框外全部变成未知',
};
// 禁行区页（v1.1.8）：画的东西不改地图，存在这张图旁边，导航中保存马上生效
const KEEP_HINT = {
  pan: '拖动平移地图 · 点列表里的一项在地图上高亮',
  line: '逐个点击放折线的点 · 再点最后一个点、按 Enter 或「完成」结束 · Backspace 退一个点 · Esc 放弃',
  zone: '逐个点击放区域的角 · 点回第一个点、按 Enter 或「完成」闭合 · Backspace 退一个点 · Esc 放弃',
  del: '点一下要删的禁行线 / 区域',
};
const KEEP_MAX = 100, KEEP_PTS = 400;
const UNDO_MAX = 40, MAX_RUNS = 120000;
const NAME_RE = /^[A-Za-z0-9_-]{1,48}$/;
const isOcc = v => v >= 65 && v !== UNK;

let CRC_T = null;
// 和后端 zlib.crc32 一样
export function crc32(u8){
  if (!CRC_T){
    CRC_T = new Uint32Array(256);
    for (let n = 0; n < 256; n++){ let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; CRC_T[n] = c >>> 0; }
  }
  let c = 0xFFFFFFFF;
  for (let i = 0; i < u8.length; i++) c = CRC_T[(c ^ u8[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

// 孤立的小障碍块（8 邻域连通，格数 ≤ max）。每块看它周围一圈：空地多就擦成空地，未知多就擦成未知
// （射线尽头、楼外的零星点）。返回 { idx, val, blobs }
export function findSpecks(g, w, h, max){
  const n = w * h, seen = new Uint8Array(n), stack = new Int32Array(n), buf = new Int32Array(n);
  const idx = [], val = []; let blobs = 0;
  for (let i0 = 0; i0 < n; i0++){
    if (seen[i0] || !isOcc(g[i0])) continue;
    let sp = 0, cnt = 0; stack[sp++] = i0; seen[i0] = 1;
    while (sp){
      const i = stack[--sp]; buf[cnt++] = i;
      const x = i % w, y = (i - x) / w;
      for (let dy = -1; dy <= 1; dy++){ const yy = y + dy; if (yy < 0 || yy >= h) continue;
        for (let dx = -1; dx <= 1; dx++){ const xx = x + dx; if ((!dx && !dy) || xx < 0 || xx >= w) continue;
          const j = yy * w + xx; if (!seen[j] && isOcc(g[j])){ seen[j] = 1; stack[sp++] = j; } } }
    }
    if (cnt > max) continue;
    let nf = 0, nu = 0;
    for (let k = 0; k < cnt; k++){
      const i = buf[k], x = i % w, y = (i - x) / w;
      for (let dy = -1; dy <= 1; dy++){ const yy = y + dy; if (yy < 0 || yy >= h) continue;
        for (let dx = -1; dx <= 1; dx++){ const xx = x + dx; if (xx < 0 || xx >= w) continue;
          const v = g[yy * w + xx]; if (isOcc(v)) continue; if (v === UNK) nu++; else nf++; } }
    }
    const fill = nu > nf ? UNK : FREE;
    for (let k = 0; k < cnt; k++){ idx.push(buf[k]); val.push(fill); }
    blobs++;
  }
  return { idx: Int32Array.from(idx), val: Uint8Array.from(val), blobs };
}

// 和 base 比，变了的格按 [起点, 长度, 新值] 连成段（新值 0 / 100 / -1）
export function diffRuns(base, work){
  const out = [], n = base.length; let i = 0;
  while (i < n){
    if (base[i] === work[i]){ i++; continue; }
    const v = work[i]; let j = i + 1;
    while (j < n && base[j] !== work[j] && work[j] === v) j++;
    out.push([i, j - i, v === UNK ? -1 : v]); i = j;
  }
  return out;
}

export function createMapEditor(store, actions, { exit, onHint }){
  const E = { tool: 'free', brush: 2, speckMax: 4, showSpecks: true, compare: false, tab: 'map', ktool: 'line' };
  // K：禁行区编辑。shapes 是工作副本，saved 是上次保存（或后端推来）的 JSON，比较两者就知道有没有没存的
  let K = null;
  let S = null, rec = null, stampId = 0, drag = null, hoverPt = null, speckT = null, checkT = 0;
  const panel = $('#medit');

  /* ---------- 坐标：世界（米）↔ 格（浮点，行 0 在 origin 一侧），地图可以带 oyaw ---------- */
  function toCell(x, y){
    const c = Math.cos(S.oyaw), s = Math.sin(S.oyaw), dx = x - S.ox, dy = y - S.oy;
    return [(c*dx + s*dy) / S.res, (-s*dx + c*dy) / S.res];
  }
  function toWorld(gx, gy){
    const c = Math.cos(S.oyaw), s = Math.sin(S.oyaw);
    return { x: S.ox + (c*gx - s*gy) * S.res, y: S.oy + (s*gx + c*gy) * S.res };
  }

  /* ---------- 改格子：记录撤销信息、更新画布 ---------- */
  function paint(i){
    const x = i % S.w, y = (i - x) / S.w, r = S.h - 1 - y;
    paintCell(S.im.data, S.work[i], x, y, S.w, S.h);
    const b = S.dirtyBox;
    if (x < b[0]) b[0] = x; if (x > b[2]) b[2] = x; if (r < b[1]) b[1] = r; if (r > b[3]) b[3] = r;
  }
  function flush(){
    const b = S.dirtyBox; if (b[2] < b[0]) return;
    S.cx.putImageData(S.im, 0, 0, b[0], b[1], b[2] - b[0] + 1, b[3] - b[1] + 1);
    S.dirtyBox = [Infinity, Infinity, -1, -1];
  }
  function begin(kind, meta){ rec = { idx: [], old: [], kind, meta }; stampId = (stampId + 1) >>> 0 || 1; }
  function set(i, v){
    const o = S.work[i]; if (o === v) return;
    if (S.stamp[i] !== stampId){ S.stamp[i] = stampId; rec.idx.push(i); rec.old.push(o); }
    S.work[i] = v; paint(i);
  }
  function count(kind, meta, sign){
    const st = S.stats;
    if (kind === 'denoise'){ st.denoise_blobs += sign * meta.blobs; st.denoise_cells += sign * meta.cells; if (sign > 0) st.denoise_max = meta.max; }
    else st[kind] += sign;
  }
  function commit(){
    const r = rec; rec = null; flush();
    if (!r || !r.idx.length) return false;
    const idx = Int32Array.from(r.idx), old = Uint8Array.from(r.old), nw = new Uint8Array(idx.length);
    for (let k = 0; k < idx.length; k++) nw[k] = S.work[idx[k]];
    S.undo.push({ idx, old, nw, kind: r.kind, meta: r.meta }); if (S.undo.length > UNDO_MAX) S.undo.shift();
    S.redo = []; count(r.kind, r.meta, 1); changed();
    return true;
  }
  function replay(op, vals){ for (let k = 0; k < op.idx.length; k++){ S.work[op.idx[k]] = vals[k]; paint(op.idx[k]); } flush(); }
  function undo(){ const op = S?.undo.pop(); if (!op) return; replay(op, op.old); S.redo.push(op); count(op.kind, op.meta, -1); changed(); }
  function redo(){ const op = S?.redo.pop(); if (!op) return; replay(op, op.nw); S.undo.push(op); count(op.kind, op.meta, 1); changed(); }
  function dirtyCount(){ let n = 0; const b = S.base, w = S.work; for (let i = 0; i < b.length; i++) if (b[i] !== w[i]) n++; return n; }

  /* ---------- 三种编辑 ---------- */
  function stamp(gx, gy, v){
    const r = E.brush, cx = Math.floor(gx), cy = Math.floor(gy), lim = r*r + r;
    for (let dy = -r; dy <= r; dy++){ const y = cy + dy; if (y < 0 || y >= S.h) continue;
      for (let dx = -r; dx <= r; dx++){ if (dx*dx + dy*dy > lim) continue; const x = cx + dx; if (x < 0 || x >= S.w) continue; set(y*S.w + x, v); } }
  }
  function strokeTo(gx, gy){
    const [lx, ly] = drag.last, d = Math.hypot(gx - lx, gy - ly), step = Math.max(.5, E.brush * .5), n = Math.max(1, Math.ceil(d / step));
    for (let k = 1; k <= n; k++) stamp(lx + (gx - lx)*k/n, ly + (gy - ly)*k/n, PAINT[drag.tool]);
    drag.last = [gx, gy]; flush();
  }
  function cropBox(d = drag){
    const [ax, ay] = d.a, [bx, by] = d.b;
    return [Math.max(0, Math.floor(Math.min(ax, bx))), Math.max(0, Math.floor(Math.min(ay, by))),
            Math.min(S.w - 1, Math.floor(Math.max(ax, bx))), Math.min(S.h - 1, Math.floor(Math.max(ay, by)))];
  }
  function applyCrop(d){
    const [x0, y0, x1, y1] = cropBox(d);
    if (x1 - x0 < 2 || y1 - y0 < 2) return toast('框太小了：拖一个框，框外的会变成未知', 'warn');
    begin('crop');
    for (let y = 0; y < S.h; y++) for (let x = 0; x < S.w; x++) if (x < x0 || x > x1 || y < y0 || y > y1) set(y*S.w + x, UNK);
    if (!commit()) toast('框外本来就都是未知，没有变化', 'warn');
  }
  function applyDenoise(){
    const sp = S.specks; if (!sp || !sp.idx.length) return toast('没有找到这么小的孤立障碍', 'warn');
    begin('denoise', { blobs: sp.blobs, cells: sp.idx.length, max: E.speckMax });
    for (let k = 0; k < sp.idx.length; k++) set(sp.idx[k], sp.val[k]);
    commit(); toast(`已去掉 ${sp.blobs} 块噪点（${sp.idx.length} 格），不满意可以撤销`);
  }

  /* ---------- 噪点预览（红色） ---------- */
  function computeSpecks(){
    if (!S) return;
    S.specks = findSpecks(S.work, S.w, S.h, E.speckMax);
    const d = S.spIm.data; d.fill(0);
    const mark = (x, y, a) => { if (x < 0 || y < 0 || x >= S.w || y >= S.h) return; const p = ((S.h-1-y)*S.w + x)*4; if (d[p+3] < a){ d[p] = 255; d[p+1] = 75; d[p+2] = 92; d[p+3] = a; } };
    for (const i of S.specks.idx){ const x = i % S.w, y = (i - x) / S.w;
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) mark(x + dx, y + dy, dx || dy ? 70 : 255); }
    S.spCx.putImageData(S.spIm, 0, 0);
    renderPanel();
  }
  function scheduleSpecks(){ clearTimeout(speckT); speckT = setTimeout(computeSpecks, 120); }
  function changed(){ S.dirty = dirtyCount(); scheduleSpecks(); renderPanel(); }

  /* ---------- 面板 ---------- */
  function renderPanel(){
    if (!S) return;
    $('#meditName').textContent = S.source;
    $$('#meditTools [data-et]').forEach(b => b.classList.toggle('on', b.dataset.et === E.tool));
    const brushOn = E.tool in PAINT;
    $('#meditBrushRow').classList.toggle('off', !brushOn);
    $('#meditBrushTxt').textContent = `${Math.round((2*E.brush + 1) * S.res * 100)} cm`;
    $('#meditSpeckTxt').textContent = `${E.speckMax} 格`;
    const sp = S.specks;
    $('#meditSpeckInfo').textContent = !sp ? '查找中…' : sp.blobs ? `找到 ${sp.blobs} 块（${sp.idx.length} 格）${E.showSpecks ? '，地图上标红' : ''}` : '没有这么小的孤立障碍';
    $('#meditDenoise').disabled = !sp || !sp.blobs;
    $('#meditUndo').disabled = !S.undo.length; $('#meditRedo').disabled = !S.redo.length;
    $('#meditDirty').textContent = S.dirty ? `已改 ${S.dirty} 格` : '未修改';
    $('#meditSave').disabled = !S.dirty || store.mapBusy || store.mapSaving;
    $('#meditHint').textContent = TOOL_HINT[E.tool];
    $$('#meditTabs [data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === E.tab));
    $('#meditMapPane').hidden = E.tab !== 'map'; $('#meditKeepPane').hidden = E.tab !== 'keep';
    const kd = keepDirty();
    $('#meditTabs [data-tab=keep]').textContent = kd ? '禁行区 •' : '禁行区';
    $('#meditTabs [data-tab=map]').textContent = S.dirty ? '修地图 •' : '修地图';
    renderKeep();
  }
  /* ---------- 禁行区页 ---------- */
  const keepJSON = shapes => JSON.stringify(shapes.map(s => ({ kind: s.kind, pts: s.pts, ...(s.name ? { name: s.name } : {}) })));
  const keepDirty = () => !!K && keepJSON(K.shapes) !== K.saved;
  function keepFromStore(){
    // storeRef 记 store 里的原值（没有禁行区时是 undefined）：tick 拿它比较判断别处有没有改过
    const ref = (store.keepouts || {})[S.source], v = ref || [];
    return { shapes: JSON.parse(JSON.stringify(v)), saved: keepJSON(v), storeRef: ref };
  }
  function keepState(){
    const st = store.keepoutState, nav = store.profiles.navigation === 'run', live = nav && store.mapCurrent === S.source;
    if (store.source !== 'ros') return ['模拟模式：只存在这个浏览器里', ''];
    if (!live) return ['保存后，下次用这张图导航时生效', ''];
    if (!st?.ready) return ['这台导航版本还没有禁行层（filter_mask_server），保存了也要等更新导航镜像后才生效', 'warn'];
    if (st.loading) return ['正在加载进导航…', ''];
    return st.live === S.source ? ['导航正在用这张图：保存后马上生效，不用重新定位', 'ok'] : ['导航里还没加载这张图的禁行区，保存一次就会加载', 'warn'];
  }
  function renderKeep(){
    if (!S || !K) return;
    $$('#keepTools [data-kt]').forEach(b => b.classList.toggle('on', b.dataset.kt === E.ktool));
    $('#keepHint').textContent = KEEP_HINT[E.ktool];
    $('#keepFinish').hidden = !K.draft;
    $('#keepFinish').disabled = !K.draft || K.draft.pts.length < (K.draft.kind === 'line' ? 2 : 3);
    const list = $('#keepList'); list.textContent = '';
    if (!K.shapes.length){ const e = document.createElement('div'); e.className = 'keep-empty'; e.textContent = '这张图还没有禁行区。选「禁行线」沿路牙点几下，或用「禁行区域」圈一块不让进的地方'; list.appendChild(e); }
    K.shapes.forEach((sh, k) => {
      const d = KO.describe(sh, k), row = document.createElement('div');
      row.className = 'keep-item' + (k === K.hi ? ' on' : '');
      row.innerHTML = `<i class="keep-ic ${sh.kind}"></i><input maxlength="24" spellcheck="false"><span class="mono"></span><button class="ib" title="删除" aria-label="删除"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 6l12 12M18 6L6 18"/></svg></button>`;
      const inp = row.querySelector('input'); inp.value = sh.name || ''; inp.placeholder = d.title;
      row.querySelector('span').textContent = d.size;
      inp.oninput = () => { sh.name = inp.value.trim(); renderTabsOnly(); };
      inp.onfocus = () => { K.hi = k; };
      row.onmouseenter = () => { K.hi = k; }; row.onmouseleave = () => { if (K.hi === k) K.hi = -1; };
      row.querySelector('button').onclick = () => keepRemove(k);
      list.appendChild(row);
    });
    const kd = keepDirty();
    $('#keepUndo').disabled = !K.undo.length; $('#keepRedo').disabled = !K.redo.length;
    $('#keepDirty').textContent = kd ? '有未保存的修改' : K.shapes.length ? `${K.shapes.length} 个，已保存` : '';
    const [msg, cls] = keepState(), ls = $('#keepLive'); ls.textContent = msg; ls.className = 'keep-live ' + cls;
    $('#keepSave').disabled = !kd || !!K.saving || !!K.draft;
    $('#keepSave').textContent = K.saving ? '保存中…' : '保存禁行区';
  }
  function renderTabsOnly(){
    $('#meditTabs [data-tab=keep]').textContent = keepDirty() ? '禁行区 •' : '禁行区';
    const kd = keepDirty(); $('#keepSave').disabled = !kd || !!K.saving || !!K.draft;
    $('#keepDirty').textContent = kd ? '有未保存的修改' : K.shapes.length ? `${K.shapes.length} 个，已保存` : '';
  }
  function keepPush(){ K.undo.push(JSON.stringify(K.shapes)); if (K.undo.length > UNDO_MAX) K.undo.shift(); K.redo = []; }
  function keepUndo(){ if (!K?.undo.length) return; K.redo.push(JSON.stringify(K.shapes)); K.shapes = JSON.parse(K.undo.pop()); K.hi = -1; renderPanel(); }
  function keepRedo(){ if (!K?.redo.length) return; K.undo.push(JSON.stringify(K.shapes)); K.shapes = JSON.parse(K.redo.pop()); K.hi = -1; renderPanel(); }
  function keepRemove(k){ keepPush(); K.shapes.splice(k, 1); K.hi = -1; renderPanel(); }
  function keepFinish(){
    const d = K.draft; if (!d) return;
    const need = d.kind === 'line' ? 2 : 3;
    if (d.pts.length < need) return toast(d.kind === 'line' ? '禁行线至少要 2 个点' : '禁行区域至少要 3 个点', 'warn');
    if (K.shapes.length >= KEEP_MAX) return toast(`禁行线 / 区域最多 ${KEEP_MAX} 个`, 'err');
    keepPush(); K.shapes.push({ kind: d.kind, pts: d.pts.map(([x, y]) => [+x.toFixed(3), +y.toFixed(3)]) });
    K.draft = null; renderPanel();
  }
  async function keepSave(){
    if (!keepDirty() || K.saving) return;
    if (K.draft) return toast('还有一条没画完：先「完成」或按 Esc 放弃', 'warn');
    const shapes = JSON.parse(keepJSON(K.shapes)), src = S.source;
    // 已保存的任务站点落进新禁行区：后端出发时会拒绝，这里先说出来
    const bad = (store.routes || []).filter(r => r.map === src).flatMap(r => (r.points || []).map((p, i) => [r, i, p]))
      .filter(([, , p]) => KO.blocked(shapes, p.x, p.y) >= 0);
    const doSave = async () => {
      K.saving = true; renderKeep();
      let r; try { r = await actions.saveKeepout(src, shapes); } finally { if (K) { K.saving = false; } }
      if (!K || !S || S.source !== src) return;
      if (r && r.ok !== false){ K.saved = keepJSON(shapes); K.storeRef = (store.keepouts || {})[src] || K.storeRef; }
      renderPanel();
    };
    if (bad.length){
      const names = [...new Set(bad.map(([r, i]) => `「${r.name}」第 ${i + 1} 站`))].slice(0, 4).join('、');
      return confirmBox({ title: '有任务站点落在禁行范围里', body: `${names}${bad.length > 4 ? ' 等' : ''}离禁行线不到 ${KO.BAND} m 或在区域内。保存后这些任务出发时会被拒绝，要先改站点位置。`, ok: '仍然保存', onOk: doSave });
    }
    return doSave();
  }
  $$('#meditTabs [data-tab]').forEach(b => { b.onclick = () => { if (!S) return; E.tab = b.dataset.tab; if (K) K.draft = null; renderPanel(); exitHint(); }; });
  $$('#keepTools [data-kt]').forEach(b => { b.onclick = () => { E.ktool = b.dataset.kt; if (K) K.draft = null; renderPanel(); exitHint(); }; });
  $('#keepFinish').onclick = () => K && keepFinish();
  $('#keepUndo').onclick = keepUndo; $('#keepRedo').onclick = keepRedo;
  $('#keepSave').onclick = () => S && keepSave();
  const exitHint = () => onHint && onHint();
  $$('#meditTools [data-et]').forEach(b => { b.onclick = () => { E.tool = b.dataset.et; renderPanel(); }; });
  $('#meditBrush').oninput = e => { E.brush = +e.target.value; renderPanel(); };
  $('#meditSpeck').oninput = e => { E.speckMax = +e.target.value; if (S){ S.specks = null; renderPanel(); scheduleSpecks(); } };
  $('#meditShow').onchange = e => { E.showSpecks = e.target.checked; renderPanel(); };
  $('#meditDenoise').onclick = () => S && applyDenoise();
  $('#meditUndo').onclick = undo; $('#meditRedo').onclick = redo;
  const cmp = $('#meditCompare'), hold = v => () => { E.compare = v; cmp.classList.toggle('on', v); };
  cmp.onpointerdown = hold(true); cmp.onpointerup = cmp.onpointerleave = cmp.onpointercancel = hold(false);
  $('#meditSave').onclick = () => S && saveDialog();
  $('#meditClose').onclick = () => exit();

  /* ---------- 打开 / 关闭 ---------- */
  function blocker(){
    if (Object.entries(store.profiles).some(([k, v]) => k === 'mapping' && v !== 'stop')) return '建图运行中不能编辑：先停止建图，再打开保存好的地图来编辑';
    const g = store.mapGrid, name = store.mapCurrent;
    if (!g || !name || !store.map) return '没有可编辑的地图：先在右栏选一张已保存的地图';
    if (store.source === 'ros' && !store.maps.some(m => m.name === name)) return `${name} 不在 data/maps 里，不能编辑`;
    return '';
  }
  function open(){
    const why = blocker(); if (why){ toast(why, 'warn'); return false; }
    const g = store.mapGrid, w = g.w, h = g.h, base = Uint8Array.from(g.grid);
    const cv = document.createElement('canvas'); cv.width = w; cv.height = h;
    const cx = cv.getContext('2d'), im = cx.createImageData(w, h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) paintCell(im.data, base[y*w + x], x, y, w, h);
    cx.putImageData(im, 0, 0);
    const spCv = document.createElement('canvas'); spCv.width = w; spCv.height = h;
    const spCx = spCv.getContext('2d');
    S = { source: store.mapCurrent, gridRef: g, w, h, res: g.res, ox: g.ox, oy: g.oy, oyaw: g.oyaw || 0,
      base, work: Uint8Array.from(base), crc: crc32(base), stamp: new Uint32Array(w*h),
      cv, cx, im, baseCv: gridToCanvas(base, w, h), spCv, spCx, spIm: spCx.createImageData(w, h),
      rect: { w: w*g.res, h: h*g.res, ox: g.ox, oy: g.oy, yaw: g.oyaw || 0 },
      undo: [], redo: [], specks: null, dirty: 0, dirtyBox: [Infinity, Infinity, -1, -1],
      stats: { free: 0, occ: 0, unknown: 0, crop: 0, denoise_blobs: 0, denoise_cells: 0, denoise_max: 0 } };
    $('#meditBrush').value = E.brush; $('#meditSpeck').value = E.speckMax; $('#meditShow').checked = E.showSpecks;
    K = { ...keepFromStore(), undo: [], redo: [], draft: null, hi: -1, hover: null, scale: 40, saving: false };
    panel.hidden = false; renderPanel(); computeSpecks();
    return true;
  }
  function end(){ K = null; S = null; drag = null; rec = null; E.compare = false; cmp.classList.remove('on'); panel.hidden = true; clearTimeout(speckT); }
  // 返回 true = 已经关了；有没保存的修改时先问，确认后关掉再调 cb
  function close(cb){
    if (!S) return true;
    const kd = keepDirty();
    if (S.dirty || kd){
      const what = [S.dirty ? `${S.dirty} 格地图修改` : '', kd ? '禁行区修改' : ''].filter(Boolean).join('和');
      confirmBox({ title: '放弃没保存的修改？', body: `还有${what}没有保存，退出编辑会丢掉。`, ok: '放弃修改', danger: true,
        onOk: () => { end(); cb && cb(); } });
      return false;
    }
    end(); return true;
  }
  // 编辑中显示的地图换了（切了地图 / 开了建图 / 地图内容变了）：底图不再是这张，只能退出
  function tick(){
    if (!S || performance.now() - checkT < 500) return; checkT = performance.now();
    let why = '';
    if (store.profiles.mapping && store.profiles.mapping !== 'stop') why = '建图启动了';
    else if (store.mapCurrent !== S.source) why = `显示的地图换成了 ${store.mapCurrent || '（无）'}`;
    else if (store.mapGrid && store.mapGrid !== S.gridRef){
      const g = store.mapGrid;
      if (g.w !== S.w || g.h !== S.h || crc32(g.grid) !== S.crc) why = '地图内容变了';
      else S.gridRef = g;   // 同一张图又发了一次（比如导航刚启动），继续编辑
    }
    if (!why && K && (store.keepouts || {})[S.source] !== K.storeRef){
      // 别的网页（或这次保存）改了这张图的禁行区：自己没有未保存的修改就跟上，有就保留自己的
      if (!keepDirty() && !K.draft){ Object.assign(K, keepFromStore()); K.undo = []; K.redo = []; renderPanel(); }
      else K.storeRef = (store.keepouts || {})[S.source];
    }
    if (K && !K.saving && performance.now() - (K.stT || 0) > 1000){ K.stT = performance.now(); renderKeep(); }
    if (why){ const n = S.dirty; end(); exit(true); toast(`${why}，地图编辑已退出${n ? `（${n} 格修改没有保存）` : ''}`, 'warn'); }
  }

  /* ---------- 保存 ---------- */
  function suggest(){
    const stem = S.source.replace(/_e\d+$/, '').slice(0, 44);
    for (let k = 1; k < 100; k++){ const n = `${stem}_e${k}`; if (!store.maps.some(m => m.name === n)) return n; }
    return `${stem}_e${Date.now() % 1000}`;
  }
  function saveDialog(){
    const runs = diffRuns(S.base, S.work);
    if (!runs.length) return toast('和原图相比没有任何修改', 'warn');
    if (runs.length > MAX_RUNS) return toast(`修改太零碎了（${runs.length} 段），请分几次编辑保存`, 'err');
    const st = S.stats, src = S.source, nRoutes = (store.routes || []).filter(r => r.map === src).length;
    if (!S.dirty) return toast('地图本身没有修改：禁行区请在「禁行区」页保存，不用另存新图', 'warn');
    const rows = [['原图', src], ['修改', `${S.dirty} 格（约 ${(S.dirty * S.res * S.res).toFixed(2)} m²）`]];
    if (st.denoise_blobs) rows.push(['去噪', `${st.denoise_blobs} 块 · ${st.denoise_cells} 格`]);
    const manual = [['free', '擦成空地'], ['occ', '画墙'], ['unknown', '擦成未知']].filter(([k]) => st[k] > 0).map(([k, l]) => `${l} ${st[k]} 笔`);
    if (st.crop > 0) manual.push(`裁剪 ${st.crop} 次`);
    if (manual.length) rows.push(['手动', manual.join('、')]);
    formBox({ title: '另存为新地图', rows, ok: '保存',
      body: '原图不会改动。新图的分辨率和原点跟原图一样，站点坐标照样能用。',
      warn: '编辑过的地图不能用来续建：以后要续建请用原图，续建完再重新编辑。',
      build: f => {
        const t = document.createElement('div'); t.className = 'modal-label'; t.textContent = '新地图名（字母、数字、_、-）';
        const inp = document.createElement('input'); inp.className = 'modal-select'; inp.dataset.name = ''; inp.value = suggest(); inp.maxLength = 48; inp.spellcheck = false;
        const msg = document.createElement('div'); msg.className = 'modal-warn'; msg.dataset.msg = ''; msg.hidden = true;
        f.append(t, inp, msg);
        const chk = (key, text, on) => { const l = document.createElement('label'); l.className = 'modal-chk';
          l.innerHTML = `<input type="checkbox" data-${key}${on ? ' checked' : ''}><span></span>`; l.querySelector('span').textContent = text; f.appendChild(l); };
        if (nRoutes) chk('routes', `把原图上的 ${nRoutes} 条任务复制一份到新图（原图任务保留，坐标不用重标）`, true);
        if (keepDirty()) chk('keepout', '把「禁行区」页还没保存的修改一起带到新图（原图的禁行区不改）', true);
        else if (K?.shapes.length) { const t = document.createElement('div'); t.className = 'modal-label'; t.textContent = `这张图的 ${K.shapes.length} 个禁行区会一起带到新图`; f.appendChild(t); }
        const live = ['localization', 'navigation'].some(k => store.profiles[k] === 'run');
        chk('switch', live ? '保存后切换到新地图（导航正在运行：会取消当前导航，切换后要重新定位）' : '保存后切换到新地图', true);
        setTimeout(() => { inp.focus(); inp.select(); });
      },
      valid: f => {
        const v = f.querySelector('[data-name]').value.trim(), m = f.querySelector('[data-msg]');
        const why = !NAME_RE.test(v) ? '只能用字母、数字、下划线和减号，最多 48 个字符' : v === src ? '不能和原图同名（编辑不覆盖原图）'
          : store.maps.some(x => x.name === v) ? '已经有这个名字的地图了' : '';
        m.hidden = !why; m.textContent = why; return !why;
      },
      onOk: async f => {
        const name = f.querySelector('[data-name]').value.trim(), sw = f.querySelector('[data-switch]').checked;
        const p = { source: src, name, crc: S.crc, w: S.w, h: S.h, runs, routes: !!f.querySelector('[data-routes]')?.checked,
          ...(f.querySelector('[data-keepout]')?.checked && !K.draft ? { keepout: JSON.parse(keepJSON(K.shapes)) } : {}),
          summary: Object.fromEntries(Object.entries(st).filter(([, v]) => v > 0)) };
        $('#meditSave').disabled = true; $('#meditSave').textContent = '保存中…';
        let r;
        try { r = await actions.saveEditedMap(p); }
        finally { $('#meditSave').textContent = '另存为新地图…'; renderPanel(); }
        if (!r || r.ok === false) return;
        end(); exit(true);
        if (sw) actions.loadMap(name, { confirmed: true });
      } });
  }

  /* ---------- 指针（map_view 转进来，坐标是世界米） ---------- */
  function keepDown(x, y){
    const near = 12 / K.scale;
    if (E.ktool === 'pan') return false;
    if (E.ktool === 'del'){
      const k = KO.pick(K.shapes, x, y, near);
      if (k < 0){ toast('附近没有禁行线 / 区域', 'warn'); return true; }
      keepRemove(k); return true;
    }
    const d = K.draft;
    if (!d){ K.draft = { kind: E.ktool, pts: [[x, y]] }; renderKeep(); return true; }
    const last = d.pts[d.pts.length - 1], first = d.pts[0];
    if (d.kind === 'zone' && d.pts.length >= 3 && Math.hypot(x - first[0], y - first[1]) < near){ keepFinish(); return true; }
    if (Math.hypot(x - last[0], y - last[1]) < near){ if (d.pts.length >= (d.kind === 'line' ? 2 : 3)) keepFinish(); return true; }
    if (d.pts.length >= KEEP_PTS){ toast(`一条最多 ${KEEP_PTS} 个点`, 'warn'); return true; }
    d.pts.push([x, y]); renderKeep(); return true;
  }
  function down(x, y){
    if (S && E.tab === 'keep') return keepDown(x, y);
    if (!S || E.tool === 'pan') return false;
    const c = toCell(x, y);
    if (E.tool === 'crop'){ drag = { tool: 'crop', a: c, b: c }; return true; }
    drag = { tool: E.tool, last: c }; begin(E.tool); stamp(c[0], c[1], PAINT[E.tool]); flush(); return true;
  }
  function move(x, y){
    hoverPt = S ? { x, y } : null;
    if (K) K.hover = hoverPt;
    if (S && E.tab === 'keep') return;
    if (!S || !drag) return;
    const c = toCell(x, y);
    if (drag.tool === 'crop') drag.b = c; else strokeTo(c[0], c[1]);
  }
  function up(){
    if (!drag) return; const d = drag; drag = null;
    if (d.tool === 'crop') applyCrop(d); else commit();
  }
  function key(e){
    if (!S) return false;
    const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
    if (E.tab === 'keep' && K){
      if (mod && k === 'z'){ e.preventDefault(); e.shiftKey ? keepRedo() : keepUndo(); return true; }
      if (mod && k === 'y'){ e.preventDefault(); keepRedo(); return true; }
      if (!K.draft) return false;
      if (k === 'enter'){ e.preventDefault(); keepFinish(); return true; }
      if (k === 'escape'){ e.preventDefault(); K.draft = null; renderKeep(); return true; }
      if (k === 'backspace'){ e.preventDefault(); K.draft.pts.pop(); if (!K.draft.pts.length) K.draft = null; renderKeep(); return true; }
      return false;
    }
    if (mod && k === 'z'){ e.preventDefault(); e.shiftKey ? redo() : undo(); return true; }
    if (mod && k === 'y'){ e.preventDefault(); redo(); return true; }
    return false;
  }

  /* ---------- 画（在 map_view 的旋转坐标系里调用） ---------- */
  function layer(){ return S ? { rect: S.rect, img: E.compare ? S.baseCv : S.cv } : null; }
  function overlay(ctx, sx, sy, drawGridImg, scale){
    if (!S) return;
    if (K){
      K.scale = scale;
      KO.draw(ctx, sx, sy, K.shapes, scale, { hi: K.hi, dim: E.tab !== 'keep' });
      if (E.tab === 'keep'){
        KO.drawDraft(ctx, sx, sy, K.draft, K.hover);
        if (E.ktool === 'del' && K.hover){ const k = KO.pick(K.shapes, K.hover.x, K.hover.y, 12 / scale); if (k >= 0) KO.draw(ctx, sx, sy, [K.shapes[k]], scale, { hi: 0 }); }
        return;
      }
    }
    if (E.showSpecks && !E.compare && S.specks?.blobs) drawGridImg(S.rect, S.spCv);
    if (drag?.tool === 'crop'){
      const [x0, y0, x1, y1] = cropBox(), pts = [[x0, y0], [x1 + 1, y0], [x1 + 1, y1 + 1], [x0, y1 + 1]].map(([a, b]) => toWorld(a, b));
      ctx.save(); ctx.strokeStyle = '#ffe08a'; ctx.lineWidth = 1.5; ctx.setLineDash([6, 4]); ctx.fillStyle = 'rgba(255,224,138,.06)';
      ctx.beginPath(); pts.forEach((p, i) => i ? ctx.lineTo(sx(p.x), sy(p.y)) : ctx.moveTo(sx(p.x), sy(p.y))); ctx.closePath(); ctx.fill(); ctx.stroke(); ctx.restore();
    }
    if (hoverPt && E.tool in PAINT && !E.compare){
      const r = (E.brush + .5) * S.res * scale, col = { free: '#dbe6f3', occ: '#38e1dc', unknown: '#9db0c7' }[E.tool];
      ctx.save(); ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(sx(hoverPt.x), sy(hoverPt.y), Math.max(2, r), 0, 7); ctx.stroke(); ctx.restore();
    }
  }

  return { open, close, tick, down, move, up, key, layer, overlay, renderPanel,
    get active(){ return !!S; }, get hint(){ return S ? (E.tab === 'keep' ? KEEP_HINT[E.ktool] : TOOL_HINT[E.tool]) : ''; },
    get keepTab(){ return !!S && E.tab === 'keep'; },
    // 从别处直接进「禁行区」页（工具栏外的入口用）
    openKeep(){ if (!S) return false; E.tab = 'keep'; renderPanel(); exitHint(); return true; },
    leave(){ hoverPt = null; if (K) K.hover = null; } };
}
