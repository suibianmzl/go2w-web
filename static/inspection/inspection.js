import { gridToCanvas } from '../js/util.js';
import { createRtcViewer } from '../js/rtc_viewer.js';
import {
  INSPECTION_NODES,
  COT_BY_NOTE,
  cotEventFamilyKey,
  inspectionLocalizationHints,
  inspectionNodeIndexForTask,
  inspectionTaskRestarted,
} from './cot_mapping.js';
import { createFootwayBlockageWatch, footwayLowSpeedActive } from './blockage_watch.js';

const $ = selector => document.querySelector(selector);
const T_MAP = 1;
const POSE_RENDER_MS = 1000;
const TRAIL_MIN_MOVE = 0.04;
const HEARTBEAT_MS = 500;
const COT_REVEAL_MS = 700;
const COT_PUBLISH_MS = 300;
const DEFAULT_ROUTE_ID = '49997f67fac041ecb7a74ab12259b3af';

const els = {
  robotChip: $('#robotChip'),
  linkChip: $('#linkChip'),
  clock: $('#clock'),
  consoleLink: $('#consoleLink'),
  fleetLink: $('#fleetLink'),
  routeSelect: $('#routeSelect'),
  taskPill: $('#taskPill'),
  taskStateText: $('#taskStateText'),
  taskPercent: $('#taskPercent'),
  taskProgress: $('#taskProgress'),
  startBtn: $('#startBtn'),
  stopBtn: $('#stopBtn'),
  steps: $('#steps'),
  presence: $('#presence'),
  modeValue: $('#modeValue'),
  speedValue: $('#speedValue'),
  batteryValue: $('#batteryValue'),
  cpuValue: $('#cpuValue'),
  rtcBadge: $('#rtcBadge'),
  rtcMessage: $('#rtcMessage'),
  rtcVideo: $('#rtcVideo'),
  mapOverlay: $('#mapOverlay'),
  mapCanvas: $('#inspectionMap'),
  mapEmpty: $('#mapEmpty'),
  zoomOutMap: $('#zoomOutMap'),
  zoomInMap: $('#zoomInMap'),
  collapseMap: $('#collapseMap'),
  expandMap: $('#expandMap'),
  exceptionRows: $('#exceptionRows'),
  exceptionEmpty: $('#exceptionEmpty'),
  exceptionCount: $('#exceptionCount'),
  exceptionMore: $('#exceptionMore'),
  exceptionDetail: $('#exceptionDetail'),
  detailTitle: $('#detailTitle'),
  detailBody: $('#detailBody'),
  detailId: $('#detailId'),
  detailStatus: $('#detailStatus'),
  detailPose: $('#detailPose'),
  detailMap: $('#detailMap'),
  detailBag: $('#detailBag'),
  detailRobot: $('#detailRobot'),
  detailClose: $('#detailClose'),
  incidentHistory: $('#incidentHistory'),
  historyRows: $('#historyRows'),
  historyEmpty: $('#historyEmpty'),
  historySummary: $('#historySummary'),
  historyMore: $('#historyMore'),
  historyClose: $('#historyClose'),
  incidentPlayback: $('#incidentPlayback'),
  playbackTitle: $('#playbackTitle'),
  playbackType: $('#playbackType'),
  playbackTime: $('#playbackTime'),
  playbackPoint: $('#playbackPoint'),
  playbackBag: $('#playbackBag'),
  incidentSnapshot: $('#incidentSnapshot'),
  snapshotPlaceholder: $('#snapshotPlaceholder'),
  snapshotPlaceholderTitle: $('#snapshotPlaceholderTitle'),
  snapshotPlaceholderText: $('#snapshotPlaceholderText'),
  snapshotRetry: $('#snapshotRetry'),
  playbackClose: $('#playbackClose'),
  toast: $('#toast'),
};

const state = {
  robotId: '',
  ws: null,
  wsOpen: false,
  reconnectTimer: null,
  heartbeat: null,
  pending: new Map(),
  commandId: 0,
  online: false,
  videoUploadEnabled: null,
  map: null,
  mapLoading: false,
  pose: null,
  poseAt: 0,
  machine: null,
  task: { type: 'idle' },
  routes: [],
  selectedRouteId: '',
  incidents: [],
  history: { items: new Map(), before: null, loading: false, hasMore: true },
  playbackId: null,
  openNodeIndex: null,
  lastOpenedNodeIndex: null,
  activeNodeIndex: -1,
  activeCotKey: '',
  cotEvents: [],
  cotEventKeys: new Set(),
  cotTimer: null,
  cotLastAt: null,
  preparing: false,
  prepareText: '',
  inspectionRunStart: null,
  inspectionRouteId: null,
  trail: [],
  lastTrailPoint: null,
  rtcStarted: false,
  snapshotToken: 0,
  snapshotObjectUrl: '',
};

const footwayBlockage = createFootwayBlockageWatch();
let cotPublishTimer = null;

const rtc = createRtcViewer(els.rtcVideo, message => {
  els.rtcMessage.textContent = message || '';
  els.rtcMessage.hidden = !message;
  els.rtcBadge.dataset.s = message ? 'warn' : 'ok';
}, '/quadruped');

let toastTimer = null;
function toast(message, kind = 'ok'){
  clearTimeout(toastTimer);
  els.toast.querySelector('i').textContent = kind === 'err' ? '!' : kind === 'warn' ? '!' : '✓';
  els.toast.querySelector('span').textContent = message;
  els.toast.dataset.k = kind;
  els.toast.classList.add('show');
  toastTimer = setTimeout(() => els.toast.classList.remove('show'), 2800);
}

