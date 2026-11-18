const $ = selector => document.querySelector(selector);
const devices = $('#devices');
let currentDevices = [];

function text(value, fallback = '—'){
  return value === null || value === undefined || value === '' ? fallback : String(value);
}

function html(value, fallback = '—'){
  return text(value, fallback).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

function render(robots){
  currentDevices = robots;
  $('#device-count').textContent = robots.length;
  $('#total').textContent = robots.length;
  $('#online').textContent = robots.filter(robot => robot.online).length;
  $('#tasks').textContent = robots.filter(robot => robot.task === 'active').length;

  if (!robots.length){
    devices.innerHTML = '<div class="empty">暂无已接入的机器狗</div>';
    return;
  }

  devices.replaceChildren(...robots.map(robot => {
    const card = document.createElement('article');
    card.className = 'device';
    const online = !!robot.online;
    const task = robot.task === 'active'
      ? `${text(robot.task_type, '任务')}执行中`
      : online ? '空闲待命' : '等待重新连接';
    const battery = robot.battery == null ? '电量 —' : `电量 ${Math.round(robot.battery)}%`;
    const lastSeen = robot.last_seen_seconds == null ? '' : ` · ${robot.last_seen_seconds.toFixed(1)} 秒前`;
    const video = robot.video_upload_enabled === false
      ? '视频上传关闭'
      : robot.camera_source ? '视频源在线' : '视频源离线';
    const consoleUrl = `/quadruped/?robot_id=${encodeURIComponent(robot.robot_id)}`;
    const inspectionUrl = `/quadruped/inspection/?robot_id=${encodeURIComponent(robot.robot_id)}`;
    const binding = robot.ssh;
    const sshUser = binding?.username || 'unitree';
    const sshCommand = binding
      ? `ssh ${sshUser}@127.0.0.1 -p ${binding.port}`
      : '';
    card.innerHTML = `
      <div class="device-head">
        <span class="badge ${online ? '' : 'offline'}">${online ? '● 在线' : '● 离线'}</span>
        <span class="robot-id">${html(robot.robot_id)}</span>
      </div>
      <div class="visual"><div class="robot-icon"></div></div>
      <h3>${html(robot.name, robot.robot_id)}</h3>
      <p>${html(task)}${online ? '' : html(lastSeen)}</p>
      <div class="metrics">
        <span>${battery}</span>
        <span>${video}</span>
      </div>
      ${binding ? `
        <div class="ssh-box">
          <div class="ssh-title">SSH 反向代理 · broker 端口 ${html(binding.port)}</div>
          <code title="${html(sshCommand)}">${html(sshCommand)}</code>
          <div class="ssh-actions">
            <button class="mini" type="button" data-action="copy-ssh" data-device-id="${html(robot.robot_id)}">复制第二步</button>
            <button class="mini danger" type="button" data-action="unbind" data-device-id="${html(robot.robot_id)}">手动解绑</button>
          </div>
        </div>
      ` : `
        <div class="ssh-box empty-ssh">
          <div class="ssh-title">SSH 反向代理</div>
          <span>未分配端口</span>
        </div>
      `}
      <div class="actions">
        <a class="console-link inspection-link" href="${inspectionUrl}">进入巡检 →</a>
        <a class="console-link" href="${consoleUrl}">进入设备控制台 →</a>
      </div>
    `;
    return card;
  }));
}

function mergeDevices(robots, bindings){
  const merged = new Map();
  for (const robot of robots){
    merged.set(robot.robot_id, { ...robot });
  }
  for (const binding of bindings){
    const id = binding.deviceId;
    const existing = merged.get(id) || {};
    merged.set(id, {
      ...existing,
      robot_id: id,
      name: existing.name || binding.deviceName || id,
      online: Object.hasOwn(existing, 'online') ? !!existing.online : !!binding.online,
      last_seen_at: binding.lastSeenAt || existing.last_seen_at,
      video_upload_enabled: existing.video_upload_enabled,
      camera_source: existing.camera_source,
      battery: existing.battery,
      task: existing.task,
      task_type: existing.task_type,
      ssh: binding,
    });
  }
  return [...merged.values()].sort((left, right) => left.robot_id.localeCompare(right.robot_id));
}

async function fetchJson(url){
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${url} HTTP ${response.status}`);
  return response.json();
}

async function refresh(){
  try {
    const [robotData, bindingData] = await Promise.all([
      fetchJson('/quadruped/api/robots'),
      fetchJson('/internal/v1/ssh-bindings').catch(() => ({ items: [] })),
    ]);
    render(mergeDevices(
      Array.isArray(robotData.robots) ? robotData.robots : [],
      Array.isArray(bindingData.items) ? bindingData.items : [],
    ));
  } catch (error){
    devices.innerHTML = `<div class="empty">设备列表加载失败：${html(error.message)}</div>`;
  }
}

async function copySsh(robotId){
  const robot = currentDevices.find(item => item.robot_id === robotId);
  const binding = robot?.ssh;
  if (!binding?.port) return;
  const command = `ssh ${binding.username || 'unitree'}@127.0.0.1 -p ${binding.port}`;
  try {
    await navigator.clipboard.writeText(command);
    window.alert('SSH 命令已复制');
  } catch {
    window.prompt('复制 SSH 命令', command);
  }
}

async function copyGuide(){
  const command = 'ssh -p 22 -i <本机 broker 私钥路径> <broker SSH 用户>@ssh-portbroker.evoxrobot.com';
  try {
    await navigator.clipboard.writeText(command);
    window.alert('第一步 SSH 命令已复制，请替换私钥路径');
  } catch {
    window.prompt('复制第一步 SSH 命令', command);
  }
}

async function unbind(robotId){
  const robot = currentDevices.find(item => item.robot_id === robotId);
  const binding = robot?.ssh;
  if (!binding?.port) return;
  const confirmed = window.confirm(
    `确认手动解绑 ${robotId} 的 SSH 端口 ${binding.port}？\n离线设备不会自动解绑，此操作会释放 broker 端口。`,
  );
  if (!confirmed) return;
  try {
    const response = await fetch(
      `/internal/v1/robots/${encodeURIComponent(robotId)}/ssh-binding`,
      {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          manual: true,
          operator: 'fleet-console',
          reason: 'destroy',
        }),
      },
    );
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
    window.alert(`已解绑 ${robotId}，释放端口 ${payload.port}`);
    await refresh();
  } catch (error){
    window.alert(`解绑失败：${error?.message || error}`);
  }
}

devices.addEventListener('click', event => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const robotId = button.dataset.deviceId;
  if (button.dataset.action === 'copy-ssh') copySsh(robotId);
  if (button.dataset.action === 'unbind') unbind(robotId);
});

document.querySelector('[data-copy-guide="broker"]').addEventListener('click', copyGuide);
$('#refresh').addEventListener('click', refresh);
refresh();
setInterval(refresh, 5000);
