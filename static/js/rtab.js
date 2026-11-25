// RTAB 影子库卡片（11-20，后端 server/rtab.py）：建图时顺带录 RTAB-Map 库；
// 定位 / 导航在用一张有库的图时可以「局部重扫」——在旧库上续录一轮，之后（P2）用它替换地图里变了的一块。
// 卡片跟着档位变：建图中显示录制状态；定位 / 导航中显示当前图有没有库、局部重扫按钮；随时可以停。
import { $ } from './util.js';
import { toast, formBox } from './ui.js';

const PHASE = {
  starting: ['warn', '启动中'], localizing: ['warn', '对位中'], recording: ['run', '录制中'], paused: ['', '已暂停'],
  stopping: ['warn', '存库中'], stuck: ['crit', '存库超时'], stale: ['crit', '需要重来'],
};
const KIND = { map: '建图同步录制', continue: '局部重扫' };

export function setupRtab(store, src){
  const prof = id => store.profiles[id];
  const run = id => prof(id) === 'run';
  const exec = p => Promise.resolve(p).then(r => { if (r && r.ok === false) toast(r.msg || '操作失败', 'err'); else if (r?.msg) toast(r.msg); return r; });
  const fmt = s => s == null ? '--' : s < 60 ? `${Math.round(s)} 秒` : `${Math.floor(s / 60)} 分 ${String(Math.floor(s % 60)).padStart(2, '0')} 秒`;
  const hm = t => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });
  const set = (el, txt) => { if (el.textContent !== txt) el.textContent = txt; };

  $('#rtabStart').onclick = () => exec(src.cmd.rtabStart());
  $('#rtabPause').onclick = () => exec(src.cmd.rtabPause());
  $('#rtabResume').onclick = () => exec(src.cmd.rtabResume());
  $('#rtabStop').onclick = () => exec(src.cmd.rtabStop());
  $('#rtabAuto').onchange = e => { const on = e.target.checked; e.target.checked = !on; exec(src.cmd.rtabAuto(on)); };   // 勾选框跟着后端状态走
  $('#rtabCont').onclick = () => {
    const R = store.rtab;
    formBox({
      title: '局部重扫', ok: '开始局部重扫',
      rows: [['地图', R.map], ['RTAB 库', R.mapDb]],
      body: '从机器人现在的位置开始，在这张图的 RTAB 库上续录一轮（原库先自动备份）。开始后让狗慢慢走一两米或原地转一圈，'
        + 'RTAB 和旧库对上才会开始录；对不上会自动停下，库不变。对上之后去变化的区域，从不同位置扫过变化的地方，扫完点「停止并保存」。',
      warn: '先确认定位准确（重定位里激光贴墙）：起点偏太多会对不上。',
      onOk: () => exec(src.cmd.rtabContinue(R.map)),
    });
  };

  function render(){
    const R = store.rtab, card = $('#rtabCard');
    const mapping = run('mapping'), locOn = run('localization') || run('navigation');
    const cur = R?.cur, last = R?.last;
    const show = !!R?.avail && (!!cur || mapping || locOn || !!last);
    card.hidden = !show;
    if (!show) return;
    const pill = $('#rtabPill'), [ps, pt] = cur ? (PHASE[cur.phase] || ['', cur.phase]) : ['', '没在录'];
    if (pill.textContent !== pt){ pill.dataset.s = ps; pill.textContent = pt; }
    // 说明
    let note = '';
    if (cur){
      note = `${KIND[cur.kind] || cur.kind} · ${cur.db}${cur.map ? `（地图 ${cur.map}）` : ''} · 已 ${fmt(cur.elapsed)}`;
      if (cur.phase === 'paused') note += cur.paused_by === 'freeze' ? ' · 建图冻结时自动暂停，继续建图后自动恢复' : '';
      if (cur.msg) note += `\n${cur.msg}`;
    } else if (mapping){
      note = store.cont ? '续建期间不录（续建的坐标系和这次建图不同）。'
        : R.pending ? '等建图稳定后自动开始录……' : '这次建图没在录 RTAB 库。要以后能局部更新这张图，点「开始录」。';
    } else if (locOn){
      note = R.mapDb ? `当前地图 ${R.map} 有 RTAB 库 ${R.mapDb}：场地局部变了（挪了家具、新隔断）可以局部重扫。`
        : `当前地图 ${R.map || '--'} 没有 RTAB 库（只有录了 RTAB 库的建图存下的图才有），不能局部重扫。`;
    }
    set($('#rtabNote'), note);
    // 按钮
    const busy = cur && ['starting', 'stopping'].includes(cur.phase);
    $('#rtabStart').hidden = !(!cur && mapping && !store.cont);
    $('#rtabCont').hidden = !(!cur && locOn && R.mapDb);
    $('#rtabPause').hidden = cur?.phase !== 'recording';
    $('#rtabResume').hidden = cur?.phase !== 'paused';
    $('#rtabStop').hidden = !cur;
    $('#rtabStop').disabled = !!busy;
    set($('#rtabStop'), cur?.phase === 'localizing' ? '取消' : '停止并保存');
    $('#rtabBtns').hidden = [...$('#rtabBtns').children].every(b => b.hidden);
    const auto = $('#rtabAuto');
    if (document.activeElement !== auto) auto.checked = !!R.auto;
    // 上一次结果
    const lt = $('#rtabLast');
    lt.hidden = !last || !!cur;
    if (last){
      set(lt, `上一次（${hm(last.wall)}，${KIND[last.kind] || last.kind} · ${last.db}）：${last.msg}`);
      lt.classList.toggle('warn', last.ok === false);
    }
  }
  return { render };
}
