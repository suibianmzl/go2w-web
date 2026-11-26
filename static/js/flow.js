// 右栏「任务」：导航 / 重定位 / 建图 / 示教采集。导航原型见 web/prototype/right_panel.html。
// 导航（11-12 改）：1 地图 → 2 导航任务（路线 + 运行参数 + 出发按钮）。选图、编辑任务都不用开导航（后端直接读地图文件）；
// 点「出发」后自动：站立 → 启动导航（或切图）→ 自动定位 → 解锁并下发任务。不再用 Nav2 预先检查站间路径。
// 建图 / 重定位标签照旧：点标签就切换机器人档位，切换时先停车（取消任务、底盘上锁）。
import { $, $$, deg, mapTitle, mapOptText } from './util.js';
import { toast, formBox, stillCheck } from './ui.js';

export function setupFlow(store, src, actions, setTool, reloc, taught, mapView){
  const prof = id => store.profiles[id];
  const up = id => ['run', 'starting'].includes(prof(id));       // 在跑或正在起来
  const run = id => prof(id) === 'run';
  const taughtMissing = () => store.taughtAvailable === true && store.taughtNavigation === false;
  const exec = p => Promise.resolve(p).then(r => { if (r && r.ok === false) toast(r.msg || '操作失败', 'err'); else if (r?.msg) toast(r.msg); return r; });
  store.uiTask = null;          // 用户选的标签（同一个导航栈上可以是「导航」或「重定位」）
  store.initPose = null;        // 这一轮导航里给过初始位置（或确认过当前位置正确）
  const openSteps = new Set();
  // 地图来自 map_server（可以定位、规划）；模拟模式没有这个区分
  const liveMap = () => store.source !== 'ros' || !!store.mapLive;

  // 当前标签：跟着机器人实际在跑的档位走；什么都没开时默认「导航」（离线选图、编辑任务）
  function tab(){
    if (up('mapping')) return 'map';
    if (store.uiTask === 'taught') return 'taught';
    if (up('localization')) return store.uiTask === 'nav' ? 'nav' : 'reloc';
    if (up('navigation')) return store.uiTask === 'reloc' ? 'reloc' : 'nav';
    return store.uiTask || 'nav';
  }
  const routeActive = () => store.task.type !== 'idle' && store.task.status === 'active';
  const mapNames = () => store.maps.map(m => m.name);
  const leavingMapWarn = () => {
    if (!up('mapping')) return '';
    const saved = store.mapSavedAt;
    return saved ? `建图上次保存是 ${Math.max(1, Math.round((Date.now() - saved) / 60000))} 分钟前，之后走过的部分会丢失。`
      : '这个页面上还没有保存过当前地图，停止建图后地图会丢失。';
  };
  const busyMsg = () => store.mapSaving || store.mapBusy ? '地图正在保存，请稍等' : store.busy ? '上一个启停还没完成，稍等几秒' : '';
  // 停车：取消任务 + 上锁（导航栈不动）
  async function park(why){
    if (routeActive() || store.task.type !== 'idle') await exec(src.cmd.cancel(why));
    if (store.armed) await exec(src.cmd.arm(false, why));
  }

  function startStack(mode, title, body, uiTask){
    const b = busyMsg(); if (b) return toast(b, 'warn');
    formBox({ title, body, warn: leavingMapWarn(), ok: title.replace('…', ''), danger: !!up('mapping') && !store.mapSavedAt,
      build: f => stillCheck(f),
      valid: f => f.querySelector('[data-still]').checked,
      onOk: () => {
        if (mode === 'mapping'){ store.mapSavedAt = null; store.mapSavedName = null; }
        store.uiTask = uiTask; store.initPose = null; openSteps.clear(); resetPrep();
        exec(src.cmd.setMode(mode, mode === 'mapping' ? null : store.mapCurrent));
      } });
  }
  // 标签对应的档位已经在跑（或正在起来）
  const live = t => t === 'taught' ? store.uiTask === 'taught' : t === 'nav' ? up('navigation') : t === 'reloc' ? up('navigation') || up('localization') : up('mapping');
  function switchTo(t, guards = {}){
    if (t === tab() && live(t)) return;
    // 先完成当前地图编辑器的离开确认，避免切换任务/档位让 tick 直接丢掉未保存的格子修改。
    if (!guards.edit && !mapView.leaveEdit(() => switchTo(t,{...guards,edit:true}))) return;
    // 采集归属于明确 session；切换任务必须等暂停确认返回，不能只隐藏面板后继续后台采样。
    if (!guards.taught && store.uiTask === 'taught' && t !== 'taught' && !taught.leave(() => switchTo(t,{...guards,taught:true}))) return;
    if (tab() === 'reloc' && t !== 'reloc' && reloc.changed()) return reloc.leave(() => switchTo(t));
    const b = busyMsg(); if (b) return toast(b, 'warn');
    if (t === 'taught'){
      const go = async () => {
        if (up('mapping')){ const r = await exec(src.cmd.setMode(null)); if (r?.ok === false) return; }
        await park('切到示教'); setTool('pan'); resetPrep(); store.uiTask = 'taught'; taught.enter(); render();
      };
      if (up('mapping')) return formBox({title:'切换到示教采集',body:'将停止建图，使用已保存的地图记录参考轨迹。',warn:leavingMapWarn(),ok:'停止建图并切换',danger:!store.mapSavedAt,onOk:go});
      if (routeActive()) return formBox({title:'切换到示教采集',body:'当前导航任务会取消，底盘会上锁。选择地图并完成定位自检后，再明确开始采集。',ok:'取消任务并切换',danger:true,onOk:go});
      return go();
    }
    if (t === 'nav'){
      // 导航不在这里启动：点「出发」时再启动
      if (up('mapping')){
        const saved = store.mapSavedName;
        return formBox({ title: '停止建图', body: `切到导航会停止建图${saved ? `，之后用刚保存的 ${saved}` : ''}。`, warn: leavingMapWarn(),
          ok: '停止建图', danger: !store.mapSavedAt,
          onOk: async () => {
            store.uiTask = 'nav'; resetPrep(); render();
            await exec(src.cmd.setMode(null));
            if (saved) exec(src.cmd.loadMap(saved));   // 导航没开：只是选好显示 / 下次启动用的地图
          } });
      }
      store.uiTask = 'nav';
      return render();
    }
    if (t === 'reloc'){
      if (run('navigation')){
        const go = async () => { store.uiTask = 'reloc'; resetPrep(); render(); await park('切到重定位'); toast('导航栈保持运行，底盘已上锁', 'warn'); };
        if (routeActive()) return formBox({ title: '切换到重定位', body: '当前多站任务会被取消，底盘会上锁。导航栈保持运行，改完位置可以直接回到导航。', ok: '取消任务并切换', danger: true, onOk: go });
        return go();
      }
      return startStack('localization', '启动定位', `${up('mapping') ? '将停止建图，' : ''}用地图 ${store.mapCurrent || '（默认）'} 启动 FAST-LIO、map_server 和 AMCL（不启动 Nav2），然后修正位置。`, 'reloc');
    }
    if (t === 'map'){
      const from = up('navigation') ? '将停止导航（正在执行的任务会取消），' : up('localization') ? '将停止定位，' : '';
      return startStack('mapping', '开始建图', `${from}启动 FAST-LIO 和 slam_toolbox。地图从机器人当前位置开始，x 轴取机器人此刻的朝向。`, 'map');
    }
  }
  $$('#taskTabs button').forEach(b => b.onclick = () => switchTo(b.dataset.t));

  // 第 1 步：地图。导航没开时选了就换（后端直接读文件显示）；导航开着时 map_server 换图，要确认
  const mapSel = $('#navMapSel');
  const pickMap = () => { const v = mapSel.value; if (v && v !== store.mapCurrent) actions.loadMap(v); };
  $('#navMapLoad').onclick = pickMap;
  $('#navMapMgr').onclick = () => actions.manageMaps();
  $('#navMapRename').onclick = () => actions.renameMap(mapSel.value);
  mapSel.onchange = () => { if (!up('navigation') && !up('localization')) pickMap(); };
  // 定位（出发流程里定位失败时手动补救）
  $('#navInitBtn').onclick = () => setTool('init');
  $('#navInitOk').onclick = () => { store.initPose = { manual: true, map: store.mapCurrent }; render(); };
  // 地图工具栏发出的初始位姿成功后算定位完成（actions.initialPose 会调这个）
  store.onInitialPose = () => { store.initPose = { map: store.mapCurrent }; render(); };
  $$('#navFlow [data-open]').forEach(b => b.onclick = () => { const k = b.dataset.open; openSteps.has(k) ? openSteps.delete(k) : openSteps.add(k); render(); });
  // 重定位 / 建图
  $('#relocInit').onclick = () => setTool('init');
  // 自动定位：按钮手动触发；导航 / 定位启动后后端也会自动跑一次。成功（state.autoLoc.seq 变了且 ok）就算定位完成
  $('#navAutoBtn').onclick = $('#relocAuto').onclick = () => actions.autoLocalize(prepHints());
  // 11-26 指定大概位置：在地图上点一下 / 拖出一个圈，只在圈里找（大图上整图搜要 10 s 以上）
  $('#navAreaBtn').onclick = $('#relocArea').onclick = () => setTool('area');
  // 自动定位失败后：机器狗小范围挪一次再定位（要确认）
  $('#navMoveBtn').onclick = $('#relocMove').onclick = () => actions.autolocMove();
  let autoSeq = null;
  function autoLocRender(){
    const a = store.autoLoc;
    // 刚打开网页时（autoSeq 还是 null）后端已经定位成功的也算：后端每次启动档位都会把状态复位成 idle
    if (a && a.seq !== autoSeq && a.state === 'ok' && a.map === store.mapCurrent && run('navigation')){
      store.initPose = { auto: true, map: store.mapCurrent };
    }
    if (a) autoSeq = a.seq;
    const busy = a?.state === 'run' || a?.state === 'move';
    const text = a?.state === 'move' ? a.msg || '移动后重试…' : busy ? '自动定位中…（机器人保持静止）'
      : a && a.map === store.mapCurrent && ['ok', 'fail'].includes(a.state) ? a.msg : '';
    for (const id of ['navAutoNote', 'relocAutoNote']){
      const el = $('#' + id);
      el.hidden = !text; el.textContent = text;
      el.classList.toggle('warn', busy || a?.state === 'fail');
    }
    $('#navAutoBtn').disabled = $('#relocAuto').disabled = busy;
    $('#navAutoBtn').textContent = $('#relocAuto').textContent = busy ? (a.state === 'move' ? '移动中…' : '定位中…') : '自动定位';
    // 「移动后重试」：这张地图上刚失败、还有次数、定位在跑
    const canMove = a?.state === 'fail' && a.map === store.mapCurrent && (a.movesLeft ?? 0) > 0 && (run('navigation') || run('localization'));
    $('#navMoveBtn').hidden = $('#relocMove').hidden = !canMove;
    $('#navMoveBtn').textContent = $('#relocMove').textContent = `移动后重试（还可 ${a?.movesLeft ?? 0} 次）`;
  }

  // ---------- 出发流程 ----------
  // 点「出发」后：站立（趴着就站起来、启动策略）→ 导航（没开就用任务的地图启动；开着但图不对就切图）→ 自动定位 → 解锁并下发任务。
  // 已经满足的步骤直接跳过。每一步都从后端推来的状态判断；失败就停在那一步，给出原因（定位失败可以手动补救，补好后自动继续）。
  // prep.step：stand / nav / loc / go；store.prep 给地图右上角的状态牌看
  const prep = { on: false };
  const STEP_NAME = { stand: '站立', nav: '启动导航', loc: '定位', go: '下发任务' };
  const MATCH_WAIT = 4000;   // ms：出发前等新匹配率的上限
  function resetPrep(){ for (const k of Object.keys(prep)) delete prep[k]; prep.on = false; }
  store.prep = () => prep.on ? { on: true, step: prep.step, name: STEP_NAME[prep.step] || '', fail: prep.fail, route: prep.routeName } : null;
  // 出发流程进行中每 2 s 告诉后端，别把这段时间当空闲自动趴下
  function prepHold(now){
    if (!prep.on || prep.fail || now - (prep.holdT || 0) < 2000) return;
    prep.holdT = now;
    if (src.can('prepHold')) src.cmd.prepHold();
  }
  function prepFail(k, msg, soft = false){ prep.fail = { k, msg, soft }; }
  // 出发流程里自动定位：先在任务第 1 站 / 断点站附近找（后端还会加上次位置、停车点），找不到后端再搜整张图
  function prepHints(){
    if (!prep.on) return {};
    const route = store.routes.find(r => r.id === prep.route), pts = route?.points || [];
    const out = [], add = (i, label) => { const p = pts[i]; if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) out.push({ x: p.x, y: p.y, label }); };
    if (prep.resume && route?.resume){ const k = (prep.resume === 'skip' ? route.resume.skipPoint : route.resume.point) - 1; add(k, `第 ${k + 1} 站`); }
    add(0, '第 1 站');
    return out.length ? { hints: out } : {};
  }

  // routes.js 的「出发」按钮调这个（参数没保存的已经先保存好了）。
  // resume（11-24 任务接续）：'continue' / 'skip'，流程一样（站立 → 导航 → 定位），最后下发时带上，后端从断点接着跑
  function startGo(route, resume = null){
    if (!route) return toast('先选择（或保存）一个任务', 'warn');
    const b = actions.routeGoBlocker(route) || busyMsg(); if (b) return toast(b, 'warn');
    if (resume && !route.resume) return toast('这个任务没有可以接续的断点', 'warn');
    if (prep.on && !prep.fail) return;
    const begin = () => {
      resetPrep();
      Object.assign(prep, { on: true, fail: null, step: 'stand', route: route.id, routeName: route.name, map: route.map, t0: Date.now(), resume,
        seq0: store.autoLoc?.seq ?? -1, standT: 0, standOk: false, holdT: 0, navT: 0, runT: 0, loadT: 0, autoT: 0, matchT: 0, goT: 0 });
      render();
    };
    if (up('navigation')) return begin();
    // 要启动导航：FAST-LIO 初始化时机器人要站好不动
    formBox({ title: resume ? '继续任务' : '出发', ok: resume ? '继续' : '出发',
      body: `${resume ? `从第 ${resume === 'skip' ? route.resume.skipPoint : route.resume.point} 站接着跑剩下的站。` : ''}将${store.ctrl?.on && store.ctrl.state !== 'Velocity' ? '让机器狗站起来，' : ''}用地图 ${route.map} 启动导航（约 10–20 秒），自动定位后解锁底盘出发。${up('localization') ? '当前的「定位」档位会先停掉。' : ''}`,
      build: f => stillCheck(f), valid: f => f.querySelector('[data-still]').checked, onOk: begin });
  }
  store.startGo = startGo;
  $('#prepStop').onclick = () => { resetPrep(); render(); toast('已取消出发', 'warn'); };
  $('#prepRetry').onclick = () => { const r = store.routes.find(x => x.id === prep.route), how = prep.resume; resetPrep(); startGo(r, how); };

  function prepTick(){
    if (!prep.on) return;
    if (prep.fail){
      if (prep.fail.k === 'loc' && store.initPose && run('navigation')) prep.fail = null;   // 手动补救了定位，接着往下走
      else return;
    }
    const route = store.routes.find(r => r.id === prep.route);
    if (!route) return prepFail('go', '任务被删除了');
    if (prep.resume && !route.resume && !prep.goT) return prepFail('go', '断点已经作废（任务站点改过或超过 24 小时）');
    if (store.estop) return prepFail(prep.step, '急停中');
    const now = Date.now();
    prepHold(now);
    // 1 站立：导航启动（FAST-LIO 初始化）和定位都要在站好之后做
    prep.step = 'stand';
    const C = store.ctrl;
    if (C?.on){
      if (!(C.state === 'Velocity' && C.ready)){
        if (prep.standOk) return prepFail('stand', C.state ? `机器狗不在行走状态（${C.state}）` : '收不到运控状态');
        if (C.state == null) return prepFail('stand', '收不到运控状态：请检查运行容器的 control 组和域 0 通信');
        if (!prep.standT){
          prep.standT = now;
          const t0 = prep.t0;
          actions.standUp().then(r => {
            if (prep.on && prep.t0 === t0 && r?.ok === false){ prepFail('stand', r.msg || '站起失败'); render(); }
          });
        }
        return;
      }
      prep.standOk = true;
    }
    // 2 导航
    prep.step = 'nav';
    if (!run('navigation') || taughtMissing()){
      if (prep.runT) return prepFail('nav', taughtMissing() ? '示教导航节点离线了' : '导航停了');
      if (store.busy || prof('navigation') === 'starting' || prof('navigation') === 'stopping') return;
      // 只有用户点普通任务「出发」才会到这里。旧导航未启用示教时也请求一次原档位启用流程；
      // navT 在整次出发中只写一次，等待期间不重复启停。计划内停/启阶段不能提前写 runT。
      if (!prep.navT){ prep.navT = now; store.initPose = null; exec(src.cmd.setMode('navigation', prep.map)); return; }
      if (now - prep.navT > 8000) prepFail('nav', taughtMissing() ? '示教导航节点没有就绪：看「日志」里的报错' : '导航没有启动起来：看「日志」里的报错');
      return;
    }
    if (!prep.runT) prep.runT = now;
    if (!store.mapCurrent || !liveMap()) return;                // 刚起来：等 map_server 的地图名
    if (store.mapCurrent !== prep.map){
      if (!prep.loadT){ prep.loadT = now; exec(src.cmd.loadMap(prep.map)); return; }
      if (now - prep.loadT > 15000) prepFail('nav', `切换到地图 ${prep.map} 没有成功`);
      return;
    }
    // 3 定位
    prep.step = 'loc';
    if (!store.initPose){
      if (store.source !== 'ros'){ store.initPose = { auto: true, map: store.mapCurrent }; return; }   // 模拟：定位没有误差
      const a = store.autoLoc;
      if (a?.state === 'run') return;
      if (a && a.seq > prep.seq0 && a.map === prep.map && a.state === 'fail') return prepFail('loc', a.msg || '自动定位失败', true);
      // 这次启动的导航：后端起来后会自动定位一次，等它（最多 40 s）；导航本来就开着就自己请求
      if (prep.navT && !prep.autoT && (!a || a.state === 'idle') && now - prep.runT < 40000) return;
      if (!prep.autoT){
        prep.autoT = now;
        const t0 = prep.t0, seq = a?.seq ?? -1;
        // 后端没开始就拒绝了（不会更新 autoLoc.seq）：直接显示原因，别干等
        actions.autoLocalize(prepHints()).then(r => {
          if (prep.on && prep.t0 === t0 && r?.ok === false && (store.autoLoc?.seq ?? -1) <= seq && !store.initPose){
            prepFail('loc', r.msg || '自动定位失败', true); render();
          }
        });
        return;
      }
      if (now - prep.autoT > 110000) prepFail('loc', '自动定位没有结果', true);
      return;
    }
    // 4 解锁并下发任务（定位可能不准时 actions.startRoute 会再问一句）
    prep.step = 'go';
    if (prep.goT) return;
    if (!store.odomOk || !store.robot.valid || store.fixed !== 'map') return;   // 等里程计和 map 坐标系
    // 刚定好位时后端的匹配率要重新算（约 1 s 才有新值）：等到 ≥ 60% 再出发，最多等 MATCH_WAIT，
    // 不然 startRoute 会拿定位前的旧值（比如 7%）误报「定位可能不准」。
    const mNow = store.loc.match;
    if (!prep.matchT) prep.matchT = now;
    if (!(mNow != null && mNow >= 60) && now - prep.matchT < MATCH_WAIT) return;
    prep.goT = now;
    const t0 = prep.t0, o = route.opts || {};
    Promise.resolve(actions.startRoute(route, { roundTrip: !!o.loop, loops: o.loops ?? 1, vmax: o.vmax, wmax: o.wmax, resume: prep.resume })).then(r => {
      if (!prep.on || prep.t0 !== t0) return;
      if (r?.ok) resetPrep();                                   // 出发了：进度看「导航任务」卡片
      else prepFail('go', r == null ? '已取消出发' : r.msg || '任务没有下发成功', true);
      render();
    });
  }

  function prepRender(){
    const box = $('#prepList'), F = prep.fail, R = store.robot, m = store.loc.match;
    box.hidden = !prep.on;
    $('#prepBtns').hidden = !prep.on;
    $('#prepRetry').hidden = !F;
    $('#prepStop').textContent = F ? '关闭' : '取消出发';
    const C = store.ctrl, order = ['stand', 'nav', 'loc', 'go'], cur = order.indexOf(prep.step);
    const st = k => { const i = order.indexOf(k); return F?.k === k ? 'fail' : i < cur ? 'ok' : i === cur && prep.on ? 'run' : 'todo'; };
    const navHere = run('navigation') && !taughtMissing() && liveMap() && store.mapCurrent === prep.map;
    const text = {
      stand: !C?.on ? (store.source === 'ros' ? '运控状态未接入（GO2W_CTRL=0）：用遥控器站起来' : '模拟：没有运控')
        : C.op === 'up' ? '站起中…（站稳约 2 s，再启动策略）'
        : C.state === 'Velocity' && C.ready ? '已站起，策略运行中'
        : C.state === 'Passive' ? '趴着，站起来' : C.state === 'FixStand' ? '站着，启动策略' : C.state || '收不到运控状态',
      nav: navHere ? `运行中 · ${mapTitle(store.mapLabels, store.mapCurrent)}` : prep.loadT ? `切换到地图 ${prep.map}…` : run('navigation') && taughtMissing() ? '启用示教导航中…（机器人保持静止）' : run('navigation') ? '等待地图…' : '启动中…（机器人保持静止）',
      loc: store.initPose && navHere ? `(${R.x.toFixed(2)}, ${R.y.toFixed(2)}) · ${Math.round(deg(R.th)) || 0}°${m != null ? ` · 匹配率 ${m.toFixed(0)}%` : ''}`
        : store.autoLoc?.state === 'move' ? store.autoLoc.msg || '移动后重试…'
        : store.autoLoc?.state === 'run' ? '定位中…（机器人保持静止）' : '等待自动定位…',
      go: prep.goT ? '解锁底盘，下发任务…' : prep.matchT ? '等定位匹配率更新…' : '定位好后自动出发',
    };
    for (const li of $$('#prepList li')){
      const k = li.dataset.k;
      // 定位失败后正在移动重试 / 重新定位：显示进度，不显示上一次的失败原因
      const retrying = k === 'loc' && ['run', 'move'].includes(store.autoLoc?.state);
      const s = st(k), t = F?.k === k && !retrying ? F.msg : s === 'todo' ? '' : text[k];
      if (li.dataset.s !== s) li.dataset.s = s;
      const sm = li.querySelector('small'); if (sm.textContent !== t) sm.textContent = t;
    }
    // 定位失败时给手动补救的按钮；补好后流程自动继续
    $('#prepLocRow').hidden = !(prep.on && F?.k === 'loc' && navHere && !store.initPose);
    $('#navInitOk').hidden = !R.valid || store.fixed !== 'map';
  }

  $('#relocBack').onclick = () => switchTo('nav');   // 有没应用的改动时 switchTo 会先问
  $('#mapTele').onclick = () => setTool('teleop');
  $('#mapToNav').onclick = () => switchTo('nav');
  $('#mapStart').onclick = () => switchTo('map');
  $('#mapStop').onclick = () => actions.setMode(null);          // 没保存会先提醒
  $('#mapBag').onchange = e => { e.target.checked = !e.target.checked; actions.toggle('bag'); };   // 勾选框跟着实际状态走
  // 地图名：默认 lab_年月日；保存成功后换成下一个没用过的名字（后端不覆盖同名地图）
  const mapName = $('#mapName'), today = () => { const d = new Date(); return `lab_${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`; };
  const freeName = base => { const used = new Set(mapNames()); if (!used.has(base)) return base; let i = 2; while (used.has(`${base}_${i}`)) i++; return `${base}_${i}`; };
  let nameTouched = false, lastSaved = null;
  mapName.oninput = () => { nameTouched = true; };
  const fmtUp = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

  function setStep(id, state, sub){
    const el = $('#' + id);
    if (el.dataset.s !== state) el.dataset.s = state;
    el.classList.toggle('open', state === 'done' && openSteps.has(id));
    const more = el.querySelector('.step-more');
    if (more){ more.hidden = state !== 'done'; more.textContent = openSteps.has(id) ? '收起' : '更改'; }
    const s = $('#' + id + 'Sub'); if (s.textContent !== sub) s.textContent = sub;
  }
  let lastMaps, lastNav, lastMapCur, lastStood;
  function render(){
    // 导航停了或换了地图：初始位置作废
    const navOn = run('navigation');
    if (!navOn && lastNav) store.initPose = null;
    lastNav = navOn;
    // 从趴着站起来：雷达的高度和姿态变了，定位要重新做（出发流程会自动定位）
    const stood = store.ctrl?.stood;
    if (stood != null && lastStood != null && stood !== lastStood) store.initPose = null;
    if (stood != null) lastStood = stood;
    if (store.initPose && store.initPose.map !== store.mapCurrent) store.initPose = null;
    autoLocRender();
    prepTick();

    const t = tab();
    $$('#taskTabs button').forEach(b => { b.classList.toggle('on', b.dataset.t === t); b.disabled = !!store.busy; });
    const stack = up('mapping') ? '建图' : up('navigation') ? '导航' : up('localization') ? '定位' : '';
    const starting = ['mapping', 'localization', 'navigation'].some(m => prof(m) === 'starting'), stopping = ['mapping', 'localization', 'navigation'].some(m => prof(m) === 'stopping');
    const pill = $('#stackPill');
    const ps = store.estop ? ['crit', '急停'] : starting ? ['warn', '启动中…'] : stopping ? ['warn', '停止中…'] : routeActive() ? ['run', '执行任务'] : stack ? ['ok', stack + '运行中'] : ['', '未运行'];
    if (pill.textContent !== ps[1]){ pill.dataset.s = ps[0]; pill.textContent = ps[1]; }
    $('#stackTxt').textContent = stack ? `${stack}${starting ? '（启动中）' : ''}${store.mapCurrent && stack !== '建图' ? ' · ' + mapTitle(store.mapLabels, store.mapCurrent) : ''}` : '无';
    $('#armTxt').textContent = store.estop ? '急停' : store.armed == null ? '--' : store.armed ? '已解锁' : '已上锁';

    $('#flowNone').hidden = !!t;
    $('#flowNav').hidden = t !== 'nav';
    $('#flowReloc').hidden = t !== 'reloc';
    $('#flowMap').hidden = t !== 'map';
    $('#flowTaught').hidden = t !== 'taught';
    taught.render();
    // 执行中：步骤收起来，只看进度卡片
    const showTask = store.task.type !== 'idle';
    $('#taskCard').hidden = !(showTask && (t === 'nav' || !t));
    $('#navFlow').hidden = showTask;

    if (t === 'nav' && !showTask){
      // 1 地图
      // 11-09 修：原来每次刷新只要下拉框没焦点就改回当前地图，点「加载」时焦点移到按钮，选择被冲掉，点了没反应。
      // 现在只在当前地图变化时同步；列表刷新时保留用户的选择
      // 11-18 修：store.maps 每条 state（2 Hz）都是新数组，原来按引用比较 → 每 0.5 s 重建一次选项，
      // 下拉框刚展开就被关掉 / 选择被冲掉（「选不上地图」）。现在按地图名比较，名单真的变了才重建
      const mapKey = mapNames().map(n => mapOptText(store.mapLabels, n)).join('\n');   // 显示名变了也重建
      if (mapKey !== lastMaps){
        const keep = mapSel.value;
        lastMaps = mapKey;
        mapSel.replaceChildren(...mapNames().map(n => new Option(mapOptText(store.mapLabels, n), n)));
        if (keep && mapNames().includes(keep)) mapSel.value = keep;
        else if (store.mapCurrent) mapSel.value = store.mapCurrent;
      }
      if (store.mapCurrent !== lastMapCur){ lastMapCur = store.mapCurrent; if (store.mapCurrent) mapSel.value = store.mapCurrent; }
      const mapOk = !!store.mapCurrent;
      const navLive = up('navigation') || up('localization');
      setStep('stMap', mapOk ? 'done' : 'cur', mapOk ? `${mapTitle(store.mapLabels, store.mapCurrent)}${navLive ? ' · 导航在用' : ''}` : '等待地图');
      $('#navMapHint').textContent = navLive
        ? '导航正在用这张图：换图后 map_server 立即加载新图，要重新定位。'
        : '导航不用先开：选好地图就能编辑任务，点「出发」时用这张图启动导航。';
      $('#navMapLoad').hidden = !navLive;       // 导航没开时选了就换
      // 2 导航任务：路线 + 运行参数 + 出发（routes.js 管里面的内容），选好地图后一直展开
      const sel = $('#routeSelect').value, route = store.routes.find(r => r.id === sel && r.map === store.mapCurrent);
      const draft = store.routeDraft, editing = draft.points.length && (!route || draft.id !== route.id || !draft.confirmed);
      // 用户换选了别的任务：这次出发作废（启动导航、切图途中 mapCurrent 会短暂变化，找不到 route 不算）
      if (prep.on && sel && sel !== prep.route) resetPrep();
      const o = route?.opts;
      setStep('stRoute', mapOk ? 'cur' : 'todo',
        prep.on ? (prep.fail ? `出发失败：${prep.fail.msg}` : `${prep.resume ? '接续' : '出发'}中 · ${STEP_NAME[prep.step]}…`)
        : route && !editing ? `${route.name} · ${route.points.length} 站${o ? ` · ${o.loop ? (o.loops ? `循环 ${o.loops} 圈` : '一直循环') : '单程'}${o.through === false ? ' · 一站一站走' : ' · 连续通过'} · ${(+o.vmax).toFixed(2)} m/s` : ''}`
        : editing ? '正在编辑路线' : store.routes.some(r => r.map === store.mapCurrent) ? '选择一个任务' : '这张地图还没有任务，点「新建」');
      prepRender();
    }
    if (t === 'reloc'){
      $('#relocMeta').textContent = run('navigation') ? '导航栈保持运行' : up('localization') ? '只开了定位（没有 Nav2）' : '';
      $('#relocNote').innerHTML = run('navigation') ? '<b>导航栈保持运行</b> · 底盘已上锁。在当前估计的基础上修正，改完直接回到导航。'
        : run('localization') ? '<b>只开了定位</b>（map_server + AMCL）。修正完点「返回导航」，点「出发」时启动导航。'
        : '定位正在启动…';
      $('#relocBack').textContent = '完成，返回导航';
    }
    // 重定位的激光上色、拖动只在这个标签、定位在跑时生效
    reloc.on = t === 'reloc' && (run('navigation') || run('localization'));
    reloc.render();
    if (t === 'map'){
      const on = run('mapping'), starting_ = prof('mapping') === 'starting';
      $('#mapOff').hidden = on || starting_;
      $('#mapFlow').hidden = !on && !starting_;
      if (on || starting_){
        const since = store.since?.mapping, el = since != null && on ? src.now() - since : null;
        setStep('mpRun', on ? 'done' : 'cur', on ? `slam_toolbox · 已运行 ${el != null ? fmtUp(el) : '--'}` : '正在启动，机器人保持静止…');
        const tele = document.querySelector('[data-tool="teleop"]')?.classList.contains('on');
        const bag = prof('bag');
        setStep('mpTele', on ? 'cur' : 'todo', `${tele ? '遥控中' : '按 T 进入遥控，WASD 移动'}${bag === 'run' ? ' · 录包中' : ''}`);
        const mb = $('#mapBag'); mb.checked = bag === 'run' || bag === 'starting'; mb.disabled = !on || bag === 'starting' || bag === 'stopping';
        const ang = Math.round((store.alignment?.angle || 0) * 180 / Math.PI);
        setStep('mpAlign', on ? 'cur' : 'todo', ang ? `显示方向 ${ang}°` : '没有调整');
        // 地图名：没手动改过就用默认名；刚保存成功就换下一个
        if (store.mapSavedName && store.mapSavedName !== lastSaved){ lastSaved = store.mapSavedName; nameTouched = false; mapName.value = freeName(store.mapSavedName); }
        if (!nameTouched && document.activeElement !== mapName && !mapName.value) mapName.value = freeName(today());
        const saved = store.mapSavedAt;
        const A0 = store.autosave;
        setStep('mpSave', on ? 'cur' : 'todo', store.mapSaving ? '正在保存…' : saved ? `已保存 ${store.mapSavedName || ''} · ${Math.max(0, Math.round((Date.now() - saved) / 60000))} 分钟前`
          : A0?.wall ? `还没有保存 · 已自动保存 ${Math.round(A0.ago)} 秒前` : '还没有保存');
        const ct = store.cont;
        // 和建图流程其他步骤一样：建图在跑就是 cur（展开）。以前是 todo / done，而这两种状态在建图流程里
        // 都隐藏步骤内容（console.css），续建的按钮根本点不到
        setStep('mpCont', on ? 'cur' : 'todo',
          ct ? `${ct.base} → ${ct.name}${ct.check ? (ct.check.ok ? ' · 自检通过' : ' · 自检没通过') : ' · 还没自检'}` : '在已有地图上继续采集');
        // 建图自动保存（后端每 30 s 覆盖一份 autosave_<开始时间>，停止前、出问题时也会存）
        const A = store.autosave, au = $('#mpAuto');
        // 摔倒 / 里程计异常时后端冻结建图：闸门关上、自动保存停住，最近那份好的不再被覆盖
        const F = A?.frozen, fz = $('#mpFrozen');
        const hm = t => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false });
        fz.hidden = !F;
        fz.textContent = !F ? '' : `建图已冻结（${hm(F.wall)}）：${F.reason}。`
          + (F.gate ? '' : '扫描闸门不可用，地图可能还在被写坏。')
          + (F.kept ? `最近一份好的自动保存 ${F.kept}（${hm(F.kept_wall)}）不会再被覆盖。` : '')
          + (F.wild ? '里程计已经飘了，不能继续：请停止建图，把狗扶正静止后从这份图续建。'
            : '建议停止建图后从这份图续建；如果只是正常趴下、地图没乱，站起来后可以点「继续建图」。');
        $('#mpFrozenRow').hidden = !F || F.wild;
        const autoTxt = !A ? '' : F ? '自动保存已暂停（建图冻结）' : A.err ? `自动保存失败：${A.err}` : A.running ? '正在自动保存…'
          : A.wall ? `已自动保存到 ${A.name} · ${A.ago < 60 ? `${Math.round(A.ago)} 秒前` : `${Math.round(A.ago / 60)} 分钟前`}` : `等待第一次自动保存（每 ${Math.round(A.period)} 秒）`;
        au.hidden = !A; au.textContent = A ? `${autoTxt}（每 ${Math.round(A.period)} 秒、停止前、出问题时覆盖这一份，只是兜底）` : '';
        au.classList.toggle('warn', !!A?.err || !!F);
        setStep('mpEnd', on ? 'cur' : 'todo', saved ? `导航会用 ${store.mapSavedName || '刚保存的地图'}` : A?.wall ? `还没正式保存；停止后可在地图列表找到自动保存 ${A.name}` : '先保存，否则停止后地图会丢失');
        $('#mapToNav').disabled = !on || !saved;
        $('#mapStop').disabled = !on;
      }
    }
  }
  const openRoute = () => { openSteps.add('stRoute'); render(); };
  store.onRouteEdit = openRoute;
  return { render, switchTo, openRoute };
}