function esc(value){
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

function selectedRoute(){
  return state.routes.find(route => route.id === state.selectedRouteId) || null;
}

function taskActive(){
  return state.task?.type !== 'idle' && state.task?.status === 'active';
}

function taskForSelectedRoute(){
  return state.task?.type !== 'idle' && state.task?.routeId === state.selectedRouteId;
}

function routeForTask(task){
  return state.routes.find(item => item.id === task?.routeId) || null;
}

function routePointForTask(task){
  const route = routeForTask(task);
  const index = Number(task?.point) - 1;
  return Number.isInteger(index) && index >= 0 ? route?.points?.[index] || null : null;
}

function taskNote(task){
  return String(task?.pointNote || routePointForTask(task)?.note || '').trim();
}

function taskPointName(task){
  return String(task?.pointName || routePointForTask(task)?.name || '').trim();
}

function cotDefinitionForTask(task){
  if (task?.back){
    const returning = COT_BY_NOTE.get('position_end');
    return returning ? { ...returning, cruiseName:'返回起点', back:true } : null;
  }
  const note = taskNote(task).toLowerCase();
  return note ? COT_BY_NOTE.get(note) || null : null;
}

function inspectionNodeForTask(task){
  return inspectionNodeIndexForTask(task, routeForTask(task)?.points || []);
}

function displayedInspectionNodeIndex(task){
  const mapped = inspectionNodeForTask(task);
  return mapped >= 0 ? mapped : state.activeNodeIndex;
}

function nodeCots(index){
  return state.cotEvents.filter(event => event.nodeIndex === index);
}

function isBlockingIncident(item){
  return item?.type === 'stuck'
    || /阻塞|受阻|无通行空间/.test(`${item?.label || ''}${item?.msg || ''}`);
}

function incidentNodeIndex(item){
  return inspectionNodeForTask(item?.task);
}

function nodeIncidents(index){
  if (!state.inspectionRunStart) return [];
  return state.incidents
    .filter(item => (
      incidentNodeIndex(item) === index
      && Number(item.ts) >= state.inspectionRunStart
      && (!state.inspectionRouteId
        || !item.task?.routeId
        || item.task.routeId === state.inspectionRouteId)
    ))
    .sort((left, right) => right.ts - left.ts);
}

function stopCotReveal(){
  if (state.cotTimer){
    clearTimeout(state.cotTimer);
    state.cotTimer = null;
  }
}

function stopLineTimer(line, finalize = false){
  const timer = line?.timer;
  if (!timer) return;
  if (timer.timerId){
    clearInterval(timer.timerId);
    delete timer.timerId;
  }
  timer.running = false;
  if (finalize && timer.type === 'countdown'){
    line.text = timer.format(timer.to ?? 0);
  }
  scheduleCotPublish();
}

function stopAllCotLineTimers(finalize = false){
  for (const event of state.cotEvents){
    for (const line of event.lines || []) stopLineTimer(line, finalize);
  }
}

function startLineTimer(event, lineIndex){
  const line = event?.lines?.[lineIndex];
  const timer = line?.timer;
  if (!timer || timer.timerId) return;
  if (!timer.format) timer.format = timerFormatter(timer, event);
  const startedAt = Number(timer.startedAt) > 0 ? Number(timer.startedAt) : Date.now();
  timer.startedAt = startedAt;
  const tick = () => {
    if (!timer.running) return;
    const elapsed = Math.max(0, Math.floor(
      (Date.now() - startedAt) / (timer.intervalMs || 1000),
    ));
    const value = timer.type === 'countdown'
      ? Math.max(timer.to ?? 0, timer.from - elapsed)
      : timer.from + elapsed;
    line.text = timer.format(value);
    scheduleCotPublish();
    renderTask();
    if (timer.type === 'countdown' && value <= (timer.to ?? 0)) stopLineTimer(line);
  };
  timer.running = true;
  tick();
  if (timer.running) timer.timerId = setInterval(tick, timer.intervalMs || 1000);
}

function timerFormatter(timer, event = null){
  if (timer?.formatKey === 'dwell' || event?.note === 'position_monitoring' || event?.note === 'position_trail'){
    return seconds => `定点巡逻中（${seconds}s）`;
  }
  if (timer?.formatKey === 'blockage' || event?.blockage){
    return seconds => `检测到阻塞（> ${seconds}s）`;
  }
  return seconds => String(seconds);
}

function cotSnapshot(routeId, runStart, events = state.cotEvents){
  return {
    routeId,
    runStart:Number.isFinite(runStart) ? runStart : null,
    activeNodeIndex:state.activeNodeIndex,
    openNodeIndex:state.openNodeIndex,
    activeCotKey:state.activeCotKey,
    events:events.map(event => ({
      key:event.key,
      routeId:event.routeId,
      point:event.point,
      loop:event.loop,
      back:!!event.back,
      note:event.note,
      cruiseName:event.cruiseName,
      nodeIndex:event.nodeIndex,
      visible:event.visible,
      at:event.at,
      fallback:!!event.fallback,
      blockage:!!event.blockage,
      lines:(event.lines || []).map(line => ({
        text:line.text,
        delayMs:line.delayMs,
        timer:line.timer ? {
          type:line.timer.type,
          from:line.timer.from,
          to:line.timer.to,
          intervalMs:line.timer.intervalMs,
          startedAt:line.timer.startedAt,
          running:!!line.timer.running,
          formatKey:line.timer.formatKey || '',
        } : null,
      })),
    })),
  };
}

function publishCotSnapshot(snapshot){
  if (!state.wsOpen || state.ws?.readyState !== WebSocket.OPEN || !snapshot?.routeId) return;
  state.ws.send(JSON.stringify({ t:'inspection_cot', snapshot }));
}

function scheduleCotPublish(delay = COT_PUBLISH_MS){
  if (!state.wsOpen || !state.robotId) return;
  clearTimeout(cotPublishTimer);
  cotPublishTimer = setTimeout(() => {
    cotPublishTimer = null;
    const routeId = state.inspectionRouteId || state.task?.routeId || '';
    if (!routeId) return;
    publishCotSnapshot(cotSnapshot(routeId, state.inspectionRunStart));
  }, delay);
}

function clearCachedCot(routeId, runStart){
  if (!routeId) return;
  const snapshot = cotSnapshot(routeId, runStart, []);
  snapshot.activeNodeIndex = -1;
  snapshot.openNodeIndex = null;
  snapshot.activeCotKey = '';
  publishCotSnapshot(snapshot);
}

function restoreCotSnapshot(snapshot){
  if (!snapshot || typeof snapshot !== 'object') return false;
  const routeId = String(snapshot.routeId || '');
  const events = Array.isArray(snapshot.events) ? snapshot.events : [];
  const currentRouteId = state.inspectionRouteId || state.task?.routeId || '';
  const task = state.task || {};
  if (!routeId || !task.routeId || task.type !== 'route') return false;
  if (currentRouteId && currentRouteId !== routeId) return false;
  const incomingStart = Number(snapshot.runStart);
  const currentStart = Number(state.inspectionRunStart);
  if (Number.isFinite(incomingStart) && Number.isFinite(currentStart)
      && Math.abs(incomingStart - currentStart) > 15000) return false;

  state.inspectionRouteId = routeId;
  if (Number.isFinite(incomingStart)) state.inspectionRunStart = incomingStart;
  state.cotEvents = events.map(raw => {
    const rawNodeIndex = Number(raw?.nodeIndex);
    const event = {
      key:String(raw?.key || ''),
      routeId:String(raw?.routeId || routeId),
      point:Number(raw?.point) || 1,
      loop:Number(raw?.loop) || 1,
      back:!!raw?.back,
      note:String(raw?.note || ''),
      cruiseName:String(raw?.cruiseName || ''),
      nodeIndex:Number.isFinite(rawNodeIndex) ? rawNodeIndex : -1,
      visible:Math.max(0, Number(raw?.visible) || 0),
      at:Number(raw?.at) || Date.now(),
      fallback:!!raw?.fallback,
      blockage:!!raw?.blockage,
      lines:[],
    };
    event.lines = (Array.isArray(raw?.lines) ? raw.lines : []).map(rawLine => {
      const rawDelay = Number(rawLine?.delayMs);
      const timer = rawLine?.timer && typeof rawLine.timer === 'object' ? {
        type:rawLine.timer.type,
        from:Number(rawLine.timer.from) || 0,
        to:Number(rawLine.timer.to) || 0,
        intervalMs:Number(rawLine.timer.intervalMs) || 1000,
        startedAt:Number(rawLine.timer.startedAt) || 0,
        running:!!rawLine.timer.running,
        formatKey:String(rawLine.timer.formatKey || ''),
      } : null;
      if (timer) timer.format = timerFormatter(timer, event);
      return {
        text:String(rawLine?.text || ''),
        delayMs:Number.isFinite(rawDelay) ? rawDelay : COT_REVEAL_MS,
        timer,
      };
    }).filter(line => line.text);
    event.visible = Math.min(event.visible, event.lines.length);
    return event;
  }).filter(event => event.key && event.lines.length);
  state.cotEventKeys = new Set(state.cotEvents.map(event => event.key));
  state.activeCotKey = state.cotEventKeys.has(snapshot.activeCotKey)
    ? snapshot.activeCotKey
    : state.cotEvents.at(-1)?.key || '';
  state.activeNodeIndex = Number.isFinite(Number(snapshot.activeNodeIndex))
    ? Number(snapshot.activeNodeIndex) : -1;
  state.openNodeIndex = snapshot.openNodeIndex == null
    ? null : Number(snapshot.openNodeIndex);
  state.cotLastAt = state.cotEvents.reduce((latest, event) => Math.max(latest, event.at || 0), 0) || null;
  for (const event of state.cotEvents){
    event.lines.forEach((line, index) => {
      if (line.timer?.running) startLineTimer(event, index);
    });
  }
  renderTask();
  const active = state.cotEvents.find(event => event.key === state.activeCotKey);
  if (active && active.visible < active.lines.length) scheduleCotReveal();
  return true;
}

function resetCotOutput(publishClear = true){
  const routeId = state.inspectionRouteId || state.task?.routeId || '';
  const runStart = state.inspectionRunStart;
  const hadEvents = state.cotEvents.length > 0;
  stopCotReveal();
  stopAllCotLineTimers();
  footwayBlockage.resetRun();
  state.cotEvents = [];
  state.cotEventKeys.clear();
  state.activeNodeIndex = -1;
  state.activeCotKey = '';
  state.openNodeIndex = null;
  state.lastOpenedNodeIndex = null;
  state.cotLastAt = null;
  state.inspectionRunStart = null;
  state.inspectionRouteId = null;
  if (publishClear && hadEvents) clearCachedCot(routeId, runStart);
}

function beginInspectionRun(task, startedAt = Date.now()){
  resetCotOutput(false);
  state.inspectionRunStart = startedAt;
  state.inspectionRouteId = task?.routeId || null;
  const index = inspectionNodeForTask(task);
  if (index >= 0){
    state.activeNodeIndex = index;
    state.openNodeIndex = index;
  }
}

function syncInspectionNode(){
  if (!state.inspectionRunStart || !taskActive()) return;
  const index = displayedInspectionNodeIndex(state.task);
  if (index < 0) return;
  state.activeNodeIndex = index;
  if (state.openNodeIndex === null) state.openNodeIndex = index;
}

function finalizeActiveCot(){
  const event = state.cotEvents.find(item => item.key === state.activeCotKey);
  if (event){
    for (const line of event.lines || []) stopLineTimer(line, true);
    event.visible = event.lines.length;
  }
  stopCotReveal();
}

function normalizeCotLines(lines){
  return (Array.isArray(lines) ? lines : []).map(line => {
    if (typeof line === 'string'){
      return { text:line, delayMs:COT_REVEAL_MS, timer:null };
    }
    return {
      text:String(line?.text || ''),
      delayMs:Number.isFinite(line?.delayMs) ? Math.max(0, line.delayMs) : COT_REVEAL_MS,
      timer:line?.timer ? { ...line.timer } : null,
    };
  }).filter(line => line.text);
}

function scheduleCotReveal(){
  stopCotReveal();
  const event = state.cotEvents.find(item => item.key === state.activeCotKey);
  if (!event || event.visible >= event.lines.length) return;
  const nextLine = event.lines[event.visible];
  const delay = event.visible === 0
    ? 160
    : Math.max(0, nextLine?.delayMs ?? COT_REVEAL_MS);
  state.cotTimer = setTimeout(() => {
    state.cotTimer = null;
    if (state.activeCotKey !== event.key) return;
    const revealed = event.visible;
    event.visible = Math.min(event.lines.length, event.visible + 1);
    startLineTimer(event, revealed);
    renderTask();
    scheduleCotPublish();
    scheduleCotReveal();
  }, delay);
}

function ingestTaskCot(task){
  if (!task || task.type !== 'route') return;
  state.cotLastAt = Date.now();
  const definition = cotDefinitionForTask(task);
  if (!definition || definition.nodeIndex < 0) return;
  if (!Array.isArray(definition.lines) || !definition.lines.length){
    finalizeActiveCot();
    state.activeNodeIndex = definition.nodeIndex;
    if (state.openNodeIndex === null) state.openNodeIndex = definition.nodeIndex;
    return;
  }
  const point = Number(task.point) || 1;
  const loop = Number(task.loop) || 1;
  const key = [
    task.routeId || '',
    loop,
    task.back ? 'back' : 'forward',
    point,
    definition.note,
  ].join(':');

  if (state.cotEventKeys.has(key)){
    state.activeCotKey = key;
    state.activeNodeIndex = definition.nodeIndex;
    return;
  }

  finalizeActiveCot();
  const event = {
    key,
    routeId:task.routeId || '',
    point,
    loop,
    back:!!task.back,
    note:definition.back ? definition.note : taskNote(task) || definition.note,
    cruiseName:definition.back ? definition.cruiseName : taskPointName(task) || definition.cruiseName,
    nodeIndex:definition.nodeIndex,
    lines:normalizeCotLines(definition.lines),
    visible:0,
    at:Date.now(),
    fallback:!!definition.fallback,
  };
  // 固定巡检节点按“路线 + 圈 + 前进/返回 + 备注”归组。页面曾漏掉上一轮
  // task 的结束状态时，新一轮同一个节点可能再次生成 CoT；这里用最新一次
  // 替换旧事件，避免“大厦出口”里同时出现两个倒计时。
  const family = cotEventFamilyKey(event);
  for (const previous of state.cotEvents){
    if (previous.blockage) continue;
    if (cotEventFamilyKey(previous) !== family) continue;
    for (const line of previous.lines || []) stopLineTimer(line);
    state.cotEventKeys.delete(previous.key);
  }
  state.cotEvents = state.cotEvents.filter(item => {
    if (item.blockage) return true;
    return cotEventFamilyKey(item) !== family;
  });
  state.cotEvents.push(event);
  state.cotEventKeys.add(key);
  state.activeCotKey = key;
  const nodeChanged = state.activeNodeIndex !== definition.nodeIndex;
  state.activeNodeIndex = definition.nodeIndex;
  if (nodeChanged || state.openNodeIndex === null) state.openNodeIndex = definition.nodeIndex;
  scheduleCotReveal();
}

function appendFootwayBlockageCot(task, key){
  if (!task || state.cotEventKeys.has(key)) return;
  const definition = COT_BY_NOTE.get('position_footway');
  if (!definition || definition.nodeIndex < 0) return;
  const event = {
    key,
    routeId:task.routeId || '',
    point:Number(task.point) || 1,
    loop:Number(task.loop) || 1,
    back:false,
    note:definition.note,
    cruiseName:taskPointName(task) || definition.cruiseName,
    nodeIndex:definition.nodeIndex,
    lines:normalizeCotLines(['检测到阻塞（> 10s）']),
    blockage:true,
    visible:1,
    at:Date.now(),
    fallback:false,
  };
  state.cotEvents.push(event);
  state.cotEventKeys.add(key);
  state.openNodeIndex = event.nodeIndex;
  event.lines[0].timer = {
    type:'countup',
    from:10,
    intervalMs:1000,
    formatKey:'blockage',
    format:seconds => `检测到阻塞（> ${seconds}s）`,
  };
  startLineTimer(event, 0);
  renderTask();
}

function stopBlockageTimers(){
  for (const event of state.cotEvents){
    if (!event.blockage) continue;
    for (const line of event.lines || []) stopLineTimer(line);
  }
}

function updateFootwayBlockage(task = state.task, pose = state.pose, now = performance.now()){
  if (!footwayLowSpeedActive(task, taskNote(task), pose)) stopBlockageTimers();
  const key = footwayBlockage.observe(task, taskNote(task), pose, now);
  if (key) appendFootwayBlockageCot(task, `${key}:blocked`);
}

function inspectionNodeState(index, task, related){
  const incidents = nodeIncidents(index);
  const ongoing = incidents.find(item => item.ongoing);
  if (related && ongoing){
    return {
      state:'failed',
      label:isBlockingIncident(ongoing) ? '阻塞中' : '异常中',
    };
  }
  if (!related) return { state:'pending', label:'待执行' };
  if (task.status === 'succeeded') return { state:'done', label:'已完成' };
  const active = displayedInspectionNodeIndex(task);
  if (task.status === 'active'){
    if (active >= 0 && index < active) return { state:'done', label:'已完成' };
    if (index === active) return { state:'active', label:'进行中' };
    return { state:'pending', label:'待执行' };
  }
  if ((task.status === 'aborted' || task.status === 'canceled') && index === active){
    return { state:'failed', label:task.status === 'aborted' ? '失败' : '已中止' };
  }
  if ((active >= 0 && index < active) || nodeCots(index).length){
    return { state:'done', label:'已完成' };
  }
  return { state:'pending', label:'待执行' };
}

function formatIncidentTime(value){
  const date = new Date(Number(value));
  if (Number.isNaN(date.getTime())) return '--';
  return date.toLocaleString('zh-CN', {
    month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit',
    second:'2-digit', hour12:false,
  });
}

function incidentDuration(item){
  if (Number.isFinite(item.dur)) return `${item.dur.toFixed(1)} s`;
  if (item.ongoing) return '持续中';
  if (Number.isFinite(item.end) && Number.isFinite(item.ts)){
    return `${Math.max(0, (item.end - item.ts) / 1000).toFixed(1)} s`;
  }
  return '--';
}

function bagLabel(item){
  const bag = item.bag;
  if (!bag || !bag.state) return '无录包信息';
  const labels = {
    pending:'等待录包',
    saving:'保存中',
    saved:bag.name ? `已保存 · ${bag.name}` : '已保存',
    failed:'录包失败',
    skipped:'未录包',
  };
  return labels[bag.state] || bag.state;
}

function setChip(el, status, value){
  el.dataset.s = status;
  el.querySelector('b').textContent = value;
}

function renderConnection(){
  const robotStatus = state.online ? '在线' : '离线';
  setChip(els.robotChip, state.online ? 'ok' : 'off', robotStatus);
  setChip(els.linkChip, state.wsOpen ? 'ok' : 'off', state.wsOpen ? '已连接' : '重连中');
  els.presence.textContent = state.online ? '机器狗在线' : '机器狗离线';
  els.presence.style.color = state.online ? 'var(--green)' : 'var(--danger)';
}

function renderRouteOptions(){
  const previous = state.selectedRouteId;
  const activeRouteId = taskActive() ? state.task?.routeId : null;
  if (activeRouteId && state.routes.some(route => route.id === activeRouteId)){
    state.selectedRouteId = activeRouteId;
  } else if (!state.routes.some(route => route.id === previous)){
    state.selectedRouteId = state.routes.some(route => route.id === DEFAULT_ROUTE_ID)
      ? DEFAULT_ROUTE_ID
      : state.routes[0]?.id || '';
  }

  if (!state.routes.length){
    els.routeSelect.replaceChildren(new Option('机器狗尚未上报巡检任务', ''));
    els.routeSelect.disabled = true;
    renderSelectedRoute();
    return;
  }

  const options = state.routes.map(route => new Option(
    `${route.name} · ${route.points?.length || 0} 站 · ${route.map}`,
    route.id,
  ));
  els.routeSelect.replaceChildren(new Option('请选择巡检任务', ''), ...options);
  els.routeSelect.value = state.selectedRouteId;
  els.routeSelect.disabled = taskActive();
  renderSelectedRoute();
}

function renderSelectedRoute(){
  const route = selectedRoute();
  els.routeSelect.disabled = !state.routes.length || taskActive();
  els.startBtn.disabled = !route || !state.wsOpen || !state.online || taskActive() || state.preparing;
  els.startBtn.textContent = state.preparing ? '正在准备…' : '开始巡检';
  els.stopBtn.disabled = !state.wsOpen || !taskActive();
  const reason = preflightReason(route);
  els.startBtn.title = reason || '下发巡检任务并自动解锁底盘';
  renderTask();
}

function taskStateLabel(){
  const task = state.task || {};
  const route = selectedRoute();
  if (!route) return '选择巡检任务';
  if (!taskForSelectedRoute()) return taskActive() ? '其他任务执行中' : '等待下发任务';
  if (task.status === 'active') {
    if (task.phase === 'checking') return '正在准备巡检';
    const node = INSPECTION_NODES[displayedInspectionNodeIndex(task)];
    return node ? `正在巡检 · ${node.name}` : `正在前往第 ${task.point || 1} 站`;
  }
  if (task.status === 'succeeded') return '巡检已完成';
  if (task.status === 'canceled') return '巡检已中止';
  if (task.status === 'aborted') return `巡检失败：${task.message || '导航异常'}`;
  return '等待下发任务';
}

function visibleNodeCotLines(events){
  const rows = [];
  const seen = new Set();
  for (const event of events){
    const current = event.key === state.activeCotKey;
    const shown = current
      ? Math.max(0, Math.min(event.lines.length, event.visible || 0))
      : event.lines.length;
    event.lines.slice(0, shown).forEach((line, index) => {
      const text = line.text;
      if (seen.has(text)) return;
      seen.add(text);
      rows.push({ line:text, event, index, current });
    });
  }
  return rows;
}

function renderSteps(task, related){
  const activeDefinition = related ? cotDefinitionForTask(task) : null;
  const activeIndex = related ? displayedInspectionNodeIndex(task) : -1;
  const activeHasCot = (activeDefinition?.lines?.length || 0) > 0;
  els.steps.innerHTML = INSPECTION_NODES.map((node, index) => {
    const { state:stepState, label } = inspectionNodeState(index, task, related);
    const events = nodeCots(index);
    const incidents = nodeIncidents(index);
    const expanded = state.openNodeIndex === index;
    const rows = visibleNodeCotLines(events);
    const cotHtml = rows.length ? `
      <div class="node-cot">
        <span class="cot-label">CoT</span>
        <div class="cot-list">
          ${rows.map(row => {
            const kind = /阻塞|无通行空间|翻越|越障|推理其他/.test(row.line) ? 'block' : 'normal';
            return `<div class="cot-item" data-kind="${kind}"><i></i><span>${esc(row.line)}</span></div>`;
          }).join('')}
        </div>
      </div>
    ` : index === activeIndex && activeHasCot ? '<div class="node-empty">等待 CoT 输出…</div>' : '';
    const incidentHtml = incidents.length ? `
      <div class="node-incidents">
        ${incidents.slice(0, 6).map(item => `
          <button class="node-incident" type="button" data-step-incident="${esc(item.id)}" data-blocking="${isBlockingIncident(item)}">
            <i></i>
            <span>${isBlockingIncident(item) ? '阻塞 · ' : ''}${esc(item.label)}</span>
            <b>${esc(incidentDuration(item))}${item.ongoing ? ' · 持续中' : ''}</b>
          </button>
        `).join('')}
      </div>
    ` : '';
    const marker = stepState === 'done' ? '✓' : esc(node.code);
    return `<li class="inspection-node" data-s="${stepState}">
      <button class="inspection-node-head" type="button" data-open-node="${index}" aria-expanded="${expanded}">
        <span class="node-index">${marker}</span>
        <span class="node-name">${esc(node.name)}</span>
        <span class="node-state">${esc(label)}</span>
        <span class="chevron">${expanded ? '⌃' : '⌄'}</span>
      </button>
      <div class="node-body" ${expanded ? '' : 'hidden'}>
        ${cotHtml}
        ${incidentHtml}
      </div>
    </li>`;
  }).join('');

  if (state.openNodeIndex !== null && state.openNodeIndex !== state.lastOpenedNodeIndex){
    state.lastOpenedNodeIndex = state.openNodeIndex;
    requestAnimationFrame(() => {
      els.steps.querySelector(`[data-open-node="${state.openNodeIndex}"]`)
        ?.scrollIntoView({ block:'nearest', behavior:'smooth' });
    });
  }
}

function renderTask(){
  const task = state.task || {};
  const related = taskForSelectedRoute();
  const status = related ? task.status : 'idle';
  const progress = related && Number.isFinite(task.progress)
    ? Math.max(0, Math.min(1, task.progress))
    : status === 'succeeded' ? 1 : 0;

  els.taskPill.dataset.s = status === 'active' ? 'run'
    : status === 'succeeded' ? 'ok'
      : status === 'canceled' ? 'warn'
        : status === 'aborted' ? 'danger' : 'idle';
  els.taskPill.textContent = status === 'active' ? '巡检中'
    : status === 'succeeded' ? '已完成'
      : status === 'canceled' ? '已中止'
        : status === 'aborted' ? '失败' : '等待任务';
  els.taskStateText.textContent = state.preparing
    ? state.prepareText || '正在准备巡检…'
    : taskStateLabel();
  els.taskPercent.textContent = `${Math.round(progress * 100)}%`;
  els.taskProgress.style.width = `${progress * 100}%`;
  renderSteps(task, related);
}

function preflightReason(route){
  if (!state.wsOpen) return '巡检页面正在连接云端';
  if (!state.online) return '机器狗未连接到云端';
  if (state.machine?.estop) return '机器狗处于急停状态';
  if (state.machine?.busy) return '机器狗正在切换运行模式';
  if (!route) return '请选择一个巡检任务';
  if (taskActive()) return '已有巡检任务正在执行';
  return '';
}

function renderTelemetry(){
  const machine = state.machine || {};
  const pose = state.pose;
  const profiles = machine.profiles || {};
  const mode = profiles.navigation === 'run' ? '自主导航'
    : profiles.localization === 'run' ? '定位'
      : profiles.mapping === 'run' ? '建图' : '未启动';
  els.modeValue.textContent = mode;
  const speed = pose?.valid && Number.isFinite(pose.v)
    ? (Math.abs(pose.v) < 0.05 ? 0 : pose.v)
    : null;
  els.speedValue.textContent = speed == null ? '--' : `${speed.toFixed(2)} m/s`;
  els.batteryValue.textContent = Number.isFinite(machine.battery) ? `${Math.round(machine.battery)}%` : '--';
  els.cpuValue.textContent = Number.isFinite(machine.sys?.cpu) ? `${Math.round(machine.sys.cpu)}%` : '--';
}

function normalizeIncident(item, index = 0){
  if (!item || typeof item !== 'object') return null;
  const id = item.id ?? item.event_id ?? item.eventId;
  const ts = Number(item.ts ?? (item.started_at ? Date.parse(item.started_at) : NaN));
  if (!id || !Number.isFinite(ts)) return null;
  const task = item.task && typeof item.task === 'object' ? item.task : null;
  const pose = item.pose && typeof item.pose === 'object' ? item.pose : null;
  return {
    ...item,
    id:String(id),
    ts,
    end:item.end == null ? null : Number(item.end),
    dur:item.dur == null ? (item.duration_s == null ? null : Number(item.duration_s)) : Number(item.dur),
    ongoing:!!item.ongoing,
    robot:String(item.robot || state.robotId || ''),
    type:String(item.type || item.anomaly_type || 'unknown'),
    label:String(item.label || item.title || item.anomaly_type || `异常 ${index + 1}`),
    level:String(item.level || (item.severity === 'error' ? 'error' : 'warn')),
    msg:String(item.msg || item.detail || ''),
    map:item.map || null,
    pose,
    task,
    bag:item.bag && typeof item.bag === 'object' ? item.bag : null,
  };
}

function mergeIncidents(items, replace = false){
  const byId = new Map(replace ? [] : state.incidents.map(item => [item.id, item]));
  for (const [index, raw] of (Array.isArray(items) ? items : []).entries()){
    const item = normalizeIncident(raw, index);
    if (item) byId.set(item.id, item);
  }
  state.incidents = [...byId.values()]
    .sort((left, right) => right.ts - left.ts)
    .slice(0, 1000);
  renderExceptions();
  renderTask();
  if (state.playbackId){
    const current = state.incidents.find(item => item.id === state.playbackId)
      || state.history.items.get(state.playbackId);
    if (current) renderPlayback(current);
  }
}

function renderExceptions(){
  const items = state.incidents;
  els.exceptionCount.textContent = String(items.length);
  els.exceptionEmpty.hidden = items.length > 0;
  if (!items.length){
    els.exceptionRows.replaceChildren();
    return;
  }

  els.exceptionRows.innerHTML = items.map((item, index) => `
    <tr data-index="${index}" tabindex="0">
      <td class="mono">${esc(formatIncidentTime(item.ts))}</td>
      <td><span class="exception-type" data-level="${esc(item.level)}">● ${esc(item.label)}</span></td>
      <td>${esc(item.task?.pointName || '--')}</td>
      <td>
        <div class="exception-duration">
          <span class="mono">${esc(incidentDuration(item))}</span>
          <span class="bag-state" data-s="${esc(item.bag?.state || '')}">${esc(bagLabel(item))}</span>
        </div>
      </td>
      <td><button class="view-button" type="button" data-view-index="${index}">查看</button></td>
    </tr>
  `).join('');
}

function showIncident(item){
  if (!item) return;
  els.exceptionDetail.hidden = false;
  els.detailTitle.textContent = item.label;
  els.detailBody.textContent = item.msg || item.type;
  els.detailId.textContent = item.id;
  els.detailStatus.textContent = item.ongoing ? '持续中' : '已结束';
  els.detailPose.textContent = Number.isFinite(item.pose?.x) && Number.isFinite(item.pose?.y)
    ? `${Number(item.pose.x).toFixed(2)}, ${Number(item.pose.y).toFixed(2)}`
    : '--';
  els.detailMap.textContent = item.map || '--';
  els.detailBag.textContent = bagLabel(item);
  els.detailRobot.textContent = item.robot || '--';
}

function showException(index){
  showIncident(state.incidents[index]);
}

function renderHistory(){
  const items = [...state.history.items.values()]
    .sort((left, right) => right.ts - left.ts);
  els.historyEmpty.hidden = items.length > 0;
  els.historySummary.textContent = `${items.length} 条`;
  els.historyMore.disabled = state.history.loading || !state.history.hasMore;
  els.historyRows.innerHTML = items.map(item => `
    <tr data-history-id="${esc(item.id)}" tabindex="0">
      <td class="mono">${esc(formatIncidentTime(item.ts))}</td>
      <td class="mono">${esc(item.robot || '--')}</td>
      <td><span class="exception-type" data-level="${esc(item.level)}">● ${esc(item.label)}</span></td>
      <td>${esc(item.task?.pointName || '--')}</td>
      <td class="mono">${esc(incidentDuration(item))}</td>
      <td><span class="bag-state" data-s="${esc(item.bag?.state || '')}">${esc(bagLabel(item))}</span></td>
      <td><button class="view-button" type="button" data-history-view="${esc(item.id)}">查看</button></td>
    </tr>
  `).join('');
}

function setSnapshotPlaceholder(title, text, kind, retry = false){
  els.snapshotPlaceholderTitle.textContent = title;
  els.snapshotPlaceholderText.textContent = text;
  els.snapshotPlaceholder.dataset.s = kind;
  els.snapshotRetry.hidden = !retry;
  els.snapshotPlaceholder.hidden = false;
}

function releaseSnapshotObjectUrl(){
  if (!state.snapshotObjectUrl) return;
  URL.revokeObjectURL(state.snapshotObjectUrl);
  state.snapshotObjectUrl = '';
}

function roundRectPath(context, x, y, width, height, radius){
  context.beginPath();
  context.roundRect(x, y, width, height, radius);
}

function drawSnapshotMapPanel(context, width, height, item){
  const pose = item.pose;
  const map = state.map;
  const currentMap = state.machine?.mapCurrent;
  if (!map || !Number.isFinite(pose?.x) || !Number.isFinite(pose?.y)
      || (item.map && currentMap && item.map !== currentMap)) return false;

  const panelWidth = Math.round(Math.min(width * 0.46, 620));
  const panelHeight = Math.round(Math.min(height * 0.52, 360));
  const panelX = width - panelWidth - 24;
  const panelY = height - panelHeight - 24;
  const headerHeight = 40;
  const bodyX = panelX + 1;
  const bodyY = panelY + headerHeight;
  const bodyWidth = panelWidth - 2;
  const bodyHeight = panelHeight - headerHeight - 1;
  const centerX = bodyX + bodyWidth / 2;
  const centerY = bodyY + bodyHeight / 2;
  const scale = Math.min(bodyWidth / 20, bodyHeight / 14);
  const mapPoint = (x, y) => [
    centerX + (x - pose.x) * scale,
    centerY - (y - pose.y) * scale,
  ];

  context.save();
  context.shadowColor = 'rgba(0,0,0,.38)';
  context.shadowBlur = 24;
  context.shadowOffsetY = 8;
  context.fillStyle = '#fff';
  roundRectPath(context, panelX, panelY, panelWidth, panelHeight, 18);
  context.fill();
  context.restore();

  context.save();
  roundRectPath(context, panelX, panelY, panelWidth, panelHeight, 18);
  context.clip();
  context.fillStyle = '#fff';
  context.fillRect(panelX, panelY, panelWidth, headerHeight);
  context.fillStyle = '#e9e9eb';
  context.fillRect(bodyX, bodyY, bodyWidth, bodyHeight);

  context.save();
  context.beginPath();
  context.rect(bodyX, bodyY, bodyWidth, bodyHeight);
  context.clip();
  context.strokeStyle = '#dedee3';
  context.lineWidth = 1;
  const grid = Math.max(18, Math.round(bodyWidth / 25));
  for (let x = bodyX; x <= bodyX + bodyWidth; x += grid){
    context.beginPath();
    context.moveTo(x, bodyY);
    context.lineTo(x, bodyY + bodyHeight);
    context.stroke();
  }
  for (let y = bodyY; y <= bodyY + bodyHeight; y += grid){
    context.beginPath();
    context.moveTo(bodyX, y);
    context.lineTo(bodyX + bodyWidth, y);
    context.stroke();
  }

  context.save();
  context.globalAlpha = .94;
  if (Math.abs(map.yaw || 0) < 1e-5){
    const [ox, oy] = mapPoint(map.ox, map.oy + map.h);
    context.drawImage(map.img, ox, oy, map.w * scale, map.h * scale);
  } else {
    const [ox, oy] = mapPoint(map.ox, map.oy);
    context.translate(ox, oy);
    context.rotate(-map.yaw);
    context.drawImage(map.img, 0, -map.h * scale, map.w * scale, map.h * scale);
  }
  context.restore();

  const route = state.routes.find(entry => entry.id === item.task?.routeId);
  if (route?.points?.length > 1){
    context.save();
    context.strokeStyle = '#1fa9bb';
    context.lineWidth = 2.2;
    context.lineJoin = 'round';
    context.lineCap = 'round';
    context.beginPath();
    route.points.forEach((point, index) => {
      const [x, y] = mapPoint(point.x, point.y);
      if (index) context.lineTo(x, y);
      else context.moveTo(x, y);
    });
    context.stroke();
    context.restore();
  }

  const [poseX, poseY] = mapPoint(pose.x, pose.y);
  context.save();
  context.translate(poseX, poseY);
  context.rotate(-pose.th);
  context.fillStyle = '#f3a51f';
  context.strokeStyle = '#fff';
  context.lineWidth = 2;
  context.beginPath();
  context.arc(0, 0, 8, 0, Math.PI * 2);
  context.fill();
  context.stroke();
  context.fillStyle = '#f3a51f';
  context.beginPath();
  context.moveTo(20, 0);
  context.lineTo(8, -5);
  context.lineTo(8, 5);
  context.closePath();
  context.fill();
  context.restore();

  const legendWidth = Math.max(120, Math.min(194, bodyWidth - 20));
  const legendHeight = 24;
  const legendX = bodyX + bodyWidth - legendWidth - 10;
  const legendY = bodyY + bodyHeight - legendHeight - 10;
  context.fillStyle = 'rgba(255,255,255,.94)';
  roundRectPath(context, legendX, legendY, legendWidth, legendHeight, 6);
  context.fill();
  context.font = '10px Inter, sans-serif';
  context.textAlign = 'left';
  context.textBaseline = 'middle';
  context.strokeStyle = '#27272a';
  context.lineWidth = 2;
  context.beginPath();
  context.moveTo(legendX + 8, legendY + 12);
  context.lineTo(legendX + 21, legendY + 12);
  context.stroke();
  context.fillStyle = '#65656f';
  context.fillText('规划路线', legendX + 26, legendY + 12);
  context.strokeStyle = '#168354';
  context.beginPath();
  context.moveTo(legendX + 76, legendY + 12);
  context.lineTo(legendX + 89, legendY + 12);
  context.stroke();
  context.fillStyle = '#65656f';
  context.fillText('已完成', legendX + 94, legendY + 12);
  context.fillStyle = '#f3a51f';
  context.beginPath();
  context.arc(legendX + 141, legendY + 12, 3.5, 0, Math.PI * 2);
  context.fill();
  context.fillStyle = '#65656f';
  context.fillText('异常位置', legendX + 149, legendY + 12);
  context.restore();

  const controls = ['−', '+', '⌄'];
  controls.forEach((label, index) => {
    const size = 27;
    const x = panelX + panelWidth - (controls.length - index) * (size + 6) - 7;
    const y = panelY + 7;
    context.fillStyle = '#f8f8fa';
    context.strokeStyle = '#e2e2e7';
    context.lineWidth = 1;
    roundRectPath(context, x, y, size, size, 6);
    context.fill();
    context.stroke();
    context.fillStyle = '#666671';
    context.font = '14px Inter, sans-serif';
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillText(label, x + size / 2, y + size / 2 + 1);
  });
  context.textAlign = 'left';
  context.restore();
  return true;
}

function composeIncidentSnapshot(item, image){
  const canvas = document.createElement('canvas');
  canvas.width = image.naturalWidth || 1280;
  canvas.height = image.naturalHeight || 720;
  const context = canvas.getContext('2d');
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  drawSnapshotMapPanel(context, canvas.width, canvas.height, item);
  return new Promise((resolve, reject) => {
    canvas.toBlob(blob => {
      if (blob) resolve(blob);
      else reject(new Error('snapshot compose failed'));
    }, 'image/jpeg', 0.92);
  });
}

function loadIncidentSnapshot(url, item){
  state.snapshotToken += 1;
  const token = state.snapshotToken;
  const request = new URL(url, location.href);
  request.searchParams.set('_snapshot', String(Date.now()));
  releaseSnapshotObjectUrl();
  els.incidentSnapshot.hidden = true;
  delete els.incidentSnapshot.dataset.composite;
  els.incidentSnapshot.dataset.source = url;
  els.incidentSnapshot.dataset.request = request.href;
  setSnapshotPlaceholder('正在加载异常截图', '正在合成 RTC 画面与异常位置地图…', 'loading');

  const image = new Image();
  image.decoding = 'async';
  image.onload = async () => {
    if (token !== state.snapshotToken) return;
    try {
      const blob = await composeIncidentSnapshot(item, image);
      if (token !== state.snapshotToken) return;
      const objectUrl = URL.createObjectURL(blob);
      state.snapshotObjectUrl = objectUrl;
      els.incidentSnapshot.dataset.request = objectUrl;
      els.incidentSnapshot.dataset.composite = '1';
      els.incidentSnapshot.src = objectUrl;
    } catch {
      if (token !== state.snapshotToken) return;
      els.incidentSnapshot.dataset.request = request.href;
      els.incidentSnapshot.dataset.composite = '0';
      els.incidentSnapshot.src = request.href;
    }
  };
  image.onerror = () => {
    if (token !== state.snapshotToken) return;
    els.incidentSnapshot.hidden = true;
    setSnapshotPlaceholder('截图加载失败', '请检查快照地址或网络连接后重试', 'error', true);
  };
  image.src = request.href;
}

function renderPlayback(item){
  if (!item) return;
  state.playbackId = item.id;
  els.playbackTitle.textContent = `异常数据 · ${formatIncidentTime(item.ts)} · ${item.label}`;
  els.playbackType.textContent = item.label;
  els.playbackTime.textContent = formatIncidentTime(item.ts);
  els.playbackPoint.textContent = item.task?.pointName || '无任务节点';
  els.playbackBag.textContent = bagLabel(item);
  const url = item.snapshot?.local_url || item.snapshot?.url;
  if (url){
    loadIncidentSnapshot(url, item);
  } else {
    state.snapshotToken += 1;
    releaseSnapshotObjectUrl();
    els.incidentSnapshot.removeAttribute('src');
    delete els.incidentSnapshot.dataset.source;
    delete els.incidentSnapshot.dataset.request;
    delete els.incidentSnapshot.dataset.composite;
    els.incidentSnapshot.hidden = true;
    setSnapshotPlaceholder('该异常暂无截图', '该异常没有关联快照数据', 'empty');
  }
}

function openIncidentPlayback(item){
  if (!item) return;
  els.incidentPlayback.hidden = false;
  renderPlayback(item);
}

function closeIncidentPlayback(){
  state.playbackId = null;
  state.snapshotToken += 1;
  releaseSnapshotObjectUrl();
  els.incidentPlayback.hidden = true;
  els.incidentSnapshot.removeAttribute('src');
  delete els.incidentSnapshot.dataset.source;
  delete els.incidentSnapshot.dataset.request;
  delete els.incidentSnapshot.dataset.composite;
}

async function loadHistory(reset = false){
  if (state.history.loading || (!reset && !state.history.hasMore)) return;
  state.history.loading = true;
  els.historyMore.disabled = true;
  if (reset){
    state.history.items.clear();
    state.history.before = null;
    state.history.hasMore = true;
    renderHistory();
  }
  try {
    const query = new URLSearchParams({ robot_id:state.robotId, limit:'50' });
    if (state.history.before != null) query.set('before', String(state.history.before));
    const response = await fetch(`/quadruped/api/incidents?${query}`, { cache:'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const rawItems = Array.isArray(payload.items) ? payload.items : [];
    rawItems.forEach((raw, index) => {
      const item = normalizeIncident(raw, index);
      if (item) state.history.items.set(item.id, item);
    });
    const oldest = rawItems
      .map(item => Number(item.ts))
      .filter(Number.isFinite)
      .sort((left, right) => left - right)[0];
    if (Number.isFinite(oldest)) state.history.before = oldest;
    state.history.hasMore = rawItems.length >= 50;
  } catch (error){
    toast(`异常历史加载失败：${error?.message || error}`, 'err');
    state.history.hasMore = false;
  } finally {
    state.history.loading = false;
    renderHistory();
  }
}

function openHistory(){
  els.incidentHistory.hidden = false;
  loadHistory(true);
}

function closeHistory(){
  els.incidentHistory.hidden = true;
}

function updateRtc(){
  const shouldStart = state.online && state.videoUploadEnabled !== false;
  if (shouldStart && !state.rtcStarted){
    state.rtcStarted = true;
    rtc.start();
  } else if (!shouldStart && state.rtcStarted){
    state.rtcStarted = false;
    rtc.stop();
    els.rtcMessage.hidden = false;
    els.rtcMessage.textContent = state.online ? '机器狗视频上传已关闭' : '等待机器狗重新连接云端…';
    els.rtcBadge.dataset.s = 'warn';
  }
}

function render(){
  renderConnection();
  renderRouteOptions();
  renderTelemetry();
  renderExceptions();
  updateRtc();
}

function decodeFrame(buffer){
  const view = new DataView(buffer);
  const kind = view.getUint8(0);
  const metadataLength = view.getUint32(1, true);
  const metadata = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 5, metadataLength)));
  return { kind, metadata, payload:new Uint8Array(buffer, 5 + metadataLength) };
}

