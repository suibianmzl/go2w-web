import { gridToCanvas } from '../js/util.js';
import { createRtcViewer } from '../js/rtc_viewer.js';

const $ = selector => document.querySelector(selector);
const T_MAP = 1;
const POSE_RENDER_MS = 1000;
const TRAIL_MIN_MOVE = 0.04;
const HEARTBEAT_MS = 500;

const els = {
  robotChip: $('#robotChip'),
  linkChip: $('#linkChip'),
  clock: $('#clock'),
  consoleLink: $('#consoleLink'),
  routeSelect: $('#routeSelect'),
  routeMap: $('#routeMap'),
  routeCount: $('#routeCount'),
  taskPill: $('#taskPill'),
  taskStateText: $('#taskStateText'),
  taskPercent: $('#taskPercent'),
  taskProgress: $('#taskProgress'),
  startBtn: $('#startBtn'),
  stopBtn: $('#stopBtn'),
  actionHint: $('#actionHint'),
  steps: $('#steps'),
  currentPoint: $('#currentPoint'),
  presence: $('#presence'),
  modeValue: $('#modeValue'),
  speedValue: $('#speedValue'),
  batteryValue: $('#batteryValue'),
  cpuValue: $('#cpuValue'),
  preflight: $('#preflight'),
  rtcBadge: $('#rtcBadge'),
  rtcMessage: $('#rtcMessage'),
  rtcVideo: $('#rtcVideo'),
  mapOverlay: $('#mapOverlay'),
  mapCanvas: $('#inspectionMap'),
  mapTitle: $('#mapTitle'),
  mapEmpty: $('#mapEmpty'),
  poseValue: $('#poseValue'),
  zoomOutMap: $('#zoomOutMap'),
  zoomInMap: $('#zoomInMap'),
  fitMap: $('#fitMap'),
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
  playbackMap: $('#playbackMap'),
  snapshotPlaceholder: $('#snapshotPlaceholder'),
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
  trail: [],
  lastTrailPoint: null,
  rtcStarted: false,
};

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
    state.selectedRouteId = state.routes[0]?.id || '';
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
  const points = route?.points || [];
  els.routeSelect.disabled = !state.routes.length || taskActive();
  els.routeMap.textContent = route?.map ? `地图 ${route.map}` : '地图 --';
  els.routeCount.textContent = `${points.length} 个节点`;
  els.startBtn.disabled = !route || !state.wsOpen || !state.online || taskActive();
  els.stopBtn.disabled = !state.wsOpen || !taskActive();
  renderTask();
}

function taskStateLabel(){
  const task = state.task || {};
  const route = selectedRoute();
  if (!route) return '选择任务后开始巡检';
  if (!taskForSelectedRoute()) return taskActive() ? '其他任务执行中' : '等待下发任务';
  if (task.status === 'active') return task.phase === 'checking' ? '出发准备中' : `前往第 ${task.point || 1} 站`;
  if (task.status === 'succeeded') return '巡检已完成';
  if (task.status === 'canceled') return '巡检已中止';
  if (task.status === 'aborted') return `巡检失败：${task.message || '导航异常'}`;
  return '等待下发任务';
}

function renderTask(){
  const route = selectedRoute();
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
  els.taskPill.textContent = status === 'active' ? '执行中'
    : status === 'succeeded' ? '已完成'
      : status === 'canceled' ? '已中止'
        : status === 'aborted' ? '失败' : '等待任务';
  els.taskStateText.textContent = taskStateLabel();
  els.taskPercent.textContent = `${Math.round(progress * 100)}%`;
  els.taskProgress.style.width = `${progress * 100}%`;
  els.currentPoint.textContent = related && task.point
    ? `${task.point}/${task.count || route?.points?.length || 0} · ${task.pointName || ''}`
    : '--';
  renderSteps(route, task, related);
  renderHint(route);
}

