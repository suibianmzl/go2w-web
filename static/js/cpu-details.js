// 明细仅在弹窗打开时采集；独立于控制指令和高频状态推送。
export function setupCpuDetails(src){
  const button = document.getElementById('chCpu');
  let opened = false;
  button.addEventListener('click', () => {
    if (opened) return;
    opened = true;
    const scrim = document.createElement('div'); scrim.className = 'scrim';
    scrim.innerHTML = `<div class="modal cpu-modal" role="dialog" aria-modal="true" aria-labelledby="cpuDetailsTitle" aria-describedby="cpuDetailsScope">
      <div class="cpu-heading"><h3 id="cpuDetailsTitle">CPU 占用前 5</h3><span class="cpu-mode" hidden>模拟数据</span></div>
      <div class="cpu-total">整机 CPU <b>—</b></div>
      <div class="cpu-status" role="status">正在采样…</div>
      <table class="cpu-table" hidden><thead><tr><th scope="col">模块 / 进程</th><th scope="col">CPU 占用率</th></tr></thead><tbody></tbody></table>
      <p class="cpu-scope" id="cpuDetailsScope">按整机 CPU 总容量计算，多线程合计。仅列出当前运行环境可见的进程；容器外进程不在列表内，同一进程中的组件合并统计。</p>
      <div class="btn-row"><button type="button" class="btn pri">关闭</button></div></div>`;
    const status = scrim.querySelector('.cpu-status');
    const table = scrim.querySelector('table'), body = scrim.querySelector('tbody'), total = scrim.querySelector('.cpu-total b');
    const closeButton = scrim.querySelector('button');
    let closed = false, timer = null, controller = null;
    function close(){
      closed = true; opened = false;
      clearTimeout(timer); controller?.abort();
      document.removeEventListener('keydown', keys, true);
      scrim.remove(); button.focus();
    }
    function keys(e){
      if (e.code === 'Space') return; // 保留全局急停。
      e.stopPropagation(); // 弹窗中的按键不能切换地图工具或驱动遥控。
      if (e.key === 'Escape'){ e.preventDefault(); close(); }
      if (e.key === 'Tab'){ e.preventDefault(); closeButton.focus(); }
    }
    async function refresh(){
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 8000);
      try {
        const data = await src.cpuStats(controller.signal);
        if (closed) return;
        if (!data.ok || !Array.isArray(data.items)) throw new Error(data.message || 'CPU 统计数据不可用');
        const rows = data.items.filter(item => Number.isFinite(item.cpu) && item.cpu >= 0 && item.cpu <= 100)
          .sort((a,b) => b.cpu-a.cpu).slice(0,5);
        body.replaceChildren();
        for (const item of rows){
          const row = document.createElement('tr');
          row.innerHTML = '<td><span class="cpu-name"></span><small></small></td><td><b></b><span class="cpu-bar"><i></i></span></td>';
          row.querySelector('.cpu-name').textContent = item.name || '未知进程';
          row.querySelector('small').textContent = `PID ${item.pid}`;
          row.querySelector('b').textContent = `${item.cpu.toFixed(1)}%`;
          row.querySelector('i').style.width = `${item.cpu}%`;
          body.appendChild(row);
        }
        total.textContent = Number.isFinite(data.cpu) ? `${data.cpu.toFixed(1)}%` : '—';
        scrim.querySelector('.cpu-mode').hidden = !data.mock;
        table.hidden = !rows.length;
        status.dataset.s = '';
        status.textContent = rows.length ? `已更新 ${new Date((data.sampledAt || Date.now()/1000)*1000).toLocaleTimeString()} · 每 2 秒刷新`
          : '暂无可统计的进程，稍后自动重试';
      } catch (error){
        if (closed) return;
        table.hidden = true; body.replaceChildren(); total.textContent = '—';
        status.dataset.s = 'warn';
        const message = error.name === 'TypeError' ? '无法读取 CPU 统计，请检查连接' : error.message;
        status.textContent = error.name === 'AbortError' ? '读取超时，请检查连接；稍后自动重试' : `${message || 'CPU 统计不可用'}；稍后自动重试`;
      } finally {
        clearTimeout(timeout);
        if (!closed) timer = setTimeout(refresh, 2000);
      }
    }
    scrim.addEventListener('click', e => { if (e.target === scrim) close(); });
    closeButton.addEventListener('click', close);
    document.addEventListener('keydown', keys, true);
    document.body.appendChild(scrim); closeButton.focus();
    refresh();
  });
}