async function inflate(bytes){
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function onMapFrame(metadata, payload){
  state.mapLoading = true;
  try {
    const grid = await inflate(payload);
    state.map = {
      img: gridToCanvas(grid, metadata.w, metadata.h),
      w: metadata.w * metadata.res,
      h: metadata.h * metadata.res,
      ox: metadata.ox,
      oy: metadata.oy,
      yaw: metadata.oyaw || 0,
      res: metadata.res,
      name: metadata.name || metadata.map || 'map',
    };
    els.mapEmpty.hidden = true;
    fitView();
    drawMap();
  } catch (error){
    toast(`地图解析失败：${error?.message || error}`, 'err');
  } finally {
    state.mapLoading = false;
  }
}

function onJson(message){
  switch (message.t){
    case 'pong':
      if (Number.isFinite(message.ts)) setChip(els.linkChip, 'ok', `${Math.max(0, performance.now() - message.ts).toFixed(0)} ms`);
      break;
    case 'robot_status':
      if (message.robot_id && message.robot_id !== state.robotId) break;
      state.online = !!message.online;
      if (typeof message.video_upload_enabled === 'boolean') state.videoUploadEnabled = message.video_upload_enabled;
      if (!state.online){
        state.task = { type:'idle' };
        resetCotOutput();
      }
      renderConnection();
      updateRtc();
      renderSelectedRoute();
      break;
    case 'camera_upload':
      if (message.robot_id && message.robot_id !== state.robotId) break;
      state.videoUploadEnabled = !!message.enabled;
      updateRtc();
      break;
    case 'routes':
      state.routes = Array.isArray(message.items) ? message.items.filter(route => route?.id) : [];
      syncInspectionNode();
      renderRouteOptions();
      drawMap();
      break;
    case 'task': {
      const previous = state.task || { type:'idle' };
      const next = message.task || { type:'idle' };
      const elapsed = Number(next.elapsed);
      const restarted = inspectionTaskRestarted(previous, next);
      if (
        next.status === 'active'
        && next.routeId
        && (previous.status !== 'active' || previous.routeId !== next.routeId || restarted)
      ){
        beginInspectionRun(
          next,
          Number.isFinite(elapsed) ? Date.now() - elapsed * 1000 : Date.now(),
        );
      }
      state.task = next;
      if (next.status === 'active' && next.type === 'route'){
        ingestTaskCot(next);
      } else {
        stopBlockageTimers();
        finalizeActiveCot();
        if (next.type === 'idle') resetCotOutput();
      }
      if (next.status === 'active' && taskNote(next) !== 'position_footway') stopBlockageTimers();
      syncInspectionNode();
      if (taskActive() && state.task.routeId) {
        state.selectedRouteId = state.task.routeId;
        els.routeSelect.value = state.selectedRouteId;
      }
      renderTask();
      renderSelectedRoute();
      drawMap();
      break;
    }
    case 'pose':
      state.pose = message;
      state.poseAt = performance.now();
      updateFootwayBlockage(state.task, message, state.poseAt);
      renderTelemetry();
      break;
    case 'state':
      state.machine = message;
      if (taskActive()) els.routeSelect.disabled = true;
      renderTelemetry();
      renderTask();
      break;
    case 'incidents':
      mergeIncidents(message.items, true);
      break;
    case 'incident':
      mergeIncidents(message.item ? [message.item] : []);
      break;
    case 'inspection_cot':
      restoreCotSnapshot(message.snapshot);
      break;
    case 'inspection_exceptions': {
      const items = Array.isArray(message.items) ? message.items : message.item ? [message.item] : [];
      mergeIncidents(items, true);
      break;
    }
    case 'ack': {
      const pending = state.pending.get(message.id);
      if (!pending) break;
      clearTimeout(pending.timer);
      state.pending.delete(message.id);
      pending.resolve({ ok: !!message.ok, msg: message.msg || '', data: message.data });
      break;
    }
    case 'toast':
      if (message.msg) toast(message.msg, message.level === 'err' ? 'err' : message.level === 'warn' ? 'warn' : 'ok');
      break;
  }
}

function samplePose(pose){
  const point = [pose.x, pose.y];
  if (!state.lastTrailPoint || Math.hypot(point[0] - state.lastTrailPoint[0], point[1] - state.lastTrailPoint[1]) >= TRAIL_MIN_MOVE){
    state.trail.push(point);
    if (state.trail.length > 900) state.trail.shift();
    state.lastTrailPoint = point;
  }
}

function send(command, timeout = 10000){
  return new Promise(resolve => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN){
      resolve({ ok:false, msg:'巡检页面尚未连接云端' });
      return;
    }
    const id = ++state.commandId;
    const timer = setTimeout(() => {
      state.pending.delete(id);
      resolve({ ok:false, msg:'云端没有响应（超时）' });
    }, timeout);
    state.pending.set(id, { resolve, timer });
    state.ws.send(JSON.stringify({ t:'cmd', id, ...command }));
  });
}