function renderSteps(route, task, related){
  const points = route?.points || [];
  if (!points.length){
    els.steps.innerHTML = '<li class="empty-step">选择任务后显示巡检节点</li>';
    return;
  }

  const current = related && Number.isFinite(task.point) ? task.point : 0;
  els.steps.innerHTML = points.map((point, index) => {
    const no = index + 1;
    let stepState = 'pending';
    let label = '待执行';
    if (related && task.status === 'succeeded'){
      stepState = 'done';
      label = '已完成';
    } else if (related && task.status === 'aborted' && no === current){
      stepState = 'failed';
      label = '失败';
    } else if (related && task.status === 'canceled' && no === current){
      stepState = 'failed';
      label = '已中止';
    } else if (related && task.status === 'active'){
      if (task.back){
        stepState = no === 1 ? 'active' : 'done';
        label = no === 1 ? '返回中' : '已完成';
      } else if (no < current){
        stepState = 'done';
        label = '已完成';
      } else if (no === current){
        stepState = 'active';
        label = task.back ? '返回中' : '前往中';
      }
    }
    return `<li data-s="${stepState}">
      <span class="index">${stepState === 'done' ? '✓' : String(no).padStart(2, '0')}</span>
      <span class="name" title="${esc(point.name || `第 ${no} 站`)}">${esc(point.name || `第 ${no} 站`)}</span>
      <span class="state">${label}</span>
    </li>`;
  }).join('');
}

function preflightReason(route){
  if (!state.wsOpen) return '巡检页面正在连接云端';
  if (!state.online) return '机器狗未连接到云端';
  if (state.machine?.estop) return '机器狗处于急停状态';
  if (state.machine?.busy) return '机器狗正在切换运行模式';
  if (!route) return '请选择一个巡检任务';
  if (taskActive()) return '已有巡检任务正在执行';
  const nav = state.machine?.profiles?.navigation;
  if (nav !== 'run') return '请先在设备控制台启动导航';
  if (!state.pose?.valid || state.pose?.frame !== 'map') return '等待地图定位和位姿就绪';
  if (state.machine?.mapCurrent && state.machine.mapCurrent !== route.map){
    return `请先在控制台切换到任务地图 ${route.map}`;
  }
  return '';
}

function renderHint(route){
  const reason = preflightReason(route);
  els.actionHint.textContent = reason || '导航和定位已就绪，可以下发巡检任务';
  els.actionHint.style.color = reason ? 'var(--warn)' : 'var(--green)';
  if (reason) els.startBtn.title = reason;
  else els.startBtn.title = '下发巡检任务并自动解锁底盘';
}

function renderTelemetry(){
  const machine = state.machine || {};
  const pose = state.pose;
  const profiles = machine.profiles || {};
  const mode = profiles.navigation === 'run' ? '自主导航'
    : profiles.localization === 'run' ? '定位'
      : profiles.mapping === 'run' ? '建图' : '未启动';
  els.modeValue.textContent = mode;
  els.speedValue.textContent = pose?.valid && Number.isFinite(pose.v) ? `${pose.v.toFixed(2)} m/s` : '--';
  els.batteryValue.textContent = Number.isFinite(machine.battery) ? `${Math.round(machine.battery)}%` : '--';
  els.cpuValue.textContent = Number.isFinite(machine.sys?.cpu) ? `${Math.round(machine.sys.cpu)}%` : '--';

  const nav = profiles.navigation === 'run';
  const mapReady = !!state.map && !!machine.mapCurrent;
  const poseReady = !!pose?.valid && pose.frame === 'map' && machine.odomOk !== false;
  const values = {
    nav: [nav ? 'ok' : 'warn', nav ? '运行中' : '未启动'],
    map: [mapReady ? 'ok' : 'warn', mapReady ? machine.mapCurrent : '等待'],
    pose: [poseReady ? 'ok' : 'warn', poseReady ? '已就绪' : '未就绪'],
  };
  for (const [key, [status, text]] of Object.entries(values)){
    const node = els.preflight.querySelector(`[data-k="${key}"]`);
    node.dataset.s = status;
    node.querySelector('b').textContent = text;
  }
  els.poseValue.textContent = pose?.valid
    ? `x ${pose.x.toFixed(2)} · y ${pose.y.toFixed(2)} · θ ${(pose.th * 180 / Math.PI).toFixed(0)}°`
    : 'x -- · y -- · θ --';
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
    els.incidentSnapshot.src = url;
    els.incidentSnapshot.hidden = false;
    els.snapshotPlaceholder.hidden = true;
  } else {
    els.incidentSnapshot.removeAttribute('src');
    els.incidentSnapshot.hidden = true;
    els.snapshotPlaceholder.hidden = false;
  }
  drawPlaybackMap(item);
}

