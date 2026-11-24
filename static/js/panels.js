import { setupRouteEditor } from './routes.js';
// 左右两栏、顶栏、HUD 和底部抽屉：全部从 store 读，操作交给 actions
import { $, $$, clamp, deg, fmt, fmtDur, tstr, ACC, TRAJ, MOTOR_LEGS, MOTOR_LEG_CN, MOTOR_JOINTS, MOTOR_WARN, MOTOR_CRIT, MOTOR_VIEWS, motorIdx, motorName, motorLevel, motorErrCount, mapTitle, mapOptText } from './util.js';
import { LAYERS, L } from './layers.js';
import { MODES, MODE_PKG } from './actions.js';

// 内容没变就不动 DOM：高频重建按钮会吞掉正在进行的点击
const setHTML = (sel, html) => { const el = $(sel); if (el._html !== html){ el._html = html; el.innerHTML = html; } };

export function createPanels(store, src, actions, mapView, bus, setTool){
  const ros = src.name === 'ros';
  const run = id => store.profiles[id] === 'run';
  const now = () => src.now();

  /* ---------- 日志 ---------- */
  const logs = []; let logFilter = 'ALL';
  const logEl = $('#logBox');
  function appendLog(l){
    const r = document.createElement('div'); r.className = 'lrow ' + l.level;
    r.innerHTML = `<span class="lt">${l.t}</span><span class="lv ${l.level}">${l.level}</span><span class="ln"></span><span class="lm"></span>`;
    r.children[2].textContent = `[${l.node}]`; r.children[2].title = l.node; r.lastChild.textContent = l.msg; logEl.appendChild(r);
    while (logEl.children.length > 300) logEl.firstChild.remove();
    logEl.scrollTop = logEl.scrollHeight;
  }
  bus.on('log', (level, node, msg) => {
    const l = { t: tstr(), level, node, msg }; logs.push(l); if (logs.length > 400) logs.shift();
    if (logFilter==='ALL' || logFilter===level) appendLog(l);
  });
  $('#logFilters').onclick = e => { const b = e.target.closest('[data-lv]'); if (!b) return; logFilter = b.dataset.lv;
    $$('#logFilters .fchip').forEach(x => x.classList.toggle('on', x===b));
    logEl.textContent = ''; logs.filter(l => logFilter==='ALL'||l.level===logFilter).forEach(appendLog); };

  /* ---------- 图层 ---------- */
  // 图层开关记在本机；真机模式下告诉后端打开了哪些重图层（点云、代价地图、粒子没人看就不算不发）
  try { const s = JSON.parse(localStorage.getItem('go2w.layers')); if (s) for (const l of LAYERS) if (typeof s[l.id] === 'boolean') l.on = s[l.id]; } catch {}
  function renderLayers(){
    $('#layers').innerHTML = LAYERS.map(l => { const na = ros && !l.ros, topic = ros ? (l.ros || l.topic) : l.topic;
      return `<div class="layer ${na?'na':''}" title="${na ? l.note : ''}"><span class="sw" style="background:${l.c==='grad'?'linear-gradient(135deg,#ff4fa3,#6fb6ff)':l.c}"></span>
      <div class="nm">${l.name}${l.id === 'taught' ? '<a class="taught-layer-edit" id="taughtLayerEdit" href="/taught-editor" target="_blank" rel="noopener">编辑</a><small id="taughtLayerMeta">每张地图一条参考</small>' : `<small>${topic}${na ? ' · 真机上没有' : ''}</small>`}</div>
      <button class="tg" role="switch" aria-label="${l.name}" aria-checked="${l.on}" data-l="${l.id}"></button></div>`; }).join('');
    $('#layerCount').textContent = `${LAYERS.filter(l => l.on).length}/${LAYERS.length}`;
    if (src.setLayers) src.setLayers(LAYERS.filter(l => l.on && l.heavy).map(l => l.id));
    renderTaughtLayer();
  }
  function renderTaughtLayer(){
    const map = store.mapCurrent, d = store.taughtByMap[map], el = $('#taughtLayerMeta');
    if (!el) return;
    el.textContent = map ? `${map} · ${d ? `${d.points?.length || 0} 点 / ${Number(d.length || 0).toFixed(1)} m` : '未读取'}` : '先选一张已保存的地图';
    el.title = el.textContent;
    $('#taughtLayerEdit').href = map ? `/taught-editor?map=${encodeURIComponent(map)}` : '/taught-editor';
  }
  $('#layers').addEventListener('click', e => { const b = e.target.closest('[data-l]'); if (!b) return;
    const l = L[b.dataset.l];
    if (ros && !l.ros && !l.on) bus.emit('toast', l.note, 'warn');
    l.on = !l.on; renderLayers();
    try { localStorage.setItem('go2w.layers', JSON.stringify(Object.fromEntries(LAYERS.map(x => [x.id, x.on])))); } catch {} });

  /* ---------- topic 列表 + 检查器 ---------- */
  function renderTopics(){
    const q = $('#topicSearch').value.trim().toLowerCase();
    const list = store.topics.filter(t => !q || t.name.toLowerCase().includes(q) || t.type.toLowerCase().includes(q));
    setHTML('#topics', list.map(t => {
      const shown = !t.alive ? '—' : t.hz == null ? '' : t.hz===0 ? 'latched' : `${t.hz} Hz`;
      return `<button class="topic ${t.alive?'':'off'}" data-t="${t.name}"><i></i><span style="min-width:0"><span class="tn">${t.name}${t.d0?'<span class="dom0">domain 0</span>':''}</span><span class="tt">${t.type.replace('/msg/','/')}</span></span><span class="th">${shown}</span></button>`; }).join('')
      || `<div class="empty">${ros && !store.link.ok ? '未连接' : '没有 topic'}</div>`);
    $('#topicCount').textContent = `${store.topics.filter(t => t.alive).length} 在线 / ${store.topics.length}`;
  }
  $('#topicSearch').addEventListener('input', renderTopics);
  $('#topics').addEventListener('click', e => { const b = e.target.closest('[data-t]'); if (b) openInspector(b.dataset.t); });
  let inspN = 0;
  function openInspector(n){
    const t = store.topics.find(x => x.name===n) || { type: '' }, f = document.createElement('div'); f.className = 'float';
    const k = inspN++ % 5; f.style.left = (window.innerWidth/2 - 160 + k*26) + 'px'; f.style.top = (120 + k*26) + 'px';
    f.innerHTML = `<div class="float-h"><b></b><span></span><button class="ib" style="width:24px;height:24px" aria-label="关闭"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div><pre></pre>`;
    f.querySelector('b').textContent = n; f.querySelector('.float-h span').textContent = t.type.split('/').pop();
    document.body.appendChild(f);
    const pre = f.querySelector('pre'), esc = s => String(s).replace(/[&<>]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;' }[c]));
    const note = s => `<span class="key">${esc(s)}</span>`;
    let close;
    if (src.watch){
      // 真机：后端按需订阅，最多 2 Hz 推 YAML 文本；关掉面板就取消订阅
      pre.innerHTML = note('（等待消息……）');
      let got = false;
      const unwatch = src.watch(n, m => {
        got = true;
        if (m.err){ pre.innerHTML = note(`（${m.err}）`); return; }
        f.querySelector('.float-h span').textContent = `${(m.type || '').split('/').pop()} · ${m.hz} Hz`;
        // YAML：键名灰色、数字青色
        pre.innerHTML = esc(m.text || '').replace(/^(\s*-?\s*)([\w.]+):/gm, (_, a, k) => `${a}<span class="key">${k}:</span>`)
          .replace(/(:\s|- |\[|, )(-?\d+(\.\d+)?(e-?\d+)?)(?=[\s,\]]|$)/gm, (_, a, num) => `${a}<span class="num">${num}</span>`);
      });
      const quiet = setTimeout(() => { if (!got) pre.innerHTML = note('（5 s 内没有收到消息：发布者可能很久才发一次，比如 latched 的 /map）'); }, 5000);
      close = () => { clearTimeout(quiet); unwatch(); };
    } else {
      const upd = () => {
        const tt = store.topics.find(x => x.name===n);
        if (!tt || !tt.alive){ pre.innerHTML = note('（没有发布者：相关进程没有运行）'); return; }
        const lines = src.inspect(n);
        pre.innerHTML = lines ? lines.map(([k, v]) => `<span class="key">${esc(k)}:</span> ${typeof v==='number' ? `<span class="num">${v}</span>` : esc(v)}`).join('\n')
          : note('（模拟模式没有模拟这个 topic 的内容）');
      };
      upd(); const iv = setInterval(upd, 200);
      close = () => clearInterval(iv);
    }
    f.querySelector('button').onclick = () => { close(); f.remove(); };
    const h = f.querySelector('.float-h');
    h.addEventListener('pointerdown', e => { if (e.target.closest('button')) return; const ox = e.clientX - f.offsetLeft, oy = e.clientY - f.offsetTop; h.setPointerCapture(e.pointerId);
      const mv = ev => { f.style.left = clamp(ev.clientX-ox, 0, window.innerWidth-60)+'px'; f.style.top = clamp(ev.clientY-oy, 0, window.innerHeight-40)+'px'; };
      const up = () => { h.removeEventListener('pointermove', mv); h.removeEventListener('pointerup', up); };
      h.addEventListener('pointermove', mv); h.addEventListener('pointerup', up); });
  }

  /* ---------- 运行档位 ---------- */
  const OVERLAY = [['bag', '录包', 'ros2 bag record']];
  const uptime = id => run(id) && store.since[id] != null ? fmtDur(now() - store.since[id]) : '';
  function renderProfiles(){
    setHTML('#modes', Object.entries(MODES).map(([id, name]) => { const st = store.profiles[id];
      const meta = st==='run' ? uptime(id) : st==='starting' ? '启动中…' : st==='stopping' ? '停止中…' : MODE_PKG[id];
      return `<button class="mode" data-s="${st}" data-mode="${id}"><b><i></i>${name}</b><small>${meta}</small></button>`; }).join(''));
    setHTML('#procs', OVERLAY.map(([id, name, pkg]) => { const st = store.profiles[id], busy = st==='starting'||st==='stopping';
      const meta = st==='run' ? uptime(id) : st==='starting' ? '启动中…' : st==='stopping' ? '停止中…' : '未运行';
      return `<div class="proc" data-s="${st}"><i></i><div class="pn">${name}<small>${pkg}</small></div><div class="pm">${meta}</div>
        <button class="btn sm ${st==='run'?'ghost':''}" data-p="${id}" ${busy?'disabled':''}>${st==='run'?'停止':'启动'}</button></div>`; }).join(''));
    setHTML('#nodes', store.nodes.map(n => `<div class="node ${n.up?'up':''}"><i></i><span>${n.name}</span></div>`).join(''));
    $('#modeStop').disabled = !actions.curMode();
  }
  $('#modes').onclick = e => { const b = e.target.closest('[data-mode]'); if (b) actions.setMode(b.dataset.mode); };
  $('#procs').onclick = e => { const b = e.target.closest('[data-p]'); if (b) actions.toggle(b.dataset.p); };
  $('#modeStop').onclick = () => actions.setMode(null);
  $('#simDrop').onclick = () => actions.simulateDrop();

  /* ---------- 任务卡片 ---------- */
  // 步骤由数据源给出：模拟器能看到所有内部阶段，真机只能从 Nav2 行为树日志里看到规划 / 跟随 / 恢复
  const renderRoutes = setupRouteEditor(store, actions, setTool);
  const PHASES = src.steps.map(s => s[0]);
  $('#steps').innerHTML = src.steps.map(([p, name, note]) => `<li data-p="${p}">${name}<small>${note}</small></li>`).join('');
  if (!src.can('pause')){ $('#btnPause').title = '真机暂不支持暂停：可以取消任务，或者锁定底盘'; }
  function renderTask(){
    const T = store.task, idle = T.type==='idle', done = !idle && T.status && T.status!=='active';
    $('#taskBody').hidden = idle; $('#taskEmpty').hidden = !idle;
    let st = 'run', label = '执行中';
    if (idle){ st = ''; label = '空闲'; }
    else if (T.status==='aborted'){ st = 'crit'; label = '失败'; }
    else if (T.status==='canceled'){ st = ''; label = '已取消'; }
    else if (T.phase==='arrived' || T.status==='succeeded'){ st = 'ok'; label = '已完成'; }
    else if (store.estop){ st = 'crit'; label = '急停'; }
    else if (T.paused){ st = 'warn'; label = '已暂停'; }
    else if (T.phase==='recover'){ st = 'warn'; label = '恢复中'; }
    else if (!actions.canMove() && T.phase!=='plan'){ st = 'warn'; label = !store.armed ? '等待解锁' : '底盘不可用'; }
    const pill = $('#taskPill'); pill.dataset.s = st; pill.textContent = label;
    $('#btnPause').disabled = idle || done || !src.can('pause') || T.type === 'route'; $('#btnPause').textContent = T.paused ? '继续' : '暂停';
    $('#btnCancel').disabled = idle || done;
    $('#taskSpeed').hidden = idle || done;          // 临时调速：只在任务执行中
    if (idle) return;
    if (T.type==='route'){
      $('#taskTitle').textContent = T.name;
      const lap = !T.roundTrip ? '' : `${T.loops ? `第 ${T.loop} / ${T.loops} 圈` : `第 ${T.loop} 圈（一直循环）`}${T.back ? ' · 回到起点' : ''} · `;
      $('#taskSub').textContent = T.message || (T.phase === 'checking' ? '出发前清除旧障碍标记'
        : T.phase === 'dwell' && T.dwellLeft != null ? `${lap}第 ${T.point} / ${T.count} 站${T.pointName ? ' · ' + T.pointName : ''} 停留中 · 还剩 ${Math.ceil(T.dwellLeft)} s`
        : `${lap}正在前往第 ${T.point} / ${T.count} 站${T.pointName ? ' · ' + T.pointName : ''}`);
    }
    else if (T.type==='external'){ $('#taskTitle').textContent = '外部导航目标'; $('#taskSub').textContent = 'RViz 或命令行发出的目标 ·「取消」同样有效'; }
    else { const g = T.goal; $('#taskTitle').textContent = '单点导航'; $('#taskSub').textContent = `目标 (${g.x.toFixed(2)}, ${g.y.toFixed(2)}) · ${deg(g.th).toFixed(0)}°`; }
    const ph = ['dwell','arrived'].includes(T.phase) || T.status==='succeeded' ? 'arrive' : T.phase, rem = T.remaining;
    $('#progBar').style.width = T.type === 'route'
      ? `${(T.status === 'succeeded' ? 1 : T.progress ?? (T.point - 1) / T.count) * 100}%`
      : rem!=null && T.total ? (100*clamp(1 - rem/T.total, 0, 1)).toFixed(1)+'%' : '0%';
    $('#tRem').textContent = rem==null ? '--' : rem.toFixed(1)+' m';
    const eta = T.eta != null ? T.eta : rem != null ? rem/Math.max(.2, store.params.vmax*.8) : null;
    $('#tEta').textContent = eta==null || done ? '--' : rem < .05 ? '0 s' : Math.ceil(eta)+' s';
    $('#tEl').textContent = T.elapsed==null ? '--' : fmtDur(T.elapsed); $('#tRec').textContent = T.recoveries || 0;
    const ci = PHASES.indexOf(ph);
    $$('#steps li').forEach((li, i) => { li.className = i<ci ? 'done' : i===ci ? (ph==='arrive' ? 'done' : 'cur') : ''; });
  }
  $('#btnPause').onclick = () => actions.pause();
  $('#btnCancel').onclick = () => actions.cancel();

  /* ---------- 底盘控制 ---------- */
  const ICON_LOCK = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/></svg>';
  const ICON_OPEN = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 017.5-2"/></svg>';
  function renderArm(){
    const s = store.estop ? 'estop' : store.armed==null ? 'unknown' : store.armed ? 'armed' : 'locked';
    $('#armBox').dataset.s = s; $('#armIco').innerHTML = s==='armed' ? ICON_OPEN : ICON_LOCK;
    $('#armT').textContent = { armed:'已解锁', estop:'急停锁定', locked:'已锁定', unknown:'状态未知' }[s];
    $('#armS').textContent = { armed:`执行 /cmd_vel · 超时 ${store.params.tmo.toFixed(1)} s 自动停车`, estop:'先解除急停', locked:'不执行任何速度指令', unknown:'cmd_bridge 没有运行' }[s];
    const b = $('#armBtn'); b.textContent = store.armed ? '锁定' : '解锁'; b.className = 'btn sm ' + (store.armed ? '' : 'pri'); b.disabled = store.estop || store.armed==null;
    $('#estopBanner').hidden = !store.estop;
  }
  $('#armBtn').onclick = () => actions.arm();
  $('#estopBtn').onclick = () => actions.estop();
  $('#estopRelease').onclick = () => actions.releaseEstop();
  // 「导航任务」卡片上的临时调速 + 调试区的指令超时：滑条平时跟着实际参数走（真机上来自 cmd_bridge）；
  // 拖动中不打断，松手后立即设给 cmd_bridge，只影响这次运行（任务里保存的参数不变），失败就弹回实际值。
  // cmd_bridge 没在跑时先记在滑条上（held），起来了再设过去
  const bridgeUp = () => store.source !== 'ros' || run('navigation') || run('bridge');
  const sliders = [['#slV','#oV','vmax','m/s',2], ['#slW','#oW','wmax','rad/s',1], ['#slT','#oT','tmo','s',1]].map(([id, out, key, unit, dec]) => {
    const el = $(id), show = v => $(out).textContent = `${(+v).toFixed(dec)} ${unit}`;
    let busy = false, held = false;
    el.oninput = () => { busy = true; show(el.value); };
    const push = () => { busy = true; held = false; Promise.resolve(actions.setParam(key, +el.value)).finally(() => { busy = false; }); };
    el.onchange = () => { if (bridgeUp()) push(); else { held = true; busy = false; } };
    return () => {
      if (held){ if (bridgeUp() && !busy) push(); return; }
      if (!busy && store.params[key] != null && +el.value !== store.params[key]){ el.value = store.params[key]; show(el.value); }
    };
  });
  const syncSliders = () => sliders.forEach(f => f());

  /* ---------- 顶栏 / HUD / 机器人卡片 ---------- */
  function chip(id, s, txt){ const c = $(id); c.dataset.s = s; c.querySelector('b').textContent = txt; }
  // 运控姿态（store.ctrl，后端 ctrl_view）：顶栏「姿态」+ 机器人卡片上的站起 / 趴下 / 保持站立
  const POSE = { Passive: '趴着', FixStand: '站立', Velocity: '行走' };
  $('#poseUp').onclick = () => actions.standUp();
  $('#poseDown').onclick = () => actions.lieDown();
  $('#poseHold').onclick = () => actions.standHold(!store.ctrl?.hold);
  $('#climbBtn').onclick = () => actions.climb(!store.ctrl?.climb);
  let policyPicking = false;   // 确认框开着时不让渲染把下拉框改回去
  $('#policySel').onchange = e => { policyPicking = true; actions.setPolicy(e.target.value, () => { policyPicking = false; renderPose(); }); };
  const escPol = s => String(s).replace(/[<>&"]/g, '');
  function renderPose(){
    const C = store.ctrl, on = !!C?.on && store.link.ok;
    $('#chPose').hidden = $('#poseRow').hidden = !on;
    if (!on) $('#policyRow').hidden = $('#climbRow').hidden = true;
    if (!on) return;
    const name = C.state == null ? '运控离线' : POSE[C.state] || String(C.state).replace(/[<>&"]/g, '');
    const txt = C.op === 'up' ? '站起中…' : C.op === 'down' ? '趴下中…' : C.op === 'policy' ? '切换策略中…' : C.state === 'FixStand' && !C.ready ? '站起中…'
      : C.lieIn != null ? `${Math.ceil(C.lieIn)} s 后趴下` : name;
    chip('#chPose', C.state == null ? 'crit' : C.op || C.lieIn != null ? 'warn' : C.state === 'Velocity' ? 'ok' : '', txt);
    renderPolicy(C);
    renderClimb(C);
    const pt = $('#poseTxt');
    pt.className = 'pose-txt' + (C.lieIn != null ? ' warn' : '');
    pt.innerHTML = C.op === 'policy' ? '姿态 <b>切换策略中…</b> · 运控重启'
      : C.state == null ? '<b class="crit">运控离线</b> · 收不到运控状态'
      : C.lieIn != null ? `空闲 · <b>${Math.ceil(C.lieIn)} s 后原地趴下</b>`
      : `姿态 <b>${txt}</b>${C.hold ? ' · 保持站立' : ''}`;
    const busy = !!C.op || C.state == null;
    $('#poseUp').hidden = C.state === 'Velocity' && C.ready;
    $('#poseUp').disabled = busy || store.estop;
    $('#poseDown').hidden = C.state === 'Passive';
    $('#poseDown').disabled = busy && C.op !== 'up';   // 站起过程中可以打断
    $('#poseHold').hidden = !(C.lieIn != null || C.hold);
    $('#poseHold').textContent = C.hold ? '取消保持' : '保持站立';
  }
  // 爬台模式：后端 ctrl.dual = 运控回报的 {policy, switching, req}，ctrl.climb = 网页开着爬台模式 {since, max}
  function renderClimb(C){
    const D = C.dual, row = $('#climbRow');
    row.hidden = !D;
    if (!D) return;
    const on = !!C.climb, climbing = D.policy === 'climb';
    const st = D.switching ? (climbing ? '切到 climb 中…' : '切回 rough 中…')
      : D.req && !climbing ? '等速度降下来再切（vx≤0.5 vy≤0.15 wz≤0.15）'
      : climbing ? (on ? 'climb（爬台中）' : 'climb（遥控器 RB）')
      : on ? '已请求，等运控切换…' : 'rough';
    const left = on && C.climb.max ? ` · 剩 ${Math.max(0, Math.ceil((C.climb.max - C.climb.since) / 60))} 分钟` : '';
    const t = $('#climbTxt');
    t.className = 'pose-txt' + (climbing || on ? ' warn' : '');
    t.innerHTML = `爬台 <b>${st}</b>${left}`;
    const b = $('#climbBtn');
    b.textContent = on ? '关爬台模式' : '开爬台模式';
    b.classList.toggle('danger', on);
    b.disabled = !on && (!(C.state === 'Velocity' && C.ready) || !!C.op || store.estop);
    setAttr(b, 'title', on ? '切回 rough' : '切到 climb 策略（只能站着时开）');
  }
  function renderPolicy(C){
    const sel = $('#policySel'), list = C.policies || [];
    $('#policyRow').hidden = !list.length && !C.policy;
    const lying = C.state === 'Passive' && C.ready;
    const dis = !lying || !!C.op || list.length < 2;
    if (sel.disabled !== dis) sel.disabled = dis;
    if (policyPicking) return;   // 确认框开着 / 正在切：别把选中值改回去
    const opts = list.includes(C.policy) || !C.policy ? list : [C.policy, ...list];
    const key = opts.join('|');
    if (sel.dataset.k !== key){
      sel.innerHTML = opts.map(p => `<option value="${escPol(p)}">${escPol(p.replace(/^mjlab_go2w_/, ''))}</option>`).join('');
      sel.dataset.k = key;
    }
    // 4 Hz 重绘：下拉框展开（有焦点）时别动选中值，不然高亮一直被拉回当前策略、闪个不停；其它也只在变了才写
    if (C.policy && sel.value !== C.policy && document.activeElement !== sel) sel.value = C.policy;
    setAttr(sel, 'title', C.policy ? `当前：${C.policy}` + (lying ? '' : '（只能趴着时切换）') : '读不到当前策略');
  }
  function renderChips(){
    const lk = store.link, anyMode = !!actions.curMode();
    chip('#chLink', lk.ok ? (lk.ms==null ? 'warn' : lk.ms>40 ? 'warn' : 'ok') : 'crit', lk.ok ? (lk.ms==null ? '无响应' : `${lk.ms.toFixed(0)} ms`) : (ros ? '连接中' : '断开'));
    const Lc = store.loc;
    if (!Lc.running) chip('#chLoc', run('mapping') ? 'ok' : '', run('mapping') ? '建图' : '未运行');
    else if (Lc.std==null) chip('#chLoc', 'ok', '运行中');
    // 主要看 scan 和地图的匹配率。σ 只用来认「刚设完初始位姿」（此时 σ = 0.5 m）：
    // AMCL 只在机器人移动后才更新，静止时 σ 不变；真机定位正常时也有约 0.15 m（11-02 实测，匹配率 82–90%）
    else if (Lc.std>.3) chip('#chLoc', 'warn', '收敛中');
    else if (Lc.match!=null && Lc.match<60) chip('#chLoc', 'crit', '匹配差');
    else chip('#chLoc', 'ok', '良好');
    chip('#chOdom', store.odomOk ? 'ok' : anyMode ? 'crit' : '', store.odomOk ? '健康' : anyMode ? '不可用' : '未运行');
    renderPose();
    chip('#chBase', store.estop ? 'crit' : store.armed==null ? '' : store.armed ? 'ok' : 'warn', store.estop ? '急停' : store.armed==null ? '--' : store.armed ? '已解锁' : '锁定');
    const sy = store.sys;
    chip('#chCpu', sy.cpu==null ? '' : sy.cpu>80 ? 'crit' : sy.cpu>60 ? 'warn' : 'ok', sy.cpu==null ? '--' : `${sy.cpu.toFixed(0)}%`);
    chip('#chMem', sy.mem==null ? '' : sy.mem/sy.memTotal>.85 ? 'warn' : 'ok', sy.mem==null ? '--' : `${sy.mem.toFixed(1)} / ${sy.memTotal} G`);
    $('#chRec').hidden = !run('bag'); if (run('bag')) $('#chRec').querySelector('b').textContent = uptime('bag');
    $('#mapTag').hidden = !run('mapping') || mapView.tool==='teleop';
    const T = store.task;
    const rs = !lk.ok && ros ? ['','离线'] : store.estop ? ['crit','急停'] : mapView.tool==='teleop' ? ['run','遥控中'] : run('mapping') ? ['run','建图中']
      : T.type!=='idle' && T.phase!=='arrived' ? ['run','导航中'] : anyMode ? ['ok','待命'] : ['','未启动'];
    const rp = $('#robotPill'); rp.dataset.s = rs[0]; rp.textContent = rs[1];
    const R = store.robot;
    $('#sBat').textContent = store.battery==null ? '--' : `${store.battery}%`;
    renderMotorTemp();
    $('#sSpd').textContent = R.valid ? Math.hypot(R.v, R.vy).toFixed(2) : '--';
    $('#sOdo').textContent = store.odo==null ? '--' : `${store.odo.toFixed(0)} m`;
    $('#sFrame').textContent = R.valid ? store.fixed : '--';
    // 三项各自有数据就显示（真机上匹配率来自 scan，σ 来自 /amcl_pose，不一定同时有）
    const locVal = (id, v, dec, unit) => { if (Lc.running && v != null) $(id).innerHTML = `${v.toFixed(dec)}<small>${unit}</small>`; else $(id).textContent = '--'; };
    locVal('#lStd', Lc.std, 3, 'm'); locVal('#lYaw', Lc.yaw, 1, '°'); locVal('#lMatch', Lc.match, 0, '%');
    // 连接 / 等待地图提示
    const off = $('#offline');
    if (ros && !lk.ok){ off.hidden = false; $('#offT').textContent = '机器人未连接，正在重试…'; $('#offS').textContent = '机器人关机或网络断开时会显示这个提示，恢复后自动连接'; }
    else if (ros && !store.map){ off.hidden = false; $('#offT').textContent = '已连接，等待 /map'; $('#offS').textContent = '建图、定位或导航运行后才会有地图'; }
    else off.hidden = true;
  }
  // 电机温度：一行显示最热的电机（超过阈值变黄 / 红），点开是 4 条腿 × 髋 / 大腿 / 小腿 / 轮 的表，展开状态记在本机浏览器。
  // 表上面的下拉切换看 温度 / 错误标志 / 力矩（选的哪个也记在本机）；表开着且看力矩时让后端读得勤一点
  const mtRow = $('#mtRow'), mtGrid = $('#mtGrid'), mtBox = $('#mtBox'), mtSel = $('#mtView'), mtCells = [];
  let mtView = 'temp';
  {
    mtSel.innerHTML = MOTOR_VIEWS.map(([k, cn]) => `<option value="${k}">${cn}</option>`).join('');
    try { const v = localStorage.getItem('go2w.mtView'); if (MOTOR_VIEWS.some(([k]) => k === v)) mtView = v; } catch {}
    mtSel.value = mtView;
    mtSel.onchange = () => { mtView = mtSel.value; try { localStorage.setItem('go2w.mtView', mtView); } catch {} syncFast(); renderMotorTemp(); };
    mtGrid.innerHTML = '<span></span>' + MOTOR_JOINTS.map(j => `<span>${j}</span>`).join('')
      + MOTOR_LEGS.map((l, li) => `<span class="lg">${MOTOR_LEG_CN[l]}</span>` + MOTOR_JOINTS.map((_, j) => `<b data-i="${motorIdx(li, j)}">--</b>`).join('')).join('');
    mtGrid.querySelectorAll('b').forEach(b => { mtCells[+b.dataset.i] = b; b.title = motorName(+b.dataset.i); });
    let open = false; try { open = localStorage.getItem('go2w.mtOpen') === '1'; } catch {}
    const syncFast = () => src.motorFast?.(open && mtView === 'tau');
    const setOpen = v => { open = v; mtBox.hidden = !v; mtRow.setAttribute('aria-expanded', String(v)); try { localStorage.setItem('go2w.mtOpen', v ? '1' : '0'); } catch {} syncFast(); };
    setOpen(open);
    mtRow.onclick = () => setOpen(!open);
    mtRow.title = `点开看全部电机温度、错误标志和力矩（温度 ≥${MOTOR_WARN} ℃ 黄，≥${MOTOR_CRIT} ℃ 红）`;
  }
  const hex = v => '0x' + v.toString(16).toUpperCase();
  // 每种视图：单元格文字、颜色等级、鼠标悬停说明
  function motorCell(view, T, I, i){
    const name = motorName(i);
    if (view === 'temp') return [T[i], motorLevel(T[i]) === 'ok' ? '' : motorLevel(T[i]), `${name} ${T[i]} ℃`];
    if (!I) return ['--', '', name];
    if (view === 'err'){
      const e = I.err[i];
      return [e ? hex(e) : '0', e ? 'crit' : 'dim', `${name} 错误标志 ${e}（${hex(e)}）${e ? '' : '：正常'}`];
    }
    if (view === 'tau'){
      const v = I.tau?.[i];
      if (v == null) return ['--', '', name];
      const s = Math.abs(v) < 0.05 ? '0.0' : (v > 0 ? '+' : '') + v.toFixed(1);   // 别显示 -0.0
      return [s, Math.abs(v) < 0.5 ? 'dim' : '', `${name} 估计力矩 ${v.toFixed(2)} N·m（正负 = 方向）`];
    }
    return ['--', '', name];
  }
  // 只在内容变了才写 DOM：这里 4 Hz 重绘，下拉框展开时改它的选项（哪怕文字一样）Chrome 会重建弹出列表 → 闪、点不中
  const setTxt = (el, v) => { v = String(v); if (el.textContent !== v) el.textContent = v; };
  const setAttr = (el, k, v) => { if (el.getAttribute(k) !== v) el.setAttribute(k, v); };
  let mtSumHtml = null;
  const MT_NOTE = { temp: '℃', err: '0 = 正常', tau: 'N·m · 估计值' };
  function renderMotorTemp(){
    const T = store.motorTemp, I = store.motorInfo, nErr = motorErrCount(I);
    setTxt($('#mtNote'), MT_NOTE[mtView]);
    setTxt(mtSel.querySelector('option[value="err"]'), nErr ? `错误标志（${nErr}）` : '错误标志');
    if (!T){ mtSumHtml = null; setTxt($('#mtSum'), '--'); setAttr(mtRow, 'data-s', ''); mtCells.forEach(b => { setTxt(b, '--'); setAttr(b, 'data-s', ''); }); return; }
    let k = 0; T.forEach((v, i) => { if (v > T[k]) k = i; });
    const hot = T.filter(v => v >= MOTOR_WARN).length;
    // 一行放不下：有报错时省掉最热电机的名字，保证温度数字露出来
    const html = nErr
      ? `<small class="mt-err">${nErr} 个报错</small><small>${hot > 1 ? `${hot} 偏热 · ` : ''}最高</small>${T[k]} ℃`
      : `<small>${hot > 1 ? `${hot} 个偏热 · ` : ''}最高 ${motorName(k)}</small>${T[k]} ℃`;
    if (html !== mtSumHtml){ mtSumHtml = html; $('#mtSum').innerHTML = html; }
    setAttr(mtRow, 'data-s', nErr ? 'crit' : motorLevel(T[k]) === 'ok' ? '' : motorLevel(T[k]));
    mtCells.forEach((b, i) => { const [txt, s, tip] = motorCell(mtView, T, I, i); setTxt(b, txt); setAttr(b, 'data-s', s); setAttr(b, 'title', tip); });
  }
  function renderHud(){
    const R = store.robot, m = store.map;
    $('#hMap').textContent = m ? `${mapTitle(store.mapLabels, m.name)} · ${m.cells} · ${+m.res.toFixed(3)} m` : '--';
    $('#hPose').textContent = R.valid ? `x ${fmt(R.x)}  y ${fmt(R.y)}  θ ${fmt(deg(R.th),1)}°` : '--';
    $('#hVel').textContent = R.valid ? `vx ${fmt(R.v)}  vy ${fmt(R.vy)}  wz ${fmt(R.w)}` : '--';
    $('#hFrame').textContent = `${store.fixed} → base_footprint`;
    const m1 = [.5,1,2,5,10].find(c => c*mapView.view.s >= 60) || 10;
    $('#scaleBar').style.width = (m1*mapView.view.s).toFixed(0)+'px'; $('#scaleTxt').textContent = m1+' m';
  }

  /* ---------- 示教导航开关（示教采集卡片顶上） ---------- */
  // 开：下次开导航时带示教节点（常驻约 0.6 个核）；关：不带。导航正在跑时改了要重开导航才生效。
  function renderTaughtNav(){
    const t = store.taughtNav, box = $('#taughtNavOn'), st = $('#taughtNavState');
    if (!box) return;
    if (!t){ box.disabled = true; st.textContent = '后端没有这个开关（旧版本）'; return; }
    if (document.activeElement !== box) box.checked = !!t.on;
    box.disabled = !!t.forced || (!t.installed && !t.on);
    const nodeUp = store.taughtNavigation === true, nav = run('navigation');
    st.textContent = t.forced ? `由环境变量 GO2W_TAUGHT=${t.forced} 固定`
      : !t.installed && !t.on ? '后端没有示教算法包'
      : t.on ? (nodeUp ? '示教节点运行中（约 0.6 核）' : nav ? '下次出发会先重开导航带上示教' : '下次开导航时启动')
      : nodeUp ? '已关闭，重开导航后示教节点才会停' : '已关闭（开导航不启动示教节点）';
  }

  /* ---------- 录包 ---------- */
  function renderMaps(){
    // 录包进行中（或正在启停）时最新的一个就是正在录的，不能删
    const bs = store.profiles.bag, recording = bs !== 'stop';
    // 后端告诉哪些包还在写：网页录包是分段的，列表里的都已写完；命令行手动录的才把最新的一个当成在录
    const liveBag = recording ? (store.bagLive || [])[0] ?? null : null;
    const rb = store.rollback;
    $('#rbOn').checked = rb.on;
    $('#rbState').textContent = rb.saving ? '保存中…' : rb.run ? `缓存最近 ${rb.seconds} 秒` : rb.on ? '启动建图 / 定位 / 导航后开始' : '已关闭';
    $('#rbSave').textContent = `保存前 ${rb.seconds} 秒＋后 ${rb.after ?? 10} 秒`;
    $('#rbSave').disabled = !store.link.ok || !rb.run || rb.saving || actions.rollbackCapture.state.busy || !!actions.rollbackCapture.state.pending;
    setHTML('#bagList', store.bags.length ? store.bags.map((b,i) => {
      const live = b.name === liveBag, u = store.uploads[b.name] || {}, upping = u.state === 'run';
      const anyUp = Object.values(store.uploads).some(x => x.state === 'run');
      const upTitle = live ? '正在录制，先停止录包' : upping ? `上传中 ${u.pct}%` : anyUp ? '正在上传别的包' : u.state === 'done' ? `已于 ${u.msg} 上传到 166:~/go2w_bags，点击重新上传` : u.state === 'fail' ? `上次上传失败：${u.msg}，点击重试` : `上传 ${b.name} 到 166:~/go2w_bags`;
      const up = `<button class="ib bag-up ${u.state || ''}" data-up="${i}" ${live || anyUp ? 'disabled' : ''} title="${upTitle.replace(/"/g, '&quot;')}" aria-label="上传录包 ${b.name}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${u.state === 'done' ? '<path d="M5 12.5l4.5 4.5L19 7.5"/>' : '<path d="M12 16V4M7 9l5-5 5 5M5 20h14"/>'}</svg></button>`;
      const st = upping ? `<span class="mm bag-st run">上传 ${u.pct}%</span>` : u.state === 'fail' ? `<span class="mm bag-st fail" title="${(u.msg || '').replace(/"/g, '&quot;')}">上传失败</span>` : u.state === 'done' ? '<span class="mm bag-st done">已上传</span>' : '<span class="mm"></span>';
      const del = `<button class="ib bag-del" data-del="${i}" ${live || upping ? `disabled title="${live ? '正在录制，先停止录包' : '正在上传，等传完再删'}"` : `title="删除 ${b.name}"`} aria-label="删除录包 ${b.name}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg></button>`;
      return `<div class="mrow bag"><span class="mn">${b.name}</span><span class="mm">${live ? '录制中' : fmtDur(b.dur)}</span><span class="mm">${b.size>1024?(b.size/1024).toFixed(1)+' GB':b.size.toFixed(0)+' MB'}</span>${st}${up}${del}</div>`; }).join('')
      : `<div class="empty">data/bags 里没有录包</div>`);
    $('#bagBtn').textContent = bs==='run' ? '停止录包' : bs==='stop' ? '开始录包' : bs==='starting' ? '启动中…' : '停止中…';
    $('#bagBtn').title = bs==='run' && store.bagSegment ? `正在录：每 ${store.bagSegment} 秒存一个包，包名按开始时间` : `开始后每 30 秒存一个包，包名按开始时间`;
    bagSync(); bagListRender();
    const noneSel = store.bagTopics === '';
    $('#bagSelBtn').disabled = bs !== 'stop';
    if (bs !== 'stop' && !$('#bagSelPop').hidden) bagOpen(false);
    $('#bagSelBtn').title = bs !== 'stop' ? '录包中不能改，先停止录包' : '';
    $('#bagBtn').disabled = bs==='starting' || bs==='stopping' || (bs==='stop' && noneSel);
    if (bs==='stop' && noneSel) $('#bagBtn').title = '先在左边至少勾选一个 topic';
  }
  $('#saveMap').onclick = () => actions.saveMap($('#mapName').value.trim() || 'map');
  $('#alignSelect').onclick = () => setTool('align');
  $('#alignHorizontal').onclick = () => mapView.alignWall('horizontal');
  $('#alignVertical').onclick = () => mapView.alignWall('vertical');
  $('#alignReset').onclick = () => mapView.resetAlignment();
  $('#saveAlignedMap').onclick = () => actions.saveAlignedMap($('#mapName').value.trim());
  function renderAlignment(){
    const a = store.alignment, busy = store.mapSaving || store.mapBusy;
    const selected = a.points.length === 2;
    const angle = (a.angle*180/Math.PI).toFixed(1);
    $('#alignStatus').textContent = selected
      ? `墙段 ${Math.hypot(a.points[1].x-a.points[0].x,a.points[1].y-a.points[0].y).toFixed(2)} m · 显示方向 ${angle}° · ${a.target === 'vertical' ? '竖直' : '水平'}`
      : a.points.length ? '已选第一点，再沿墙选择第二点。'
      : `显示方向 ${angle}°（每张地图单独保存）· Shift 或右键拖动地图旋转，或沿墙选两点精确对齐`;
    // 方向变了之后提示一会儿；平时不常驻，免得挡地图
    const flashing = performance.now() < (a.flashUntil || 0);
    $('#alignPreview').hidden = !flashing;
    $('#alignPreview').textContent = `地图显示方向 ${angle}° · 只转画面，位姿读数仍是地图坐标`;
    $('#alignSelect').disabled = busy || !store.map;
    $('#alignHorizontal').disabled = $('#alignVertical').disabled = busy || !selected;
    $('#alignReset').disabled = busy || (Math.abs(a.angle) < 1e-6 && !a.points.length);
    $('#saveAlignedMap').disabled = busy || !run('mapping') || Math.abs(Math.atan(Math.tan(a.angle))) < 1e-4;
    $('#saveAlignedMap').textContent = busy ? '保存中…' : '保存对齐地图';
    $('#saveMap').disabled = busy || store.contPaused || store.contBusy;
    $('#mapName').disabled = busy;
  }
  /* ---------- 续建 ---------- */
  // 基准图只能选带完整 posegraph + data 的：slam_toolbox 只能从这两件恢复图，
  // 光有 pgm/yaml 的图（lab_20261021 就是）续建无从谈起。
  let contBaseFilled = null;
  function renderCont(){
    const c = store.cont, busy = store.mapSaving || store.mapBusy;
    // 基准图下拉：列出所有地图，标一下有没有 posegraph（后端 preflight 会把关，这里只做提示）
    const names = store.maps.map(m => m.name);
    const contKey = names.map(n => mapOptText(store.mapLabels, n)).join('\n');
    if (contBaseFilled !== contKey){
      contBaseFilled = contKey;
      const sel = $('#contBase'), keep = sel.value;
      sel.replaceChildren(...names.map(n => new Option(mapOptText(store.mapLabels, n), n)));
      if (names.includes(keep)) sel.value = keep;
      else { const def = names.find(n => n === 'lab_20261116') || names[0]; if (def) sel.value = def; }
    }
    const base = $('#contBase').value;
    const nameIn = $('#contName');
    // 自动保存当基准时别沿用 autosave_ 前缀：续建结果是正式地图，不该看着像自动保存（11-19 被清理误删过）
    if (!nameIn.value && base) nameIn.value = `${base.replace(/^autosave_/, 'map_')}_c1`;
    $('#contPre').disabled = busy || !base;
    const starting = store.contBusy || !!store.contStarting || (!c && Date.now() - (store.contStartOkAt || 0) < 3000);
    const canStart = !busy && !starting && run('mapping') && !!base && !c?.ready;
    $('#contStart').disabled = !canStart;
    $('#contStartParking').disabled = !canStart;
    $('#contStart').textContent = starting ? '正在开始…' : '开始续建';
    const manual = contManualPose();
    $('#contStartManual').disabled = !canStart || !manual;
    ['#contMx', '#contMy', '#contMyaw'].forEach(id => { $(id).disabled = !canStart; });
    // 停车点记在基准图坐标系里。建图档刚启动时 map 系是 slam_toolbox 新建的，此刻记下来的位置是错的，
    // 而且不会报错——所以只在「导航 / 定位加载了这张图且定好位」或「续建这张图并自检通过」时放开（后端同样把关）
    const parkWhy = parkingBlocked(base, c);
    const pk = $('#contPark');
    pk.disabled = busy || starting || !base || !!parkWhy;
    pk.title = parkWhy || '保存当前位置，供「从停车点开始」使用';
    $('#contRow').hidden = !c;
    $('#contCheck').disabled = !c?.ready || busy || starting;
    $('#contFinish').disabled = !c?.ready || busy || starting || !c.check?.ok;
    $('#contAbort').disabled = !c || starting;
    const st = $('#contStatus');
    let text = '';
    if (starting){
      text = '正在定位、加载基准图并验证旧墙，请保持静止…';
      st.classList.remove('warn');
    } else if (c){
      const chk = c.check;
      text = `${c.base} → ${c.name} · 起点 ${c.source === 'parking' ? '停车点' : c.source === 'globloc' ? '自动定位' : '手动'}`
        + ` (${c.pose[0].toFixed(2)}, ${c.pose[1].toFixed(2)}, ${(c.pose[2]*180/Math.PI).toFixed(0)}°)`
        + (chk ? (chk.ok ? ` · 自检通过（贴合率 ${(chk.fit*100).toFixed(0)}%）` : ` · 采集已暂停：${chk.reason || ''}`) : ' · 正在验证')
        + (c.warn ? ` · ${c.warn}` : '');
      st.classList.toggle('warn', !!c.warn || (chk && !chk.ok));
    } else if (store.contErr){
      text = '没开始：' + store.contErr;
      st.classList.add('warn');
    } else if (store.contPaused && run('mapping')){
      text = '采集已暂停，可以更换基准图或重新给起点。';
      st.classList.add('warn');
    } else if (store.contPre){
      const p = store.contPre;
      text = (p.reasons?.length ? '不能续建：' + p.reasons.join('；') : '可以续建')
        + (p.chain?.length > 1 ? ` · 链 ${p.chain.join(' → ')}` : '')
        + (p.warns?.length ? ` · 注意：${p.warns.join('；')}` : '');
      st.classList.toggle('warn', !p.ok);
    } else {
      text = '选中基准图后点「检查基准」（只查文件，不动机器人）。';
      st.classList.remove('warn');
    }
    if (st.textContent !== text) st.textContent = text;
    renderContCandidates(canStart && store.contResult?.base === base ? store.contResult : null);
  }
  let drawnCandidates = null;
  function renderContCandidates(result){
    const panel = $('#contCandidates');
    panel.hidden = !result?.preview;
    if (panel.hidden || drawnCandidates === result) return;
    drawnCandidates = result;
    const { w, h, res, ox, oy, grid } = result.preview;
    const canvas = $('#contCandidateMap'), scale = Math.min(4, 300 / w);
    canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
    const ctx = canvas.getContext('2d'), cells = atob(grid);
    const colors = ['#167caa', '#ae5334'];
    ctx.save();
    ctx.scale(scale, scale);
    for (let i = 0; i < cells.length; i++){
      const value = cells.charCodeAt(i);
      ctx.fillStyle = value === 255 ? '#a1a9b4' : value >= 65 ? '#25313d' : '#eaf0f5';
      ctx.fillRect(i % w, h - 1 - Math.floor(i / w), 1.1, 1.1);
    }
    ctx.restore();
    $('#contCandidateChoices').replaceChildren(...result.candidates.map(({ pose, score }, i) => {
      const [x, y, yaw] = pose, px = (x - ox) / res * scale, py = (h - (y - oy) / res) * scale;
      ctx.fillStyle = ctx.strokeStyle = colors[i];
      ctx.beginPath(); ctx.arc(px, py, 7, 0, 2 * Math.PI); ctx.fill();
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(px + 15 * Math.cos(yaw), py - 15 * Math.sin(yaw)); ctx.stroke();
      ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(String(i + 1), px, py);
      const button = document.createElement('button');
      button.className = 'btn sm';
      button.textContent = `用候选 ${i + 1}（${score.toFixed(2)}）`;
      button.onclick = () => {
        $('#contMx').value = x.toFixed(3); $('#contMy').value = y.toFixed(3);
        $('#contMyaw').value = (yaw * 180 / Math.PI).toFixed(1);
        renderCont();
      };
      return button;
    }));
  }
  function contManualPose(){
    const v = ['#contMx', '#contMy', '#contMyaw'].map(id => $(id).value.trim());
    if (v.some(x => x === '')) return null;
    const [x, y, deg] = v.map(Number);
    if (![x, y, deg].every(Number.isFinite)) return null;
    return [x, y, deg * Math.PI / 180];
  }
  function parkingBlocked(base, c){
    if (run('mapping')){
      if (!c) return '建图档里还没开始续建：此刻的地图坐标是新建的，不是基准图的，记下来会是错的。请在导航 / 定位里定好位后记，或续建自检通过后记';
      if (c.base !== base) return `正在续建的基准是 ${c.base}，不是 ${base}`;
      if (!c.ready || !c.check?.ok) return '续建自检通过后才能记停车点';
      return '';
    }
    if (run('navigation') || run('localization')){
      if (store.mapCurrent !== base) return `当前加载的是 ${store.mapCurrent || '（未知）'}，不是 ${base}`;
      const m = store.loc?.match;
      if (m == null) return '还没有定位，先设初始位姿';
      if (m < 70) return `定位匹配率 ${m.toFixed(0)}% 低于 70%，先把位置定准`;
      return '';
    }
    return '导航、定位、建图都没在跑，拿不到可信的位置';
  }
  ['#contMx', '#contMy', '#contMyaw'].forEach(id => { $(id).oninput = () => renderCont(); });
  $('#contStartManual').onclick = () => {
    const p = contManualPose();
    if (!p) return;
    actions.contStart($('#contBase').value, $('#contName').value.trim(), p, 'manual');
  };
  $('#contPre').onclick = () => actions.contPreflight($('#contBase').value);
  $('#contBase').onchange = () => { contBaseFilled = null; $('#contName').value = ''; store.contPre = null; store.contErr = null; store.contResult = null; renderCont(); };
  $('#contStart').onclick = () => actions.contStart($('#contBase').value, $('#contName').value.trim(), null, null);
  $('#contStartParking').onclick = () => actions.contStart($('#contBase').value, $('#contName').value.trim(), null, 'parking');
  $('#contPark').onclick = () => actions.contParking($('#contBase').value, false);
  $('#contParkClear').onclick = () => actions.contParking($('#contBase').value, true);
  $('#contCheck').onclick = () => actions.contCheck();
  $('#mapResume').onclick = () => actions.mapResume();
  $('#contFinish').onclick = () => actions.contFinish(null);
  $('#contAbort').onclick = () => actions.contAbort();
  // 录包 topic 下拉多选：记「取消勾选的」，默认一个都没取消 = 全选 = *（之后新冒出来的 topic 也录）；
  // 取消了任何一个就发具体勾选的列表。刷新页面恢复全选。
  const bagOff = new Set(), esc = x => String(x).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]);
  const bagNames = () => [...new Set(store.topics.map(t => t.name))].sort();
  const bagSync = () => {
    const names = bagNames(), on = names.filter(n => !bagOff.has(n));
    store.bagTopics = !bagOff.size ? '*' : on.join(' ');
    const sum = $('#bagSelSum');
    sum.textContent = !bagOff.size ? `全部${names.length ? `（${names.length}）` : ''}` : on.length ? `已选 ${on.length} / ${names.length}` : '未选择';
    sum.classList.toggle('none', !!bagOff.size && !on.length);
    sum.title = bagOff.size ? on.join('\n') : '录所有 topic（含之后新出现的）';
  };
  const bagListRender = force => {
    if ($('#bagSelPop').hidden) return;
    const q = $('#bagSelQ').value.trim().toLowerCase(), hz = new Map(store.topics.map(t => [t.name, t.hz]));
    const names = bagNames().filter(n => !q || n.toLowerCase().includes(q));
    // 1 Hz 刷新时只在 topic 列表 / 勾选 / 筛选变了才重画（频率每秒都在变，重画会打断点击）
    const key = q + '|' + names.join(',') + '|' + [...bagOff].join(',');
    if (!force && key === bagListRender.key) return; bagListRender.key = key;
    setHTML('#bagSelList', names.length ? names.map(n => { const on = !bagOff.has(n), h = hz.get(n);
      return `<label class="tsel-it ${on ? '' : 'off'}" role="option" aria-selected="${on}"><input type="checkbox" data-bt="${esc(n)}" ${on ? 'checked' : ''}><span class="n" title="${esc(n)}">${esc(n)}</span><span class="hz">${h > 0 ? (h >= 10 ? h.toFixed(0) : h.toFixed(1)) + ' Hz' : ''}</span></label>`; }).join('')
      : `<div class="tsel-empty">${store.topics.length ? '没有匹配的 topic' : '还没拿到 topic 列表（后端连上后自动出现）'}</div>`);
    const all = $('#bagSelAll'), vis = names.filter(n => !bagOff.has(n)).length;
    all.checked = names.length > 0 && vis === names.length; all.indeterminate = vis > 0 && vis < names.length;
  };
  const bagOpen = open => {
    $('#bagSelPop').hidden = !open; $('#bagSelBtn').setAttribute('aria-expanded', open);
    if (open) bagListRender(true);
  };
  $('#bagSelBtn').onclick = () => bagOpen($('#bagSelPop').hidden);
  $('#bagSelList').onchange = e => { const n = e.target.dataset.bt; if (n == null) return;
    e.target.checked ? bagOff.delete(n) : bagOff.add(n); bagSync(); bagListRender(); };
  $('#bagSelAll').onchange = e => {   // 只作用于当前筛出来的那些
    const q = $('#bagSelQ').value.trim().toLowerCase();
    bagNames().filter(n => !q || n.toLowerCase().includes(q)).forEach(n => e.target.checked ? bagOff.delete(n) : bagOff.add(n));
    bagSync(); bagListRender(); };
  $('#bagSelQ').oninput = () => bagListRender();
  document.addEventListener('pointerdown', e => { if (!$('#bagSelPop').hidden && !e.target.closest('#bagSel')) bagOpen(false); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#bagSelPop').hidden) bagOpen(false); });
  bagSync();
  $('#bagBtn').onclick = () => actions.toggle('bag');
  $('#rbOn').onchange = e => actions.setRollback(e.target.checked);
  $('#taughtNavOn').onchange = e => { const on = e.target.checked; Promise.resolve(actions.setTaughtNav(on)).then(r => { if (r?.ok === false) e.target.checked = !on; renderTaughtNav(); }); };
  $('#rbSave').onclick = () => actions.saveRollback();
  $('#bagList').onclick = e => {
    const b = e.target.closest('[data-del],[data-up]'); if (!b || b.disabled) return;
    const bag = store.bags[+(b.dataset.del ?? b.dataset.up)]; if (!bag) return;
    b.dataset.del != null ? actions.deleteBag(bag) : actions.uploadBag(bag);
  };

  /* ---------- 曲线 ---------- */
  const hist = { v: [], vo: [], vw: [], w: [], wo: [], ww: [], roll: [], pitch: [], match: [] };
  function pushHist(){
    const p = (a, v) => { a.push(v); if (a.length > 300) a.shift(); }, R = store.robot, c = store.cmd, sc = store.speedCheck;
    p(hist.v, R.valid ? c.v : null); p(hist.vo, R.valid ? R.v : null); p(hist.w, R.valid ? c.w : null); p(hist.wo, R.valid ? R.w : null);
    p(hist.vw, sc ? sc.wall[0] : null); p(hist.ww, sc ? sc.wall[2] : null);
    p(hist.roll, store.imu.roll); p(hist.pitch, store.imu.pitch); p(hist.match, store.loc.running ? store.loc.match : null);
  }
  function chart(canvas, series, opt){
    const r = canvas.getBoundingClientRect(); if (!r.width) return; const d = window.devicePixelRatio||1;
    if (canvas.width !== Math.round(r.width*d) || canvas.height !== Math.round(r.height*d)){ canvas.width = Math.round(r.width*d); canvas.height = Math.round(r.height*d); }
    const c = canvas.getContext('2d'); c.setTransform(d,0,0,d,0,0); c.clearRect(0,0,r.width,r.height);
    const pl = opt.pad ? 44 : 2, pr = opt.pad ? 14 : 2, pb = opt.pad ? 22 : 4;
    // 图例先排版：放不下就换行，图的上边距跟着图例行数走（多图并排时每张图很窄）
    let pt = opt.pad ? 0 : 4; const legend = [];
    if (opt.pad){
      c.font = '10px "JetBrains Mono",monospace';
      let lx = pl, ly = 8;
      for (const s of series){ const iw = 16 + c.measureText(s.name).width;
        if (lx > pl && lx + iw > r.width - pr){ lx = pl; ly += 14; }
        legend.push([s, lx, ly]); lx += iw + 18; }
      pt = ly + 14;
    }
    const w = r.width-pl-pr, h = r.height-pt-pb;
    if (h < 10) return;
    const [lo, hi] = opt.range, Y = v => pt + h - (v-lo)/(hi-lo)*h, n = 300, X = i => pl + i/(n-1)*w;
    if (opt.pad){ c.fillStyle = '#5f718a'; c.strokeStyle = 'rgba(140,170,200,.08)'; c.textAlign = 'right'; c.textBaseline = 'middle';
      for (const tk of opt.ticks){ c.beginPath(); c.moveTo(pl, Y(tk)+.5); c.lineTo(pl+w, Y(tk)+.5); c.stroke(); c.fillText(tk.toFixed(opt.dec??1), pl-6, Y(tk)); }
      c.textAlign = 'center'; c.textBaseline = 'top'; for (const s of [-30,-20,-10,0]) c.fillText(s ? `${s} s` : '现在', pl + (1+s/30)*w, pt+h+6);
      c.textAlign = 'left'; c.textBaseline = 'middle';
      for (const [s, lx, ly] of legend){ c.fillStyle = s.c; c.fillRect(lx, ly-1.5, 12, 3); c.fillStyle = '#9db0c7'; c.fillText(s.name, lx+16, ly); } }
    for (const s of series){ const a = s.d, off = n - a.length; c.beginPath(); let started = false, first = -1, lastI = -1;
      a.forEach((v, i) => { if (v==null){ started = false; return; } const x = X(i+off), y = Y(clamp(v, lo, hi)); started ? c.lineTo(x, y) : c.moveTo(x, y); started = true; if (first<0) first = i; lastI = i; });
      c.strokeStyle = s.c; c.lineWidth = s.w || 1.6; c.setLineDash(s.dash || []); c.stroke(); c.setLineDash([]);
      if (s.fill && first>=0 && lastI===a.length-1){ c.lineTo(X(n-1), pt+h); c.lineTo(X(first+off), pt+h); c.closePath(); const g = c.createLinearGradient(0, pt, 0, pt+h); g.addColorStop(0, s.fill); g.addColorStop(1, 'rgba(0,0,0,0)'); c.fillStyle = g; c.fill(); }
      const lv = a[a.length-1]; if (lv!=null){ c.fillStyle = s.c; c.beginPath(); c.arc(X(n-1), Y(clamp(lv,lo,hi)), 2.8, 0, 7); c.fill(); } }
  }
  // 曲线可多选：选中的按固定顺序并排显示；面板够高时 3 张以上排成两行
  const PLOTS = ['v', 'w', 'tilt', 'loc'];
  const PLOT_NAME = { v:'线速度 vx (m/s)', w:'角速度 wz (rad/s)', tilt:'姿态 roll / pitch (°)', loc:'定位匹配率 (%)' };
  let plotSel = ['v'];
  try { const s = JSON.parse(localStorage.getItem('go2w.plots')); if (Array.isArray(s)) plotSel = s.filter(m => PLOTS.includes(m)); } catch {}
  const plotCanvas = {};
  function layoutPlots(){
    $$('#plotSide [data-m]').forEach(b => b.setAttribute('aria-pressed', String(plotSel.includes(b.dataset.m))));
    const g = $('#plotGrid'), sel = PLOTS.filter(m => plotSel.includes(m));
    g.innerHTML = ''; g.className = sel.length ? 'plot-grid' : 'plot-grid plot-empty';
    if (!sel.length){ g.textContent = '在左边勾选要看的曲线，可以同时选多条'; return; }
    const rows = sel.length >= 3 && g.clientHeight >= 300 ? 2 : 1, cols = Math.ceil(sel.length / rows);
    g.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
    g.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
    for (const m of sel){
      const cell = document.createElement('div'); cell.className = 'plot-cell';
      cell.innerHTML = `<h5>${PLOT_NAME[m]}</h5><div class="plot-body"><canvas></canvas></div>`;
      g.appendChild(cell); plotCanvas[m] = cell.querySelector('canvas');
    }
    for (const m of PLOTS) if (!sel.includes(m)) delete plotCanvas[m];
  }
  $('#plotSide').onclick = e => { const b = e.target.closest('[data-m]'); if (!b) return; const m = b.dataset.m;
    plotSel = plotSel.includes(m) ? plotSel.filter(x => x!==m) : [...plotSel, m];
    try { localStorage.setItem('go2w.plots', JSON.stringify(plotSel)); } catch {}
    layoutPlots(); renderCharts(); };
  // 面板高度变化时重新判断一行还是两行
  let lastRows = null;
  new ResizeObserver(() => { const rows = plotSel.length >= 3 && $('#plotGrid').clientHeight >= 300 ? 2 : 1; if (rows !== lastRows){ lastRows = rows; layoutPlots(); } }).observe($('#plotGrid'));
  layoutPlots();
  function renderCharts(){
    chart($('#spark'), [{ d: hist.match, c: ACC, fill: 'rgba(56,225,220,.22)' }], { range: [30, 100] });
    if ($('#pPlots').hidden || $('#drawer').classList.contains('collapsed') || document.body.classList.contains('demo')) return;
    // ROS 模式多画一条「按真实时间算的速度」，和 odom.twist 对照；图例里带上时间戳走速比
    const sc = store.speedCheck, wallName = `按真实时间${sc ? `（时间戳 ×${sc.rate.toFixed(2)}）` : ''}`;
    const vSeries = [{ d:hist.v, c:'#9db0c7', name:'cmd_vel', dash:[4,3] }, { d:hist.vo, c:ACC, name:'odom.twist', fill:'rgba(56,225,220,.12)' }];
    const wSeries = [{ d:hist.w, c:'#9db0c7', name:'cmd_vel', dash:[4,3] }, { d:hist.wo, c:TRAJ, name:'odom.twist' }];
    if (ros){ vSeries.push({ d:hist.vw, c:'#ffe08a', name:wallName }); wSeries.push({ d:hist.ww, c:'#ffe08a', name:wallName }); }
    const cfg = {
      v: [vSeries, { range:[-.1,1.2], ticks:[0,.4,.8,1.2], dec:1 }],
      w: [wSeries, { range:[-1.5,1.5], ticks:[-1,0,1], dec:0 }],
      tilt: [[{ d:hist.roll, c:'#ff6b6b', name:'roll' }, { d:hist.pitch, c:'#5be08d', name:'pitch' }], { range:[-3,3], ticks:[-2,0,2], dec:0 }],
      loc: [[{ d:hist.match, c:ACC, name:'scan / map 匹配率', fill:'rgba(56,225,220,.12)' }], { range:[30,100], ticks:[40,60,80,100], dec:0 }],
    };
    for (const [m, cv] of Object.entries(plotCanvas)) chart(cv, cfg[m][0], { ...cfg[m][1], pad:true });
  }

  // 底部面板：拖上边缘调高度，双击恢复默认；高度记在本机
  const drawer = $('#drawer'), DH_DEFAULT = 212;
  const setDH = px => { const v = clamp(Math.round(px), 150, Math.round(window.innerHeight*.6)); drawer.style.setProperty('--dh', v+'px'); return v; };
  try { const v = +localStorage.getItem('go2w.drawerH'); if (v) setDH(v); } catch {}
  const grip = $('#drawerGrip');
  grip.addEventListener('pointerdown', e => {
    e.preventDefault(); grip.setPointerCapture(e.pointerId); drawer.classList.add('dragging');
    const y0 = e.clientY, h0 = drawer.getBoundingClientRect().height;
    const mv = ev => setDH(h0 + (y0 - ev.clientY));
    const up = () => { grip.removeEventListener('pointermove', mv); grip.removeEventListener('pointerup', up); drawer.classList.remove('dragging');
      try { localStorage.setItem('go2w.drawerH', String(Math.round(drawer.getBoundingClientRect().height))); } catch {} };
    grip.addEventListener('pointermove', mv); grip.addEventListener('pointerup', up);
  });
  grip.ondblclick = () => { setDH(DH_DEFAULT); try { localStorage.removeItem('go2w.drawerH'); } catch {} };

  /* ---------- 模式 / 抽屉 ---------- */
  const setDemo = demo => { document.body.classList.toggle('demo', demo); $('#mDemo').classList.toggle('on', demo); $('#mDebug').classList.toggle('on', !demo);
    setTimeout(() => { mapView.resize(); mapView.fit(); }, 30); };
  $('#mDebug').onclick = () => setDemo(false);
  $('#mDemo').onclick = () => setDemo(true);
  // 左右栏收起 / 展开；记在本机浏览器里，刷新后保持
  function sidePanel(side, label, key){
    const cls = `${side}-off`, b = $(`#${side}Toggle`), store_ = `go2w.${side}Off`;
    const set = off => {
      document.body.classList.toggle(cls, off);
      b.setAttribute('aria-expanded', String(!off));
      b.title = `${off ? '展开' : '收起'}${label} ( ${key} )`; b.setAttribute('aria-label', `${off ? '展开' : '收起'}${label}`);
      try { localStorage.setItem(store_, off ? '1' : '0'); } catch {}
    };
    const toggle = () => set(!document.body.classList.contains(cls));
    b.onclick = toggle;
    try { if (localStorage.getItem(store_) === '1') set(true); } catch {}
    return toggle;
  }
  const toggleLeft = sidePanel('left', '左栏', '['), toggleRight = sidePanel('right', '右栏', ']');
  $$('.dtab').forEach(b => b.onclick = () => { $$('.dtab').forEach(x => x.classList.toggle('on', x===b)); $$('.dpane').forEach(p => p.hidden = p.id!==b.dataset.pane);
    $('#logFilters').style.visibility = b.dataset.pane==='pLogs' ? 'visible' : 'hidden'; $('#drawer').classList.remove('collapsed'); });
  $('#drawerToggle').onclick = () => $('#drawer').classList.toggle('collapsed');

  // 机器人照片：依次试 img/go2w.jpg / .png / .webp，都没有就保留图标
  (function loadPhoto(exts){
    if (!exts.length) return;
    const probe = new Image();
    probe.onload = () => {
      $$('.robot-img').forEach(el => { el.src = probe.src; el.hidden = false; });
      $$('.robot-fallback').forEach(el => el.style.display = 'none');
      document.body.classList.add('has-photo');
    };
    probe.onerror = () => loadPhoto(exts.slice(1));
    probe.src = `img/go2w.${exts[0]}`;
  })(['jpg', 'png', 'webp']);

  const badge = $('#srcBadge'); badge.dataset.s = src.name; badge.textContent = ros ? '实时 · ROS 2' : '模拟数据';

  // 版本号（11-18）：页面自己的 static/version.json；后端推来的 webVersion 是机器人上装的版本，
  // 不一样 = 机器人升级了但这个页面没刷新（或者页面来自别的服务器）
  const verBtn = $('#verBadge'); let verInfo = null;
  fetch('version.json', { cache: 'no-store' }).then(r => r.ok ? r.json() : null).then(v => {
    if (!v?.version) return;
    verInfo = v; verBtn.hidden = false; renderVersion();
  }).catch(() => {});
  function verStale(){ return !!(verInfo && ros && store.webVersion && store.webVersion !== verInfo.version); }
  function renderVersion(){
    if (!verInfo) return;
    const stale = verStale();
    const txt = stale ? `${verInfo.version} · 有新版本，请刷新` : verInfo.version;
    if (verBtn.textContent !== txt) verBtn.textContent = txt;
    const st = stale ? 'warn' : '';
    if (verBtn.dataset.s !== st) verBtn.dataset.s = st;
  }
  verBtn.onclick = () => {
    if (!verInfo) return;
    const s = document.createElement('div'); s.className = 'scrim';
    s.innerHTML = `<div class="modal ver-modal" role="dialog" aria-modal="true"><h3></h3><div class="ver-warn" hidden></div><div class="ver-log"></div>
      <div class="btn-row"><button class="btn ghost" data-a="r" hidden>刷新页面</button><button class="btn pri" data-a="c">关闭</button></div></div>`;
    s.querySelector('h3').textContent = `网页版本 ${verInfo.version}` + (verInfo.date ? `（${verInfo.date}）` : '');
    if (verStale()){
      const w = s.querySelector('.ver-warn'); w.hidden = false;
      w.textContent = `机器人上已经是 ${store.webVersion}，这个页面还是 ${verInfo.version}。刷新一下就是新版本。`;
      s.querySelector('[data-a=r]').hidden = false;
    }
    const log = s.querySelector('.ver-log');
    for (const n of verInfo.notes || []){
      const sec = document.createElement('section');
      const h = document.createElement('h4'); h.textContent = n.version;
      if (n.date){ const d = document.createElement('small'); d.textContent = n.date; h.appendChild(d); }
      const ul = document.createElement('ul');
      for (const it of n.items || []){ const li = document.createElement('li'); li.textContent = it; ul.appendChild(li); }
      sec.append(h, ul); log.appendChild(sec);
    }
    const close = () => { s.remove(); document.removeEventListener('keydown', esc); };
    const esc = e => { if (e.key === 'Escape') close(); };
    s.addEventListener('click', e => { const a = e.target.dataset.a; if (e.target === s || a === 'c') close(); if (a === 'r') location.reload(); });
    document.addEventListener('keydown', esc);
    document.body.appendChild(s); s.querySelector('[data-a=c]').focus();
  };
  renderLayers();

  return {
    fast(){ renderTask(); renderRoutes(); renderHud(); renderArm(); syncSliders(); }, // 5 Hz
    medium(){ renderVersion(); renderChips(); renderCharts(); renderProfiles(); renderAlignment(); renderCont(); renderTaughtLayer(); },  // 4 Hz
    slow(){ renderTopics(); renderMaps(); renderTaughtNav(); $('#clock').textContent = new Date().toTimeString().slice(0,8); }, // 1 Hz
    pushHist,                                                      // 10 Hz
    toggleLeft, toggleRight,
  };
}