function sleep(ms){
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitForCondition(check, timeout, label){
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline){
    if (!state.wsOpen) throw new Error('与云端的连接已断开');
    if (check()) return;
    await sleep(300);
  }
  throw new Error(`${label}超时`);
}

function showPrepare(message){
  state.prepareText = message;
  if (state.preparing) els.taskStateText.textContent = message;
}

function localizedForMap(route){
  const auto = state.machine?.autoLoc;
  return auto?.state === 'ok'
    && auto?.map === route.map
    && state.pose?.valid
    && state.pose?.frame === 'map';
}

async function ensureLocalized(route){
  if (localizedForMap(route)) return;
  const auto = () => state.machine?.autoLoc || {};
  if (auto().state === 'run'){
    showPrepare('正在等待导航自动定位…');
    await waitForCondition(
      () => localizedForMap(route) || auto().state === 'fail',
      90000,
      '自动定位',
    );
  }
  if (localizedForMap(route)) return;

  const seq = Number(auto().seq ?? -1);
  showPrepare('正在执行自动定位…');
  const reply = await send({
    op:'auto_localize',
    ...inspectionLocalizationHints(route),
  }, 100000);
  if (!reply.ok && !/正在进行/.test(reply.msg || '')){
    throw new Error(reply.msg || '自动定位失败');
  }
  await waitForCondition(
    () => localizedForMap(route)
      || (auto().state === 'fail' && Number(auto().seq ?? -1) > seq),
    70000,
    '自动定位',
  );
  if (!localizedForMap(route)){
    throw new Error(auto().msg || reply.msg || '自动定位失败');
  }
}