function drawPlaybackMap(item){
  const canvas = els.playbackMap;
  const pose = item.pose;
  const map = state.map;
  if (!map || !Number.isFinite(pose?.x) || !Number.isFinite(pose?.y)
    || (item.map && state.machine?.mapCurrent && item.map !== state.machine.mapCurrent)){
    canvas.hidden = true;
    return;
  }
  canvas.hidden = false;
  const context = canvas.getContext('2d');
  const width = canvas.width, height = canvas.height;
  const rangeX = 16, rangeY = rangeX * height / width;
  const scale = Math.min(width / rangeX, height / rangeY);
  const sx = x => width / 2 + (x - pose.x) * scale;
  const sy = y => height / 2 - (y - pose.y) * scale;
  context.clearRect(0, 0, width, height);
  context.fillStyle = '#030910';
  context.fillRect(0, 0, width, height);
  context.save();
  context.globalAlpha = .78;
  if (Math.abs(map.yaw || 0) < 1e-5){
    context.drawImage(map.img, sx(map.ox), sy(map.oy + map.h), map.w * scale, map.h * scale);
  } else {
    context.translate(sx(map.ox), sy(map.oy));
    context.rotate(-map.yaw);
    context.drawImage(map.img, 0, -map.h * scale, map.w * scale, map.h * scale);
  }
  context.restore();

  const x = sx(pose.x), y = sy(pose.y);
  context.save();
  context.strokeStyle = '#f3b13f';
  context.fillStyle = 'rgba(243,177,63,.28)';
  context.lineWidth = 2;
  context.beginPath();
  context.arc(x, y, 9, 0, Math.PI * 2);
  context.fill();
  context.stroke();
  context.beginPath();
  context.moveTo(x, y);
  context.lineTo(x + Math.cos(-pose.th) * 22, y + Math.sin(-pose.th) * 22);
  context.stroke();
  context.restore();
  context.fillStyle = 'rgba(8,15,22,.8)';
  context.fillRect(0, 0, width, 30);
  context.fillStyle = '#9eb0c4';
  context.font = '12px "JetBrains Mono", monospace';
  context.fillText(`异常位置  ${item.map || state.machine?.mapCurrent || 'map'}`, 10, 20);
}

function openIncidentPlayback(item){
  if (!item) return;
  els.incidentPlayback.hidden = false;
  renderPlayback(item);
}

function closeIncidentPlayback(){
  state.playbackId = null;
  els.incidentPlayback.hidden = true;
  els.incidentSnapshot.removeAttribute('src');
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
      if (!state.online) state.task = { type:'idle' };
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
      renderRouteOptions();
      drawMap();
      break;
    case 'task':
      state.task = message.task || { type:'idle' };
      if (taskActive() && state.task.routeId) {
        state.selectedRouteId = state.task.routeId;
        els.routeSelect.value = state.selectedRouteId;
      }
      renderTask();
      renderSelectedRoute();
      drawMap();
      break;
    case 'pose':
      state.pose = message;
      state.poseAt = performance.now();
      renderTelemetry();
      renderHint(selectedRoute());
      break;
    case 'state':
      state.machine = message;
      if (taskActive()) els.routeSelect.disabled = true;
      renderTelemetry();
      renderTask();
      renderHint(selectedRoute());
      break;
    case 'incidents':
      mergeIncidents(message.items, true);
      break;
    case 'incident':
      mergeIncidents(message.item ? [message.item] : []);
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

function send(command){
  return new Promise(resolve => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN){
      resolve({ ok:false, msg:'巡检页面尚未连接云端' });
      return;
    }
    const id = ++state.commandId;
    const timer = setTimeout(() => {
      state.pending.delete(id);
      resolve({ ok:false, msg:'云端没有响应（超时）' });
    }, 10000);
    state.pending.set(id, { resolve, timer });
    state.ws.send(JSON.stringify({ t:'cmd', id, ...command }));
  });
}

