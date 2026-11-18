// 地图编辑（v1.0.2，11-18）：去噪 / 画笔 / 框选裁剪，在网页里改，另存为新地图（后端 server/mapedit.py）。
// 只编辑已保存的地图（store.mapCurrent），建图运行时不能编辑。保存时只发「改了哪些格」和底图的 CRC32，
// 后端拿硬盘上的原图核对后在原图上改，所以存下来的就是这里看到的样子；原图永远不动。
import { $, $$, paintCell, gridToCanvas } from './util.js';
import { toast, confirmBox, formBox } from './ui.js';

const FREE = 0, OCC = 100, UNK = 255;
const PAINT = { free: FREE, occ: OCC, unknown: UNK };
const TOOL_HINT = {
  pan: '拖动平移地图 · 滚轮缩放',
  free: '按住拖动，把噪点、杂物擦成空地',
  occ: '按住拖动画墙（只补真实存在的墙；想禁止通行别用画墙）',
  unknown: '按住拖动，把伸到楼外的射线擦成未知',
  crop: '拖一个框：框外全部变成未知',
};
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

export function createMapEditor(store, actions, { exit }){
  const E = { tool: 'free', brush: 2, speckMax: 4, showSpecks: true, compare: false };
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
  }
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
    panel.hidden = false; renderPanel(); computeSpecks();
    return true;
  }
  function end(){ S = null; drag = null; rec = null; E.compare = false; cmp.classList.remove('on'); panel.hidden = true; clearTimeout(speckT); }
  // 返回 true = 已经关了；有没保存的修改时先问，确认后关掉再调 cb
  function close(cb){
    if (!S) return true;
    if (S.dirty){
      confirmBox({ title: '放弃地图修改？', body: `还有 ${S.dirty} 格修改没有保存，退出编辑会丢掉。`, ok: '放弃修改', danger: true,
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
        if (nRoutes) chk('routes', `把原图上的 ${nRoutes} 条任务改到新图（坐标不用重标）`, true);
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
  function down(x, y){
    if (!S || E.tool === 'pan') return false;
    const c = toCell(x, y);
    if (E.tool === 'crop'){ drag = { tool: 'crop', a: c, b: c }; return true; }
    drag = { tool: E.tool, last: c }; begin(E.tool); stamp(c[0], c[1], PAINT[E.tool]); flush(); return true;
  }
  function move(x, y){
    hoverPt = S ? { x, y } : null;
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
    if (mod && k === 'z'){ e.preventDefault(); e.shiftKey ? redo() : undo(); return true; }
    if (mod && k === 'y'){ e.preventDefault(); redo(); return true; }
    return false;
  }

  /* ---------- 画（在 map_view 的旋转坐标系里调用） ---------- */
  function layer(){ return S ? { rect: S.rect, img: E.compare ? S.baseCv : S.cv } : null; }
  function overlay(ctx, sx, sy, drawGridImg, scale){
    if (!S) return;
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
    get active(){ return !!S; }, get hint(){ return S ? TOOL_HINT[E.tool] : ''; }, leave(){ hoverPt = null; } };
}