async function ensureRouteReady(route){
  showPrepare('正在确认定位、里程计和地图状态…');
  await waitForCondition(
    () => state.pose?.valid
      && state.pose?.frame === 'map'
      && state.machine?.odomOk === true
      && state.machine?.mapCurrent === route.map,
    15000,
    '定位或里程计',
  );
  // 刚定位后 AMCL 需要约 1 秒才能算出新匹配率。和主控制台一样最多等 4 秒，
  // 避免拿定位前的旧匹配率立刻出发，也避免匹配率暂时偏低时永远不出发。
  const deadline = Date.now() + 4000;
  while (Date.now() < deadline){
    const match = Number(state.machine?.loc?.match);
    if (!Number.isFinite(match) || match >= 60) return;
    await sleep(300);
  }
}

async function prepareInspection(route){
  const machine = state.machine || {};
  const profiles = machine.profiles || {};
  const ctrl = machine.ctrl || {};
  const needsLocalization = (
    profiles.navigation !== 'run'
    || machine.mapCurrent !== route.map
    || !state.pose?.valid
    || state.pose?.frame !== 'map'
  );

  if (profiles.mapping === 'run'){
    const ok = window.confirm(
      `当前正在建图。开始巡检将停止建图，并切换到任务地图 ${route.map}。确认继续？`,
    );
    if (!ok) return false;
  }

  if (ctrl.on && ctrl.state !== 'Velocity'){
    showPrepare('正在让机器狗站起并接管运控…');
    const reply = await send({ op:'stand_up' }, 15000);
    if (!reply.ok) throw new Error(reply.msg || '机器狗站起失败');
    await waitForCondition(
      () => state.machine?.ctrl?.state === 'Velocity',
      20000,
      '机器狗站起',
    );
  }

  if (profiles.navigation !== 'run'){
    showPrepare(`正在启动导航并使用地图 ${route.map}…`);
    const reply = await send({ op:'set_mode', mode:'navigation', map:route.map }, 12000);
    if (!reply.ok) throw new Error(reply.msg || '启动导航失败');
  } else if (machine.mapCurrent !== route.map){
    showPrepare(`正在切换任务地图 ${route.map}…`);
    const reply = await send({ op:'load_map', name:route.map }, 12000);
    if (!reply.ok) throw new Error(reply.msg || '切换任务地图失败');
  }

  showPrepare('正在等待导航和任务地图就绪…');
  await waitForCondition(
    () => state.machine?.profiles?.navigation === 'run'
      && state.machine?.mapCurrent === route.map,
    120000,
    '启动导航',
  );

  if (needsLocalization) await ensureLocalized(route);
  await ensureRouteReady(route);

  showPrepare('导航、地图和定位已就绪，正在下发巡检任务…');
  return true;
}