async function startInspection(){
  const route = selectedRoute();
  const reason = preflightReason(route);
  if (reason){
    toast(reason, 'warn');
    return;
  }
  const opts = route.opts || {};
  els.startBtn.disabled = true;
  els.startBtn.textContent = '正在下发…';
  const reply = await send({
    op:'route_start',
    route_id:route.id,
    round_trip:!!opts.loop,
    loops:Number.isInteger(opts.loops) ? opts.loops : 1,
    arm:true,
    ...(Number.isFinite(opts.vmax) ? { vmax:opts.vmax } : {}),
    ...(Number.isFinite(opts.wmax) ? { wmax:opts.wmax } : {}),
  });
  els.startBtn.textContent = '开始巡检';
  if (reply.ok){
    toast(reply.msg || '巡检任务已下发');
    state.selectedRouteId = route.id;
  } else {
    toast(reply.msg || '巡检任务下发失败', 'err');
  }
  renderSelectedRoute();
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
}

function connect(){
  clearTimeout(state.reconnectTimer);
  if (!state.robotId){
    setChip(els.robotChip, 'off', '未选择');
    setChip(els.linkChip, 'off', '无设备');
    els.actionHint.textContent = '设备列表中还没有可用的机器狗';
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

  points.forEach((point, index) => {
    const x = sx(point.x), y = sy(point.y);
    const isCurrent = taskForSelectedRoute() && task.status === 'active' && task.point === index + 1;
    ctx.save();
    ctx.strokeStyle = isCurrent ? '#38e1dc' : '#7d8da0';
    ctx.fillStyle = isCurrent ? 'rgba(56,225,220,.3)' : 'rgba(7,14,21,.94)';
    ctx.lineWidth = isCurrent ? 2 : 1.2;
    if (isCurrent){
      ctx.shadowColor = '#38e1dc';
      ctx.shadowBlur = 12;
    }
    ctx.beginPath();
    ctx.arc(x, y, isCurrent ? 6 : 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.fillStyle = isCurrent ? '#b6fffc' : '#98aabc';
    ctx.font = '500 10px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(String(index + 1), x, y - 11);
    ctx.restore();
  });
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
  const route = selectedRoute();
  if (state.pose?.valid && state.pose.frame === 'map') samplePose(state.pose);
  els.mapTitle.textContent = route ? `${route.name} · 坐标线路` : '巡检坐标线路';
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
  renderSelectedRoute();
  fitView();
  drawMap();
});
els.startBtn.addEventListener('click', startInspection);
els.stopBtn.addEventListener('click', stopInspection);
els.zoomOutMap.addEventListener('click', () => zoomMap(0.8));
els.zoomInMap.addEventListener('click', () => zoomMap(1.25));
els.fitMap.addEventListener('click', () => { fitView(); drawMap(); });
els.collapseMap.addEventListener('click', () => setMapCollapsed(true));
els.expandMap.addEventListener('click', () => setMapCollapsed(false));
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
els.incidentSnapshot.addEventListener('error', () => {
  els.incidentSnapshot.hidden = true;
  els.snapshotPlaceholder.hidden = false;
});
window.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (!els.incidentPlayback.hidden) closeIncidentPlayback();
  else if (!els.incidentHistory.hidden) closeHistory();
});
new ResizeObserver(resizeCanvas).observe(canvas);
window.addEventListener('beforeunload', () => state.heartbeat?.terminate());

resizeCanvas();
init().catch(error => {
  toast(`巡检页面初始化失败：${error?.message || error}`, 'err');
});
