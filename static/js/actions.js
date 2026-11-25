// 用户操作：先按 store 做前置检查、必要时弹确认，再交给数据源执行。两种数据源共用这套规则。
// 数据源的指令可以返回 { ok, msg }（或它的 Promise）：ROS 源以后端的回复为准，模拟源不返回就算成功
import { toast, confirmBox, formBox } from './ui.js';
import { deg, isAutosave, mapTitle } from './util.js';
import { createRollbackCapture } from './recording-capture.js';

export const MODES = { mapping: '建图', localization: '定位', navigation: '导航' };
export const MODE_PKG = { mapping: 'slam_toolbox', localization: 'map_server · AMCL', navigation: 'AMCL · Nav2 · cmd_bridge' };

export function createActions(store, src){
  const rollbackCapture = createRollbackCapture(store, src, toast);
  const run = id => store.profiles[id] === 'run';
  const curMode = () => Object.keys(MODES).find(m => store.profiles[m] !== 'stop') || null;
  const canMove = () => store.armed && !store.estop && run('navigation') && store.odomOk && store.link.ok;
  // 能不能遥控（不管解没解锁）：导航档的速度桥，或不开导航时后端拉起的遥控速度桥（不查里程计）
  // 机器狗在策略（Velocity）里、能走；没接运控（ctrl.on = false）时不管
  const walking = () => !store.ctrl?.on || (store.ctrl.state === 'Velocity' && store.ctrl.ready);
  const canTeleop = () => store.link.ok && !store.estop && ((run('navigation') && store.odomOk) || run('bridge'));
  // 点「出发」前就能判断的问题（站立、导航、定位由出发流程自动处理，不在这里拦）
  function routeGoBlocker(route){
    if (!route) return '请先保存或选择一个任务。';
    if (!store.link.ok) return '等待连接后端。';
    if (store.task.status === 'active' || store.profiles.patrol !== 'stop') return '请先结束当前导航或巡逻。';
    if (store.estop) return '请先解除急停。';
    if (store.busy) return '等待运行档位切换完成。';
    if (store.profiles.mapping !== 'stop') return '正在建图：先停止建图。';
    const bad = store.alerts?.find(x => x.go);                 // 运控离线、节点掉了……：点了也走不到底，先拦住
    if (bad) return `${bad.title}：${bad.fix}`;
    if (route.map !== store.mapCurrent) return `请先切换到路线地图 ${route.map}。`;
    return '';
  }
  // 真正下发任务时的检查：出发流程走到最后一步才调 startRoute，这时导航、定位、站立都应该好了
  function routeStartBlocker(route){
    const r = routeGoBlocker(route); if (r) return r;
    if (!run('navigation')) return '导航没有运行。';
    if (!store.odomOk || !store.robot.valid || store.fixed !== 'map') return '等待地图定位和里程计就绪。';
    if (!store.initPose) return '还没有定位。';
    if (!walking()) return '机器狗没有站起来。';
    return '';
  }
  const blocked = cost => cost != null && cost >= 253;
  // 数据源还不支持这个操作时（比如真机上的暂停），给出说明并中止
  const ready = op => src.can(op) || (src.cmd.notice(op), false);
  // 执行并提示结果：失败一律显示原因
  const exec = (p, okMsg, okKind = 'ok') => Promise.resolve(p).then(r => {
    if (r && r.ok === false) toast(r.msg || '操作失败', 'err');
    else if (okMsg !== null || (r && r.msg)) toast((r && r.msg) || okMsg, okKind);
    return r;
  });

  const A = {
    rollbackCapture,
    canMove, canTeleop, curMode, routeStartBlocker, routeGoBlocker, walking,
    // 站起：趴着 → 站稳 → 策略接管（后端一步步等运控的状态）
    standUp(){
      if (!ready('standUp')) return Promise.resolve({ ok: false });
      if (store.estop) return Promise.resolve((toast('急停中：先解除急停', 'err'), { ok: false, msg: '急停中' }));
      return exec(src.cmd.standUp(), null);
    },
    // 趴下：1005 是卸力直接塌下去。任务、导航会先取消，底盘上锁，等停稳再趴
    lieDown(){
      if (!ready('lieDown')) return;
      const moving = store.task.type !== 'idle' && store.task.status === 'active';
      confirmBox({ title: '趴下', ok: '趴下', danger: true,
        body: `${moving ? '当前任务会被取消、底盘上锁，停稳后' : ''}机器狗原地卸力趴下（直接塌下去，不是慢慢坐下）。确认周围没人、地面平整。`,
        onOk: () => exec(src.cmd.lieDown(), null) });
    },
    standHold(on){ if (ready('standHold')) exec(src.cmd.standHold(on), null); },
    // 切运控策略：后端只在趴着时执行（停掉运控、换策略重启，几秒钟）。onDone 用来把下拉框恢复成实际值
    setPolicy(name, onDone){
      const C = store.ctrl;
      if (!ready('setPolicy')) return onDone?.();
      if (!(C?.state === 'Passive' && C.ready) || C.op) return (toast('只能趴着时切换策略', 'err'), onDone?.());
      confirmBox({ title: '切换运控策略', ok: '切换',
        body: `从 ${C.policy || '（未知）'} 切到 ${name}。运控会重启几秒钟，期间机器狗保持趴着。不同策略站高不同：站起来后要重新定位，建图 / 续建请用同一个策略。重启扩展坞后会恢复为默认策略。`,
        onOk: () => exec(src.cmd.setPolicy(name), null).finally(() => onDone?.()),
        onCancel: () => onDone?.() });
    },
    // 爬台是普通模式选择，不与导航、示教或姿态操作互斥。
    climb(on){
      if (!ready('climb')) return;
      return exec(src.cmd.climb(!!on), null);
    },
    // fine = 重定位的微调：后端用小协方差，AMCL 不会把粒子撒开
    initialPose(p, fine = false){
      const no = msg => (toast(msg, 'err'), { ok: false, msg });
      if (!ready('initialPose')) return Promise.resolve({ ok: false });
      if (!run('localization') && !run('navigation')) return Promise.resolve(no('定位没有运行，无法设置初始位姿'));
      if (!fine && store.costAt && blocked(store.costAt(p.x, p.y))) return Promise.resolve(no('该位置在障碍物上'));
      return exec(src.cmd.initialPose(p, fine), fine ? null : '初始位姿已发布，粒子收敛中').then(r => { if (r?.ok !== false) store.onInitialPose?.(); return r; });
    },
    // 自动定位：后端用当前 scan 在整张地图上找机器人，可信才发给 AMCL（不可信只报告，不改位姿）
    autoLocalize(){
      if (!ready('autoLocalize')) return Promise.resolve({ ok: false });
      if (!run('localization') && !run('navigation')) return Promise.resolve((toast('定位没有运行', 'err'), { ok: false }));
      if (store.task.type !== 'idle' && store.task.status === 'active') return Promise.resolve((toast('任务进行中：先取消再自动定位', 'err'), { ok: false }));
      toast('自动定位中：机器人保持静止，约需 5–10 秒', 'warn');
      return exec(src.cmd.autoLocalize(), null);
    },
    // 自动定位失败后：机器狗自己小范围挪一次再定位（后端检查空间、按里程计控制距离，没解锁会临时解锁、结束后恢复上锁）
    autolocMove(){
      if (!ready('autolocMove')) return;
      if (store.estop) return toast('急停中：先解除急停', 'err');
      confirmBox({ title: '移动后重试自动定位', ok: '开始移动',
        body: '机器狗会自己小范围移动一次：优先前进约 0.3 m；前面不够空就后退 0.3 m；前后都不够就原地左转 45°。' +
          '移动前会用激光检查空间，移动中有东西靠近会立刻停；底盘没解锁会临时解锁，结束后恢复上锁。确认周围没人。',
        onOk: () => { toast('正在移动，停稳后重新自动定位…', 'warn'); exec(src.cmd.autolocMove(), null); } });
    },
    routePoint(p){
      // 导航没开时地图是后端直接读的文件，点的坐标同样是地图坐标，可以照常选点
      const draft = store.routeDraft, map = store.mapCurrent;
      if (!map || !store.map) return toast('还没有地图：先在第 1 步选择地图', 'err');
      if (draft.map && draft.map !== map) return toast('路线属于另一张地图，请先新建路线', 'err');
      const k = store.routeReplace;
      const replacing = Number.isInteger(k) && k >= 0 && k < draft.points.length;
      if (!replacing && draft.points.length >= 50) return toast('每条路线最多 50 个点', 'err');
      if (store.costAt && blocked(store.costAt(p.x, p.y))) return toast('该位置在障碍物或膨胀区内', 'err');
      if (replacing){
        // 只换这一站的位置和朝向，站名、备注、顺序不变；和前后站重合的不收
        const near = [draft.points[k - 1], draft.points[k + 1]].some(q => q && Math.hypot(p.x - q.x, p.y - q.y) < .05);
        if (near) return toast('和相邻的站重合了，换个位置', 'warn');
        const points = draft.points.map((q, j) => j === k ? { ...q, x:p.x, y:p.y, th:p.th, manualHeading:p.manualHeading } : q);
        store.routeDraft = { ...draft, map, points, confirmed: false };
        store.routeReplace = null;
        toast(`第 ${k + 1} 点已改到新位置，确认路线后保存`, 'ok');
        store.onRouteEdit?.();
        return true;
      }
      const at = store.routeInsert;
      if (Number.isInteger(at) && at >= 0 && at < draft.points.length){
        // 插入：新站放在下标 at（原来的第 at 站之后），后面的站顺延；插完继续在新站后面插，方便连着插几站
        const near = [draft.points[at - 1], draft.points[at]].some(q => q && Math.hypot(p.x - q.x, p.y - q.y) < .05);
        if (near) return toast('和相邻的站重合了，换个位置', 'warn');
        const points = [...draft.points];
        points.splice(at, 0, p);
        store.routeDraft = { ...draft, map, points, confirmed: false };
        store.routeInsert = at + 1;
        toast(`已插入为第 ${at + 1} 站，可以接着点下一站，Esc 结束`, 'ok');
        store.onRouteEdit?.();
        return 'insert';
      }
      store.routeInsert = null;
      const last = draft.points.at(-1);
      if (last && Math.hypot(p.x - last.x, p.y - last.y) < .05) return toast('这一站已添加', 'warn');
      store.routeDraft = { ...draft, map, points: [...draft.points, p], confirmed: false };
      store.onRouteEdit?.();   // 右栏展开第 2 步
    },
    checkRoute(draft){
      if (src.name !== 'ros') return Promise.resolve({ ok:false, msg:'纯前端模拟，尚未用 Nav2 检查路径' });
      return src.cmd.checkRoute({ map:draft.map, points:draft.points });
    },
    // opts：运行参数 { loop, loops, vmax, wmax }，跟任务一起保存（11-12 起）
    saveRoute(name, opts){
      const { id, map, points, confirmed } = store.routeDraft;
      if (!confirmed) return toast('请先确认路线', 'err');
      if (!name.trim() || points.length < 2 || map !== store.mapCurrent) return toast('请在当前地图添加至少两个点，并填写任务名', 'err');
      return exec(src.cmd.saveRoute({ route_id: id, name, map, points, opts }), '任务已保存');
    },
    deleteRoute(route, onDeleted){
      confirmBox({ title:'删除任务', body:`确定删除「${route.name}」？`, ok:'删除', danger:true,
        onOk: async () => {
          const reply = await exec(src.cmd.deleteRoute(route.id), '任务已删除');
          if (reply?.ok) onDeleted();
        } });
    },
    terminateRoute(route_id){
      if (!ready('terminateRoute')) return Promise.resolve({ ok:false });
      if (!store.link.ok) return Promise.resolve({ ok:false, msg:'等待连接后端' });
      if (store.task.status === 'active' && store.task.routeId === route_id)
        return Promise.resolve((toast('请先结束正在执行的任务', 'err'), { ok:false }));
      return exec(src.cmd.terminateRoute(route_id), '未完成任务已终止，已保存的路线仍保留');
    },
    // 下发任务：出发流程（flow.js）站好、导航和定位都就绪后调用，自动解锁出发（后端 route_start 带 arm）。
    // opts.vmax / wmax：任务里保存的速度上限，后端出发前设给 cmd_bridge。
    // 只有激光和地图对不太上（匹配率 < 60%）时才多问一句
    startRoute(route, opts = {}){
      const reason = routeStartBlocker(route);
      if (reason) return toast(reason, 'err');
      const go = () => exec(src.cmd.startRoute(route.id, { ...opts, arm: true }), opts.resume ? '底盘已解锁，任务已接续' : '底盘已解锁，任务已出发');
      const R = store.robot, m = store.loc.match;
      if (m == null || m >= 60) return go();
      return new Promise(resolve => formBox({ title: '定位可能不准', ok: '仍然出发', danger: true,
        rows: [['当前位置', `(${R.x.toFixed(2)}, ${R.y.toFixed(2)}) ${Math.round(deg(R.th)) || 0}°`], ['匹配率', `${m.toFixed(0)}%`]],
        warn: `激光和地图的匹配率只有 ${m.toFixed(0)}%，建议先重新定位。`,
        onCancel: () => resolve(null), onOk: () => resolve(go()) }));
    },
    cancel(){ if (ready('cancel')) exec(src.cmd.cancel(), '任务已取消'); },
    pause(){ if (ready('pause')) src.cmd.pause(!src.paused); },
    arm(){
      if (!ready('arm') || store.estop) return;
      if (store.armed) return exec(src.cmd.arm(false), '底盘已锁定');
      if (!run('navigation') && !run('bridge')) return toast('速度桥不在线（刚开机或刚切换档位时要等几秒）', 'err');
      if (run('navigation') && !store.odomOk) return toast('里程计不健康，不能解锁', 'err');
      confirmBox({ title: '解锁底盘', body: '解锁后机器人会执行导航和遥控的速度指令。确认机器人周围没有人和障碍物。', ok: '解锁',
        onOk: () => exec(src.cmd.arm(true), '底盘已解锁') });
    },
    estop(){
      if (!ready('estop') || store.estop) return;
      store.estop = true; // 先在界面上锁存，后端的状态随后确认
      Promise.resolve(src.cmd.estop()).then(r => {
        if (r && r.ok === false) toast(store.link.ok ? `急停没有完全生效：${r.msg}` : '急停没有发出去：网页和后端断开了，请用遥控器停车', 'err');
      });
    },
    releaseEstop(){ exec(src.cmd.releaseEstop(), '急停已解除，底盘仍处于锁定', 'warn'); },
    setParam(k, v){ if (ready('setParam')) return exec(src.cmd.setParam(k, v), null); },
    setMode(m){
      if (store.mapSaving || store.mapBusy) return toast('地图正在保存，请稍等', 'warn');
      if (!ready('setMode')) return;
      if (store.busy) return toast('上一个启停还没完成，稍等几秒', 'warn');
      const cur = curMode();
      if (m === cur) return;
      const still = 'FAST-LIO 初始化时机器人要站好、停在平地上不要动。';
      const mapNote = m === 'localization' || m === 'navigation' ? `地图：${store.mapChoice || store.mapCurrent || '默认'}。` : '';
      if (!cur){
        if (!m) return;
        return confirmBox({ title: `启动「${MODES[m]}」`, body: `${mapNote}${still}`, ok: '启动', onOk: () => { if (m === 'mapping') store.mapSavedAt = null; exec(src.cmd.setMode(m), null); } });
      }
      // 离开建图：slam_toolbox 一停，没保存的地图就没了。保存记录只在这个页面里，刷新后算没有
      const leavingMap = cur === 'mapping';
      const saved = store.mapSavedAt;
      const mapWarn = !leavingMap ? '' : saved
        ? `地图上次保存是 ${Math.max(1, Math.round((Date.now() - saved) / 60000))} 分钟前，之后走过的部分会丢失。`
        : '这个页面上还没有保存过当前地图，停止建图后地图会丢失。要保存就点「取消」，在右栏「建图」第 4 步保存。';
      const extra = [run('patrol') && '巡逻（命令行启动的 patrol.py）'].filter(Boolean);
      const body = m
        ? `「${MODES[m]}」和「${MODES[cur]}」不能同时运行。将先停止${MODES[cur]}${extra.length ? '和' + extra.join('、') : ''}，再启动${MODES[m]}。正在执行的任务会取消，底盘会锁定。${mapNote}${still}`
        : `将停止${MODES[cur]}${extra.length ? '和' + extra.join('、') : ''}。正在执行的任务会取消，底盘会锁定。`;
      confirmBox({ title: m ? `切换到「${MODES[m]}」` : '停止全部', body: mapWarn + body, danger: true,
        ok: leavingMap && !saved ? (m ? '不保存，继续切换' : '不保存，继续停止') : m ? '切换' : '停止',
        onOk: () => { if (m === 'mapping') store.mapSavedAt = null; exec(src.cmd.setMode(m), null); } });
    },
    toggle(id){
      if (!ready('toggle')) return;
      exec(src.cmd.toggle(id), null);
    },
    async saveMap(name){
      if (!ready('saveMap')) return;
      if (store.mapSaving || store.mapBusy) return toast('地图正在保存，请稍等', 'warn');
      if (!run('mapping')) return toast('保存地图需要「建图」正在运行', 'err');
      store.mapSaving = true;
      try {
        const result = await exec(src.cmd.saveMap(name), `地图已保存：${name}`);
        if (!result || result.ok !== false){ store.mapSavedAt = Date.now(); store.mapSavedName = name; }
        return result;
      } finally { store.mapSaving = false; }
    },
    // ---------- 续建（docs/地图续建.md） ----------
    // 四步都是显式动作，不串成大流程：起点位姿给错不会报错，只能靠人在中间看一眼自检结果。
    contPreflight(base){
      if (!ready('contPreflight')) return;
      if (!base) return toast('先选一张基准图', 'warn');
      return exec(src.cmd.contPreflight(base), null).then(r => {
        store.contPre = r?.detail || r?.preflight || null;
        return r;
      });
    },
    contStart(base, name, pose, source){
      if (!ready('contStart')) return;
      if (!run('mapping')) return toast('续建要先让「建图」跑起来', 'err');
      if (!base) return toast('先选一张基准图', 'warn');
      if (!/^[A-Za-z0-9_-]{1,40}$/.test(name || '')) return toast('新图名限 1–40 个字母、数字、下划线或减号', 'err');
      if (name === base) return toast('续建必须另存新名字，不能覆盖基准图', 'err');
      const manual = source === 'manual';
      return confirmBox({ title: `在 ${base} 上续建`,
        body: `新图叫 ${name}。${manual ? '使用手动输入的位姿。' : source === 'parking' ? '请确认机器人正停在已记录的停车点。' : '在基准图上用当前激光自动定位。'}`
          + '请保持机器人静止。旧墙贴合自检通过后开始采集。',
        ok: '开始续建', danger: manual,
        // 要等静止激光 + 自动定位 + 加载基准图，5–15 s：期间按钮显示进行中，失败原因留在状态栏
        // （提示框只停 2.6 s，长的原因一闪就没了，看起来像「点了没反应」）
        onOk: () => {
          store.contStarting = true; store.contErr = null; store.contResult = null;
          return exec(src.cmd.contStart(base, name, pose, source), null).then(r => {
            store.contStarting = false;
            store.contErr = r && r.ok === false ? (r.msg || '没开始，原因未知') : null;
            store.contResult = r && r.ok === false ? r.data : null;
            // 成功后到下一次状态推送（≤0.5 s）之间 store.cont 还是空的：这段时间继续显示进行中
            store.contStartOkAt = r && r.ok !== false ? Date.now() : 0;
            return r;
          });
        } });
    },
    contCheck(){
      if (!ready('contCheck')) return;
      return exec(src.cmd.contCheck(), null);
    },
    mapResume(){
      if (!ready('mapResume')) return;
      return exec(src.cmd.mapResume(), null);
    },
    contFinish(reason){
      if (!ready('contFinish')) return;
      const c = store.cont;
      if (!c) return toast('没有正在进行的续建', 'warn');
      if (!c.ready || !c.check?.ok) return toast('先跑自检并通过', 'err');
      return exec(src.cmd.contFinish(reason), null).then(r => {
        if (r && r.ok !== false){ store.mapSavedAt = Date.now(); store.mapSavedName = c.name; }
        return r;
      });
    },
    contAbort(){
      if (!ready('contAbort')) return;
      confirmBox({ title: '放弃这次续建', body: '放弃会暂停扫描。可以重新选择基准图续建，或者重开建图档位。',
        danger: true, ok: '放弃', onOk: () => {
          store.contErr = null; store.contResult = null;
          return exec(src.cmd.contAbort(), null);
        } });
    },
    contParking(base, clear){
      if (!ready('contParking')) return;
      if (!base) return toast('先选一张基准图', 'warn');
      if (clear) return exec(src.cmd.contParking(base, true), null);
      // 停车点要用机器人现在的定位；没有 map 定位时后端会拒绝，这里先说明原因
      if (!store.initPose) return toast('还没有定位：先自动定位或手动给位置，再记停车点', 'err');
      return exec(src.cmd.contParking(base, false), null);
    },
    // 地图显示方向：存到后端（按地图名），失败才提示
    setMapView(map, angle){
      if (!src.cmd.setMapView) return;
      return Promise.resolve(src.cmd.setMapView(map, angle)).then(r => {
        if (r && r.ok === false) toast(r.msg || '保存地图方向失败', 'err');
        return r;
      });
    },
    async saveAlignedMap(name){
      if (!ready('alignMap') || store.mapSaving || store.mapBusy) return;
      if (!run('mapping')) return toast('保存对齐地图需要「建图」正在运行', 'err');
      if (!/^[A-Za-z0-9_-]{1,40}$/.test(name)) return toast('原图名限 1–40 个字母、数字、下划线或减号', 'err');
      const alignment = store.alignment;
      // 按当前显示方向导出：图片最多转 ±90°，剩下的 180° 记成新地图的显示方向
      const view = alignment.angle, angle = Math.atan(Math.tan(view));
      if (Math.abs(angle) < 1e-4) return toast('当前显示方向已经是水平 / 竖直，不需要导出对齐地图', 'warn');
      const rest = Math.atan2(Math.sin(view - angle), Math.cos(view - angle));
      store.mapSaving = true;
      try {
        if (alignment.source !== name){
          const result = await exec(src.cmd.saveMap(name), null);
          if (result && result.ok === false) return result;
          alignment.source = name;
          store.mapSavedAt = Date.now();
        }
        const result = await exec(src.cmd.alignMap(name, name + '_aligned', angle), null);
        if (result?.ok !== false && Math.abs(rest) > 1e-4) await A.setMapView(name + '_aligned', rest);
        return result;
      } finally { store.mapSaving = false; }
    },
    // 地图编辑另存（v1.0.2）：p = { source, name, crc, w, h, runs, summary, routes }
    saveEditedMap(p){
      if (!ready('editMap')) return Promise.resolve({ ok: false });
      if (store.mapSaving || store.mapBusy){ toast('地图正在保存，请稍等', 'warn'); return Promise.resolve({ ok: false }); }
      store.mapSaving = true;
      return exec(src.cmd.editMap(p), null).finally(() => { store.mapSaving = false; });
    },
    // 禁行区（v1.1.8）：存到这张图上（不生成新图）；导航正在用这张图时后端马上热加载
    saveKeepout(map, shapes){
      if (!ready('saveKeepout')) return Promise.resolve({ ok: false });
      return exec(src.cmd.saveKeepout(map, shapes), null);
    },
    loadMap(name, { confirmed = false } = {}){
      if (store.mapSaving || store.mapBusy) return toast('地图正在保存，请稍等', 'warn');
      if (!ready('loadMap')) return;
      const live = run('localization') || run('navigation');
      if (src.name === 'ros' && !live) return exec(src.cmd.loadMap(name), null);   // 没在跑：只是选好下次启动用的地图
      // 调用方已经把后果说清楚并让用户勾选过（地图编辑「保存后切换」）：不再弹第二个确认框
      if (confirmed) return exec(src.cmd.loadMap(name), src.name === 'mock' ? `已加载 ${name}（模拟模式仍显示示例地图）` : null);
      confirmBox({ title: `切换到地图 ${mapTitle(store.mapLabels, name)}`, body: 'map_server 会立即加载这张地图，正在执行的导航会取消。切换后要重新设置初始位姿。', ok: '切换',
        onOk: () => exec(src.cmd.loadMap(name), src.name === 'mock' ? `已加载 ${name}（模拟模式仍显示示例地图）` : null) });
    },
    // 地图改名（11-23）：只改网页显示名，可以中文；data/maps 文件名、任务、云端都不受影响，所以正在用的图也能改
    renameMap(name){
      if (!name) return toast('先在下拉框里选一张地图', 'warn');
      if (!ready('setMapLabel')) return;
      const cur = store.mapLabels?.[name] || '';
      formBox({ title: '地图改名', ok: '保存',
        body: '只改网页上显示的名字，可以用中文。地图文件名不变，导航、任务、云端调度照常用文件名，正在导航用的图也可以改。',
        rows: [['文件名', name]],
        build: form => {
          form.innerHTML = `<div class="modal-label">显示名</div>
            <input class="modal-select" data-label maxlength="32" placeholder="例如：一楼实验室（留空 = 显示文件名）">
            <div class="mapdel-sum" data-err></div>`;
          const inp = form.querySelector('[data-label]');
          inp.value = cur;
          setTimeout(() => { inp.focus(); inp.select(); }, 0);
          inp.addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); form.closest('.modal').querySelector('[data-a=o]').click(); } });
        },
        valid: form => {
          const v = form.querySelector('[data-label]').value.trim().replace(/\s+/g, ' ');
          const bad = /[<>"'\\/]/.test(v) ? '不能有 < > " \' \\ / 这些符号'
            : Object.entries(store.mapLabels || {}).some(([k, l]) => k !== name && l === v) ? '另一张地图已经叫这个名字了'
            : v !== name && store.maps.some(m => m.name === v) ? '这是另一张地图的文件名' : '';
          form.querySelector('[data-err]').textContent = bad;
          return !bad && v !== cur && !(v === name && !cur);
        },
        onOk: form => exec(src.cmd.setMapLabel(name, form.querySelector('[data-label]').value), null) });
    },
    // 管理地图：勾选多张一起删（后端挪进 data/maps/.trash，14 天后清掉），绑在这些图上的任务一起删
    manageMaps(){
      if (!ready('deleteMaps')) return;
      if (store.mapSaving || store.mapBusy) return toast('地图正在保存，请稍等', 'warn');
      const navLive = run('localization') || run('navigation');
      const lock = m => navLive && m.name === store.mapCurrent ? '导航在用'
        : store.cont?.base === m.name ? '续建基准' : '';
      const nRoutes = name => store.routes.filter(r => r.map === name).length;
      const mb = b => b == null ? '' : b >= 1048576 ? (b / 1048576).toFixed(b >= 10485760 ? 0 : 1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB';
      const when = t => { if (!t) return ''; const d = new Date(t * 1000), p = v => String(v).padStart(2, '0');
        return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; };
      const maps = [...store.maps];   // 打开时的快照：列表在弹窗里不跟着 2 Hz 刷新，免得勾选被打断
      const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' })[c]);
      formBox({ title: `管理地图（${maps.length} 张）`, ok: '删除所选', danger: true,
        build: form => {
          form.closest('.modal').classList.add('modal-wide');
          const auto = maps.filter(m => isAutosave(m.name) && !lock(m)).length;
          form.innerHTML = `<div class="mapdel-bar"><input class="modal-select" data-q placeholder="筛选地图名 / 显示名…">
              <button type="button" class="btn sm ghost" data-auto ${auto ? '' : 'disabled'}>选中自动保存（${auto}）</button>
              <button type="button" class="btn sm ghost" data-none>全不选</button></div>
            <div class="mapdel-list">${maps.map((m, i) => { const l = lock(m), r = nRoutes(m.name);
              const lb = store.mapLabels?.[m.name];
              return `<label class="mapdel-it${l ? ' locked' : ''}" data-n="${esc(m.name + ' ' + (lb || ''))}"><input type="checkbox" data-i="${i}" ${l ? 'disabled' : ''}>
                <span class="n">${lb ? `<span class="lb">${esc(lb)}</span> ` : ''}${esc(m.name)}</span>${l ? `<em class="tag">${l}</em>` : m.cur || m.name === store.mapCurrent ? '<em class="tag dim">当前显示</em>' : ''}
                <span class="m">${mb(m.size)}${m.mtime ? ' · ' + when(m.mtime) : ''}${r ? ` · <b>${r} 个任务</b>` : ''}</span></label>`; }).join('')}</div>
            <div class="mapdel-sum" data-sum>勾选要删除的地图</div>`;
          const boxes = [...form.querySelectorAll('input[data-i]')];
          const sum = () => { const sel = boxes.filter(b => b.checked).map(b => maps[+b.dataset.i]);
            const r = sel.reduce((a, m) => a + nRoutes(m.name), 0), size = sel.reduce((a, m) => a + (m.size || 0), 0);
            form.querySelector('[data-sum]').textContent = sel.length
              ? `将删除 ${sel.length} 张地图（${mb(size)}）${r ? `，连同绑在上面的 ${r} 个任务` : ''}。文件先挪到 data/maps/.trash，14 天后自动清掉。`
              : '勾选要删除的地图'; };
          const fire = () => form.dispatchEvent(new Event('change'));
          form.querySelector('[data-q]').oninput = e => { const q = e.target.value.trim().toLowerCase();
            form.querySelectorAll('.mapdel-it').forEach(el => el.hidden = !!q && !el.dataset.n.toLowerCase().includes(q)); };
          form.querySelector('[data-auto]').onclick = () => { boxes.forEach(b => { if (!b.disabled && isAutosave(maps[+b.dataset.i].name)) b.checked = true; }); fire(); };
          form.querySelector('[data-none]').onclick = () => { boxes.forEach(b => b.checked = false); fire(); };
          form.addEventListener('change', sum);
        },
        valid: form => [...form.querySelectorAll('input[data-i]')].some(b => b.checked),
        onOk: form => {
          const names = [...form.querySelectorAll('input[data-i]')].filter(b => b.checked).map(b => maps[+b.dataset.i].name);
          return exec(src.cmd.deleteMaps(names), null);
        } });
    },
    deleteBag(bag){
      if (!ready('deleteBag')) return;
      const size = bag.size > 1024 ? (bag.size/1024).toFixed(1) + ' GB' : bag.size.toFixed(0) + ' MB';
      confirmBox({ title:'删除录包', body:`确定删除 data/bags/${bag.name}（${size}）？删除后无法恢复。`, ok:'删除', danger:true,
        onOk: () => exec(src.cmd.deleteBag(bag.name), `已删除录包 ${bag.name}`) });
    },
    setTaughtNav(on){
      if (!ready('setTaughtNav')) return;
      return exec(src.cmd.setTaughtNav(on), null);
    },
    setRollback(on){
      if (!ready('setRollback')) return;
      store.rollback = { ...store.rollback, on };
      exec(src.cmd.setRollback(on), null);
    },
    saveRollback(){
      if (!ready('saveRollback')) return;
      return rollbackCapture.save();
    },
    uploadBag(bag){
      if (!ready('uploadBag')) return;
      if (store.uploads[bag.name]?.state === 'done') return confirmBox({ title:'核对已上传录包',
        body:`${bag.name} 已上传，无需重复上传。核对服务器时只补齐缺失或变化的文件，仍使用同一个目录。`, ok:'核对并补齐',
        onOk:() => exec(src.cmd.uploadBag(bag.name, true), null) });
      return A.uploadBags([bag]);
    },
    uploadBags(bags){
      if (!ready('uploadBag')) return;
      const names = [...new Set(bags.map(b => b.name))].filter(name => !['queued', 'run', 'done'].includes(store.uploads[name]?.state));
      if (!names.length) return;
      const send = () => Promise.all(names.map(name => exec(src.cmd.uploadBag(name), null)));
      return send();
    },
    simulateDrop(){ if (ready('simulateDrop')) exec(src.cmd.simulateDrop(), null); },
  };
  return A;
}