async function startInspection(){
  const route = selectedRoute();
  const reason = preflightReason(route);
  if (reason){
    toast(reason, 'warn');
    return;
  }
  state.preparing = true;
  renderSelectedRoute();
  try {
    const ready = await prepareInspection(route);
    if (!ready) return;
    const opts = route.opts || {};
    const command = {
      op:'route_start',
      route_id:route.id,
      round_trip:!!opts.loop,
      loops:Number.isInteger(opts.loops) ? opts.loops : 1,
      arm:true,
      ...(Number.isFinite(opts.vmax) ? { vmax:opts.vmax } : {}),
      ...(Number.isFinite(opts.wmax) ? { wmax:opts.wmax } : {}),
    };
    let reply = await send(command);
    if (!reply.ok && /定位|里程计|地图定位/.test(reply.msg || '')){
      showPrepare('定位状态不稳定，正在重新确认…');
      await ensureLocalized(route);
      await ensureRouteReady(route);
      reply = await send(command);
    }
    if (reply.ok){
      if (!(state.task?.status === 'active'
        && state.task?.routeId === route.id
        && state.inspectionRunStart)){
        beginInspectionRun({ routeId:route.id }, Date.now());
      }
      toast(reply.msg || '巡检任务已下发');
      state.selectedRouteId = route.id;
    } else {
      toast(reply.msg || '巡检任务下发失败', 'err');
    }
  } catch (error){
    toast(error?.message || String(error), 'err');
  } finally {
    state.preparing = false;
    state.prepareText = '';
    renderSelectedRoute();
  }
}

