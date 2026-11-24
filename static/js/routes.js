import { $, deg } from './util.js';
// 右栏「导航任务」：选 / 新建 / 编辑路线，运行参数（跟任务一起保存），出发按钮（出发流程在 flow.js）

export function setupRouteEditor(store, actions, setTool){
  const select = $('#routeSelect'), name = $('#routeName'), list = $('#routePoints');
  const save = $('#routeSave'), run = $('#routeRun'), hint = $('#routeHint'), remove = $('#routeDelete');
  const confirm = $('#routeConfirm'), saveRow = $('#routeSaveRow');
  const runHint = $('#routeRunHint'), round = $('#routeRound'), loops = $('#routeLoops');
  const optsBox = $('#routeOpts'), optsDirtyTag = $('#routeOptsDirty'), revert = $('#routeRevert');
  const rtV = $('#rtV'), rtW = $('#rtW');
  let lastRoutes, lastMap, lastDraft, lastReplace = null, lastInsert = null, savedDraft = store.routeDraft, saving = false;
  const selected = () => store.routes.find(route => route.id === select.value);

  // ---------- 运行参数（11-12 起跟任务一起保存）：循环、圈数、最大 vx / wz ----------
  const DEFAULT_OPTS = { loop:false, loops:1, vmax:0.45, wmax:1.2 };
  const optsOf = route => ({ ...DEFAULT_OPTS, ...(route?.opts || {}) });
  const readLoops = () => Math.min(1000, Math.max(0, Math.round(+loops.value || 0)));
  const readOpts = () => ({ loop:round.checked, loops:readLoops(), vmax:+(+rtV.value).toFixed(2), wmax:+(+rtW.value).toFixed(2) });
  const showOpts = () => { $('#oRtV').textContent = `${(+rtV.value).toFixed(2)} m/s`; $('#oRtW').textContent = `${(+rtW.value).toFixed(1)} rad/s`; };
  function setOpts(o){
    round.checked = !!o.loop; loops.value = o.loops; loops.disabled = !o.loop;
    rtV.value = o.vmax; rtW.value = o.wmax; showOpts();
    lastDraft = null;            // 循环开关影响站点列表里终点的写法
  }
  const sameOpts = (a, b) => a.loop === b.loop && (!a.loop || a.loops === b.loops) && Math.abs(a.vmax - b.vmax) < 1e-6 && Math.abs(a.wmax - b.wmax) < 1e-6;
  // 参数和保存的任务不一样（新路线没有「保存的」，不算）
  const optsDirty = () => { const r = selected(); return !!r && savedDraft.id === r.id && !sameOpts(readOpts(), optsOf(r)); };

  function edit(route = null){
    store.routeReplace = store.routeInsert = null;
    store.routeDraft = route ? { ...route, confirmed: true } : { id:null, map:null, points:[], confirmed:false };
    store.routeCheck = null;
    savedDraft = store.routeDraft;
    name.value = route?.name || '';
    setOpts(optsOf(route));
  }

  // 循环：1 → … → N → 1 算一圈；0 = 一直循环。勾选循环后最后一站不再是终点：重画站点列表
  round.onchange = () => { loops.disabled = !round.checked; lastDraft = null; render(); };
  loops.oninput = render;
  loops.onchange = () => { loops.value = readLoops(); render(); };
  rtV.oninput = rtW.oninput = () => { showOpts(); render(); };
  revert.onclick = () => { setOpts(optsOf(selected())); render(); };

  select.onchange = () => { edit(selected()); setTool('pan'); render(); };
  $('#routeNew').onclick = () => { select.value = ''; edit(); setTool('route'); render(); };
  // 出发：参数 / 名称改过没保存就先保存（按钮显示「保存并出发」），然后交给 flow.js 的出发流程（站立 → 导航 → 定位 → 下发）
  run.onclick = async () => {
    if (run.disabled) return;
    let route = selected();
    if (needSave()){
      const saved = await doSave();
      if (!saved) return;
      route = saved;
    }
    store.startGo?.(route);
    render();
  };
  remove.onclick = () => {
    const route = selected();
    if (!route) return;
    actions.deleteRoute(route, () => {
      if (store.routeDraft.id === route.id) edit();
      render();
    });
  };
  confirm.onclick = () => {
    const draft = store.routeDraft, points = draft.points;
    // 沿着路线给每站一个默认朝向。首站朝向下一站，之后各站沿上一段的方向到达。
    const oriented = points.map((p, i) => {
      if (p.manualHeading !== false) return p;
      const a = points[Math.max(0, i - 1)], b = i === 0 ? points[1] : p;
      return { ...p, th: Math.atan2(b.y - a.y, b.x - a.x) };
    });
    store.routeDraft = { ...draft, points:oriented, confirmed:true };
    store.routeCheck = null;
    setTool('pan');
    render();
    name.focus();
  };
  // 保存（新建或覆盖同一个任务）：路线点 + 名称 + 运行参数。成功返回保存后的任务
  async function doSave(){
    const draft = store.routeDraft, input = name.value, opts = readOpts();
    saving = true;
    render();
    try {
      const reply = await actions.saveRoute(input, opts);
      if (reply?.ok && store.routeDraft === draft && name.value === input){
        const preview = store.routeCheck;
        edit(reply.data);
        if (preview?.draft === draft){
          preview.draft = store.routeDraft;
          store.routeCheck = preview;
        }
        select.value = reply.data.id;
        return reply.data;
      }
      return null;
    } finally {
      saving = false;
      render();
    }
  }
  save.onclick = () => doSave();
  // 路线点没变、只改了名称或参数：出发时可以顺手保存
  const nameDirty = () => name.value !== (savedDraft.name || '');
  const needSave = () => { const r = selected(); return !!r && store.routeDraft === savedDraft && store.routeDraft.id === r.id && (optsDirty() || nameDirty()); };

  // 把第 from 站挪到第 to 站（下标），其余顺延；站名、备注、朝向跟着站走
  function movePoint(from, to){
    const current = store.routeDraft;
    if (from === to || !current.points[from]) { lastDraft = null; render(); return; }
    const points = [...current.points];
    const [p] = points.splice(from, 1);
    points.splice(to, 0, p);
    store.routeReplace = store.routeInsert = null;
    store.routeDraft = { ...current, points, confirmed:false };
    render();
  }
  // 列表里拖拽排序：按住 ⠿ 拖，蓝线标出松手后放在哪；拖到列表上下边缘会自动滚动
  function startDrag(e, li, from){
    if (e.button > 0) return;
    e.preventDefault();
    const grip = e.currentTarget;
    grip.setPointerCapture(e.pointerId);
    li.classList.add('route-dragging');
    let to = from, scroll = 0, lastY = e.clientY;
    const rows = () => [...list.children];
    const mark = () => {
      rows().forEach(r => r.classList.remove('drop-before', 'drop-after'));
      const rs = rows();
      if (to === from) return;
      if (to < from) rs[to]?.classList.add('drop-before');
      else rs[to]?.classList.add('drop-after');
    };
    const locate = y => {
      lastY = y;
      const rs = rows();
      let k = rs.length - 1;
      for (let j = 0; j < rs.length; j++){
        const r = rs[j].getBoundingClientRect();
        if (y < r.top + r.height / 2){ k = j > from ? j - 1 : j; break; }
      }
      to = Math.max(0, Math.min(rs.length - 1, k));
      const box = list.getBoundingClientRect();
      scroll = y < box.top + 24 ? -8 : y > box.bottom - 24 ? 8 : 0;
      mark();
    };
    const timer = setInterval(() => { if (scroll){ list.scrollTop += scroll; locate(lastY); } }, 30);
    const move = ev => locate(ev.clientY);
    const done = ev => {
      clearInterval(timer);
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', done);
      grip.removeEventListener('pointercancel', done);
      li.classList.remove('route-dragging');
      rows().forEach(r => r.classList.remove('drop-before', 'drop-after'));
      if (ev.type === 'pointerup' && to !== from) movePoint(from, to);
    };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', done);
    grip.addEventListener('pointercancel', done);
  }

  function render(){
    let draft = store.routeDraft;
    if (store.routes !== lastRoutes || store.mapCurrent !== lastMap){
      lastRoutes = store.routes; lastMap = store.mapCurrent;
      if (draft.id && !store.routes.some(r => r.id === draft.id)){
        if (draft === savedDraft && name.value === (savedDraft.name || '')) edit();
        else store.routeDraft = { ...draft, id:null };
        draft = store.routeDraft;
      }
      // 只列当前地图的任务（正在编辑的那条例外，免得换图后选择框里找不到它）
      const mine = store.routes.filter(r => r.map === store.mapCurrent || r.id === draft.id);
      const options = mine.map(r => new Option(`${r.name} · ${r.points.length} 站${r.map === store.mapCurrent ? '' : ' · ' + r.map}`, r.id));
      select.replaceChildren(new Option(mine.length ? '选择任务…' : '这张地图还没有任务', ''), ...options);
      select.value = draft.id || '';
    }
    if (store.routeReplace != null && !(store.routeReplace < draft.points.length)) store.routeReplace = null;
    if (store.routeInsert != null && !(store.routeInsert >= 1 && store.routeInsert < draft.points.length)) store.routeInsert = null;
    if (draft !== lastDraft || store.routeReplace !== lastReplace || store.routeInsert !== lastInsert){
      lastDraft = draft; lastReplace = store.routeReplace; lastInsert = store.routeInsert;
      list.replaceChildren(...draft.points.map((p, i) => {
        const li = document.createElement('li'), label = document.createElement('span');
        const end = draft.confirmed && i === draft.points.length - 1;
        const angle = `${Math.round(deg(p.th))}°`;
        const heading = p.manualHeading === false ? (draft.confirmed ? `自动 ${angle}` : '自动朝向') : angle;
        label.textContent = `${end ? (round.checked ? '回到 1 · ' : '终点 · ') : ''}${heading}`;
        label.title = `第 ${i + 1} 站${end ? (round.checked ? '（之后回到 1 号点）' : '（终点）') : ''} · 坐标 (${p.x.toFixed(2)}, ${p.y.toFixed(2)}) · ${heading}`;
        label.classList.toggle('route-end', end);
        li.classList.toggle('route-replacing', store.routeReplace === i);
        li.classList.toggle('route-insert-after', store.routeInsert === i + 1);
        li.dataset.i = i;
        // 站名、备注：只改文字，不影响路径。保留规划结果和输入框焦点
        const textField = (el, key, cls, max, placeholder) => {
          el.className = cls;
          el.maxLength = max;
          el.placeholder = placeholder;
          el.setAttribute('aria-label', placeholder);
          el.value = p[key] || '';
          el.oninput = () => {
            const current = store.routeDraft;
            const points = current.points.map((point, j) => j === i ? { ...point, [key]:el.value } : point);
            const edited = { ...current, points };
            store.routeDraft = edited;
            if (store.routeCheck?.draft === current) store.routeCheck.draft = edited;
            if (lastDraft === current) lastDraft = edited;
            render();
          };
          return el;
        };
        const pointName = textField(Object.assign(document.createElement('input'), { type:'text' }),
          'name', 'route-point-name', 40, `第 ${i + 1} 站名称（可选）`);
        const pointNote = textField(Object.assign(document.createElement('textarea'), { rows:2 }),
          'note', 'route-point-note', 200, `第 ${i + 1} 站备注（可选）`);
        const button = (text, title, action, disabled = false) => {
          const b = document.createElement('button');
          b.className = 'btn sm'; b.textContent = text; b.title = title;
          b.setAttribute('aria-label', title); b.disabled = disabled;
          b.onclick = () => {
            const current = store.routeDraft;
            const points = [...current.points];
            action(points);
            store.routeReplace = store.routeInsert = null;
            store.routeDraft = { ...current, points, confirmed:false };
            render();
          };
          return b;
        };
        // 拖拽把手：按住上下拖到想要的位置松手（鼠标、触屏都行）
        const grip = document.createElement('span');
        grip.className = 'route-grip'; grip.textContent = '⠿';
        grip.title = '按住拖动调整第 ' + (i + 1) + ' 点的顺序';
        grip.onpointerdown = e => startDrag(e, li, i);
        // 序号：直接改数字就把这一站挪到第几站（Enter / 离开输入框生效，Esc 还原）
        const order = Object.assign(document.createElement('input'), { type:'text', inputMode:'numeric', value:String(i + 1) });
        order.className = 'route-order';
        order.title = '改数字把这一站挪到第几站';
        order.setAttribute('aria-label', `第 ${i + 1} 点的序号`);
        order.onfocus = () => order.select();
        order.onkeydown = e => {
          if (e.key === 'Enter'){ e.preventDefault(); order.blur(); }
          if (e.key === 'Escape'){ order.value = String(i + 1); order.blur(); }
        };
        order.onchange = () => {
          const n = Math.round(+order.value);
          if (!Number.isFinite(n) || n < 1){ order.value = String(i + 1); return; }
          movePoint(i, Math.min(n, store.routeDraft.points.length) - 1);
        };
        // 改位置：点了以后地图上点一下（或按住拖朝向）只替换这一站；再点一次取消
        const place = document.createElement('button');
        const placing = store.routeReplace === i;
        place.className = 'btn sm' + (placing ? ' on' : '');
        place.textContent = placing ? '取消' : '改位置';
        place.title = placing ? '取消重选第 ' + (i + 1) + ' 点位置' : '在地图上重新选第 ' + (i + 1) + ' 点的位置';
        place.setAttribute('aria-label', place.title);
        place.onclick = () => {
          store.routeInsert = null;
          if (store.routeReplace === i){ store.routeReplace = null; setTool('pan'); }
          else { store.routeReplace = i; setTool('route'); }
          render();
        };
        // 插入：在这一站后面插新站（地图上点，可以连着插几站）。最后一站后面就是普通的加点，不需要这个按钮
        const ins = document.createElement('button');
        const inserting = store.routeInsert === i + 1;
        ins.className = 'btn sm' + (inserting ? ' on' : '');
        ins.textContent = inserting ? '取消' : '＋插入';
        ins.title = inserting ? '结束插入' : `在第 ${i + 1} 站和第 ${i + 2} 站之间插入新站`;
        ins.setAttribute('aria-label', ins.title);
        ins.disabled = i === draft.points.length - 1;
        ins.onclick = () => {
          store.routeReplace = null;
          if (inserting){ store.routeInsert = null; setTool('pan'); }
          else { store.routeInsert = i + 1; setTool('route'); }
          render();
        };
        const head = document.createElement('div');
        head.className = 'route-head';
        head.append(grip, order, label);
        li.append(head, ins, place, button('×', '删除第 ' + (i + 1) + ' 点', points => points.splice(i, 1)), pointName, pointNote);
        return li;
      }));
    }
    const preview = store.routeCheck?.draft === draft ? store.routeCheck : null;
    const pointsDirty = draft !== savedDraft;
    const matching = draft.map && draft.map === store.mapCurrent;
    const running = store.task.type !== 'idle' && store.task.status === 'active';
    const route = selected();
    const pDirty = optsDirty(), saveNeeded = needSave();
    remove.hidden = !route;
    remove.disabled = saving || (running && store.task.routeId === route?.id);
    confirm.hidden = draft.confirmed;
    confirm.disabled = !matching || draft.points.length < 2;
    saveRow.hidden = optsBox.hidden = !draft.confirmed;
    optsDirtyTag.hidden = revert.hidden = !pDirty;
    save.disabled = saving || !matching || !draft.confirmed || !name.value.trim() || (!pointsDirty && !saveNeeded && !!route);
    save.textContent = route && draft.id === route.id ? '保存修改' : '保存任务';
    // 出发按钮
    const going = store.prep?.();
    const runningSelected = running && !!route && store.task.routeId === route.id;
    let runMessage = actions.routeGoBlocker(route);
    if (runningSelected) runMessage = '查看下方「导航任务」卡片了解进度。';
    else if (saving) runMessage = '正在保存任务。';
    else if (pointsDirty && draft.points.length) runMessage = draft.confirmed ? '路线改过了：先保存任务。' : '请先确认并保存当前路线。';
    else if (!runMessage && saveNeeded) runMessage = '参数改过还没保存：点「保存并出发」会先保存。';
    if (going && !going.fail) runMessage = '';
    run.disabled = runningSelected || saving || !!going || (!!runMessage && !(saveNeeded && !actions.routeGoBlocker(route)));
    const runText = runningSelected ? '导航任务执行中' : going && !going.fail ? `出发中 · ${going.name}…` : going?.fail ? '出发失败' : saveNeeded ? '保存并出发' : '出发';
    if (run.textContent !== runText) run.textContent = runText;
    if (runHint.textContent !== runMessage) runHint.textContent = runMessage;
    let message = '用「导航点」单击加点，拖拽可指定朝向。至少选两站。';
    if (draft.map && !matching) message = `路线属于地图 ${draft.map}，请先切图。`;
    else if (!draft.confirmed && draft.points.length >= 2) message = '选完后点击「确认路线」，最后一站将标为终点。拖 ⠿ 或改序号调整顺序，「＋插入」在某站后面加站。';
    else if (draft.confirmed){
      message = '路线已确认。调整顺序：拖 ⠿ 或直接改序号；「＋插入」在某站后面加站。';
    }
    if (hint.textContent !== message) hint.textContent = message;
  }
  name.oninput = render;
  return render;
}