async function stopInspection(){
  if (!taskActive()) return;
  if (!window.confirm('确认中止当前巡检任务？机器狗将停止导航。')) return;
  els.stopBtn.disabled = true;
  const reply = await send({ op:'cancel' });
  els.stopBtn.disabled = false;
  toast(reply.msg || (reply.ok ? '巡检任务已中止' : '中止任务失败'), reply.ok ? 'warn' : 'err');
}

async function resolveRobotId(){
  const queryId = new URLSearchParams(location.search).get('robot_id');
  if (queryId) return queryId;
  try {
    const response = await fetch('/quadruped/api/robots', { cache:'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const robots = Array.isArray(data.robots) ? data.robots : [];
    return (robots.find(robot => robot.online) || robots[0])?.robot_id || '';
  } catch {
    return '';
  }
}

function setRobotQuery(robotId){
  const url = new URL(location.href);
  url.searchParams.set('robot_id', robotId);
  history.replaceState(null, '', url);
  els.consoleLink.href = `/quadruped/?robot_id=${encodeURIComponent(robotId)}`;
  els.fleetLink.href = `/quadruped/fleet/?robot_id=${encodeURIComponent(robotId)}`;
}

function connect(){
  clearTimeout(state.reconnectTimer);
  if (!state.robotId){
    setChip(els.robotChip, 'off', '未选择');
    setChip(els.linkChip, 'off', '无设备');
    els.taskStateText.textContent = '设备列表中还没有可用的机器狗';
    return;
  }
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${scheme}://${location.host}/quadruped/ws?robot_id=${encodeURIComponent(state.robotId)}`;
  const ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';
  state.ws = ws;
  ws.onopen = () => {
    state.wsOpen = true;
    sendPing();
    ws.send(JSON.stringify({ t:'sub', layers:[] }));
    renderConnection();
    renderRouteOptions();
  };
  ws.onmessage = event => {
    if (typeof event.data === 'string'){
      try { onJson(JSON.parse(event.data)); } catch {}
    } else {
      const { kind, metadata, payload } = decodeFrame(event.data);
      if (kind === T_MAP) onMapFrame(metadata, payload);
    }
  };
  ws.onerror = () => {};
  ws.onclose = () => {
    state.wsOpen = false;
    state.online = false;
    for (const pending of state.pending.values()){
      clearTimeout(pending.timer);
      pending.resolve({ ok:false, msg:'与云端的连接已断开' });
    }
    state.pending.clear();
    renderConnection();
    renderSelectedRoute();
    updateRtc();
    state.reconnectTimer = setTimeout(connect, 1000);
  };
}

function sendPing(){
  if (state.ws?.readyState === WebSocket.OPEN){
    state.ws.send(JSON.stringify({ t:'ping', ts:performance.now() }));
  }
}

function setupHeartbeat(){
  const workerUrl = URL.createObjectURL(new Blob(
    [`setInterval(() => postMessage(0), ${HEARTBEAT_MS})`],
    { type:'text/javascript' },
  ));
  state.heartbeat = new Worker(workerUrl);
  state.heartbeat.onmessage = sendPing;
}

const canvas = els.mapCanvas;
const ctx = canvas.getContext('2d');
const view = { scale:40, cx:0, cy:0, width:0, height:0 };

function resizeCanvas(){
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  view.width = rect.width;
  view.height = rect.height;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  drawMap();
}

function rotatePoint(x, y, angle){
  const cosine = Math.cos(angle), sine = Math.sin(angle);
  return [x * cosine - y * sine, x * sine + y * cosine];
}

function fitView(){
  if ((!state.map && !selectedRoute()?.points?.length) || view.width <= 0 || view.height <= 0) return;
  const corners = [];
  if (state.map){
    for (const [x, y] of [[0, 0], [state.map.w, 0], [0, state.map.h], [state.map.w, state.map.h]]){
      const [rx, ry] = rotatePoint(x, y, state.map.yaw || 0);
      corners.push([rx + state.map.ox, ry + state.map.oy]);
    }
  }
  for (const point of selectedRoute()?.points || []) corners.push([point.x, point.y]);
  if (!corners.length) return;
  const xs = corners.map(point => point[0]);
  const ys = corners.map(point => point[1]);
  const left = Math.min(...xs), right = Math.max(...xs);
  const bottom = Math.min(...ys), top = Math.max(...ys);
  const width = Math.max(1, right - left), height = Math.max(1, top - bottom);
  view.scale = Math.max(4, Math.min((view.width - 64) / width, (view.height - 90) / height));
  view.cx = (left + right) / 2;
  view.cy = (bottom + top) / 2 - 8 / view.scale;
}

function zoomMap(factor){
  if (!state.map) return;
  view.scale = Math.max(4, Math.min(240, view.scale * factor));
  drawMap();
}

function sx(x){
  return view.width / 2 + (x - view.cx) * view.scale;
}

function sy(y){
  return view.height / 2 - (y - view.cy) * view.scale;
}

function drawGrid(){
  const map = state.map;
  if (!map) return;
  ctx.save();
  ctx.globalAlpha = 0.96;
  if (Math.abs(map.yaw) < 1e-5){
    ctx.drawImage(map.img, sx(map.ox), sy(map.oy + map.h), map.w * view.scale, map.h * view.scale);
  } else {
    ctx.translate(sx(map.ox), sy(map.oy));
    ctx.rotate(-map.yaw);
    ctx.drawImage(map.img, 0, -map.h * view.scale, map.w * view.scale, map.h * view.scale);
  }
  ctx.restore();
}

function drawPolyline(points, color, width, dash = []){
  if (points.length < 2) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.setLineDash(dash);
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = Array.isArray(point) ? point[0] : point.x;
    const y = Array.isArray(point) ? point[1] : point.y;
    if (index) ctx.lineTo(sx(x), sy(y));
    else ctx.moveTo(sx(x), sy(y));
  });
  ctx.stroke();
  ctx.restore();
}

function drawRoute(){
  const route = selectedRoute();
  const points = route?.points || [];
  if (points.length < 2) return;
  const full = route.opts?.loop ? [...points, points[0]] : points;
  drawPolyline(full, 'rgba(56,225,220,.7)', 2, [7, 5]);

  const task = state.task || {};
  if (taskForSelectedRoute() && task.status !== 'idle'){
    let completed = 0;
    if (task.status === 'succeeded') completed = full.length - 1;
    else if (task.status === 'active'){
      completed = Math.max(0, Math.min(full.length - 1, (task.point || 1) - 1));
      if (task.back) completed = Math.max(0, full.length - 2);
    }
    if (completed > 0) drawPolyline(full.slice(0, completed + 1), 'rgba(53,212,138,.95)', 3.5);
  }

}

function drawRobot(){
  const pose = state.pose;
  if (!pose?.valid) return;
  const x = sx(pose.x), y = sy(pose.y);
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(-pose.th);
  const length = Math.max(22, 0.72 * view.scale);
  const width = Math.max(14, 0.46 * view.scale);
  ctx.fillStyle = 'rgba(8,16,25,.96)';
  ctx.strokeStyle = '#f3b13f';
  ctx.lineWidth = 1.8;
  ctx.shadowColor = 'rgba(243,177,63,.6)';
  ctx.shadowBlur = 12;
  ctx.beginPath();
  ctx.roundRect(-length / 2, -width / 2, length, width, 5);
  ctx.fill();
  ctx.stroke();
  ctx.shadowBlur = 0;
  ctx.fillStyle = '#f3b13f';
  ctx.beginPath();
  ctx.moveTo(length / 2 - 1, 0);
  ctx.lineTo(length * 0.08, -width * 0.34);
  ctx.lineTo(length * 0.08, width * 0.34);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

function drawMap(){
  if (els.mapOverlay.hidden) return;
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, view.width, view.height);
  const gradient = ctx.createRadialGradient(view.width * 0.5, view.height * 0.42, 0, view.width * 0.5, view.height * 0.42, view.width * 0.7);
  gradient.addColorStop(0, 'rgba(18,38,48,.55)');
  gradient.addColorStop(1, 'rgba(3,7,11,0)');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, view.width, view.height);

  if (!state.map){
    els.mapEmpty.hidden = false;
    return;
  }
  els.mapEmpty.hidden = true;
  drawGrid();
  if (state.trail.length > 1) drawPolyline(state.trail, 'rgba(178,140,255,.72)', 2);
  drawRoute();
  drawRobot();
}

function renderRealtime(){
  if (state.pose?.valid && state.pose.frame === 'map') samplePose(state.pose);
  renderTelemetry();
  renderTask();
  drawMap();
}

function setMapCollapsed(collapsed){
  els.mapOverlay.hidden = collapsed;
  els.expandMap.hidden = !collapsed;
  if (!collapsed){
    requestAnimationFrame(() => {
      resizeCanvas();
      drawMap();
    });
  }
}

async function init(){
  state.robotId = await resolveRobotId();
  if (state.robotId) setRobotQuery(state.robotId);
  render();
  connect();
  setupHeartbeat();
  setInterval(renderRealtime, POSE_RENDER_MS);
  setInterval(() => { els.clock.textContent = new Date().toLocaleTimeString('zh-CN', { hour12:false }); }, 250);
}

els.routeSelect.addEventListener('change', () => {
  state.selectedRouteId = els.routeSelect.value;
  state.trail = [];
  state.lastTrailPoint = null;
  if (!taskActive()) resetCotOutput();
  renderSelectedRoute();
  fitView();
  drawMap();
});
els.startBtn.addEventListener('click', startInspection);
els.stopBtn.addEventListener('click', stopInspection);
els.zoomOutMap.addEventListener('click', () => zoomMap(0.8));
els.zoomInMap.addEventListener('click', () => zoomMap(1.25));
els.collapseMap.addEventListener('click', () => setMapCollapsed(true));
els.expandMap.addEventListener('click', () => setMapCollapsed(false));
els.steps.addEventListener('click', event => {
  const toggle = event.target.closest('[data-open-node]');
  if (toggle){
    const index = Number(toggle.dataset.openNode);
    if (state.openNodeIndex === index){
      stopCotReveal();
      state.openNodeIndex = null;
      renderTask();
    } else {
      state.openNodeIndex = index;
      renderTask();
    }
    return;
  }
  const button = event.target.closest('[data-step-incident]');
  if (!button) return;
  const incident = state.incidents.find(item => item.id === button.dataset.stepIncident);
  if (incident) openIncidentPlayback(incident);
});
els.exceptionMore.addEventListener('click', openHistory);
els.exceptionRows.addEventListener('click', event => {
  const view = event.target.closest('[data-view-index]');
  if (view){
    openIncidentPlayback(state.incidents[Number(view.dataset.viewIndex)]);
    return;
  }
  const row = event.target.closest('tr[data-index]');
  if (row) showException(Number(row.dataset.index));
});
els.exceptionRows.addEventListener('keydown', event => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const row = event.target.closest('tr[data-index]');
  if (row){
    event.preventDefault();
    showException(Number(row.dataset.index));
  }
});
els.historyRows.addEventListener('click', event => {
  const view = event.target.closest('[data-history-view]');
  if (view){
    openIncidentPlayback(state.history.items.get(view.dataset.historyView));
    closeHistory();
    return;
  }
  const row = event.target.closest('tr[data-history-id]');
  if (!row) return;
  showIncident(state.history.items.get(row.dataset.historyId));
  closeHistory();
});
els.historyRows.addEventListener('keydown', event => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const row = event.target.closest('tr[data-history-id]');
  if (!row) return;
  event.preventDefault();
  showIncident(state.history.items.get(row.dataset.historyId));
  closeHistory();
});
els.historyMore.addEventListener('click', () => loadHistory(false));
els.historyClose.addEventListener('click', closeHistory);
els.incidentHistory.addEventListener('click', event => {
  if (event.target === els.incidentHistory) closeHistory();
});
els.detailClose.addEventListener('click', () => { els.exceptionDetail.hidden = true; });
els.playbackClose.addEventListener('click', closeIncidentPlayback);
els.incidentPlayback.addEventListener('click', event => {
  if (event.target === els.incidentPlayback) closeIncidentPlayback();
});
els.incidentSnapshot.addEventListener('load', () => {
  if (!els.incidentSnapshot.dataset.request
      || els.incidentSnapshot.currentSrc !== els.incidentSnapshot.dataset.request) return;
  els.snapshotPlaceholder.hidden = true;
  els.incidentSnapshot.hidden = false;
});
els.incidentSnapshot.addEventListener('error', () => {
  if (!els.incidentSnapshot.dataset.request
      || els.incidentSnapshot.currentSrc !== els.incidentSnapshot.dataset.request) return;
  els.incidentSnapshot.hidden = true;
  setSnapshotPlaceholder('截图加载失败', '请检查快照地址或网络连接后重试', 'error', true);
});
els.snapshotRetry.addEventListener('click', () => {
  const source = els.incidentSnapshot.dataset.source;
  const item = state.incidents.find(entry => entry.id === state.playbackId)
    || state.history.items.get(state.playbackId);
  if (source && item) loadIncidentSnapshot(source, item);
});
window.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (!els.incidentPlayback.hidden) closeIncidentPlayback();
  else if (!els.incidentHistory.hidden) closeHistory();
});
new ResizeObserver(resizeCanvas).observe(canvas);
window.addEventListener('beforeunload', () => {
  stopCotReveal();
  state.heartbeat?.terminate();
});

resizeCanvas();
init().catch(error => {
  toast(`巡检页面初始化失败：${error?.message || error}`, 'err');
});
