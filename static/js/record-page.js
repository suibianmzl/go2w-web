import { createStore } from './store.js';
import { Bus } from './util.js';
import { createRosSource } from './source/ros.js';
import { createMockSource } from './source/mock.js';
import { createRollbackCapture, recordingShortcut, setupRecordingControls } from './recording-capture.js';
import { confirmBox } from './ui.js';

const get=id=>document.getElementById(id), store=createStore(), bus=new Bus();
const mock=new URLSearchParams(location.search).has('mock');
const src=mock?createMockSource(store,bus):createRosSource(store,bus);
const capture=createRollbackCapture(store,src);
setupRecordingControls(capture);
get('consoleLink').href=mock?'/?mock':'/';
get('recordButton').onclick=()=>capture.save();
window.addEventListener('keydown',recordingShortcut(()=>capture.save()));
let started=0, wasBusy=false, lastKey='', loading=false;
const cards=new Map();

function render(){
  const state=capture.state, rb=store.rollback;
  const ready=store.link.ok && rb.run;
  get('recordConnection').textContent=!store.link.ok?'连接已断开':mock?'模拟数据':rb.run?'回溯录制已就绪':'回溯录制未运行';
  get('recordConnection').dataset.ready=ready;
  const button=get('recordButton');
  button.disabled=!ready || rb.saving || state.busy || !!state.pending;
  button.dataset.busy=state.busy;
  button.setAttribute('aria-busy',state.busy);
  const remaining=Math.max(0,10-Math.floor((performance.now()-started)/1000));
  get('recordHint').textContent=state.busy ? state.recording?`语音剩余 ${state.remaining} 秒 · ${remaining?`收集后续数据 ${remaining} 秒`:'正在完成保存'}`:remaining?`收集后续数据 · 剩余 ${remaining} 秒`:'正在完成保存…'
    : !store.link.ok?'等待连接，恢复后可继续录包':!rb.run?'请先在控制台启动建图、定位或导航并打开回溯录制':rb.saving?'另一段录包正在保存':state.pending?'先重试保存或下载本次语音，再继续录包':'按一次，保存一包 · Shift+Enter';
}
capture.subscribe(state=>{
  if(state.busy && !wasBusy)started=performance.now();
  if(!state.busy && wasBusy)refresh();
  wasBusy=state.busy;render();
});

function showResults(items){
  const key=JSON.stringify(items);
  if(key===lastKey)return;lastKey=key;
  get('recordCount').textContent=`${items.length} 个`;
  const nodes=items.map(item=>{
    let row=cards.get(item.name);
    if(!row){
      const card=document.createElement('article');card.className='record-result';
      const name=document.createElement('h3');name.textContent=item.name;
      const detail=document.createElement('p'),voice=document.createElement('p');card.append(name,detail,voice);
      row={card,detail,voice,player:null,recover:null};cards.set(item.name,row);
    }
    const when=item.mtime?new Date(item.mtime*1000).toLocaleString():'';
    row.detail.textContent=`${Number.isFinite(item.dur)?`${item.dur.toFixed(1)} 秒`:'时长未记录'} · ${item.size ?? 0} MB${when?` · ${when}`:''}`;
    row.voice.textContent=item.audio?'语音备注已保存':'无语音备注';
    if(item.audio){
      if(!row.player){const player=document.createElement('audio');player.controls=true;player.preload='none';player.setAttribute('aria-label',`${item.name} 的语音备注`);row.player=player;row.card.append(player);}
      if(row.player.getAttribute('src')!==item.audio.url)row.player.src=item.audio.url;
    }else if(row.player){row.player.pause();row.player.remove();row.player=null;}
    if(!item.audio && item.voice_state==='pending'){
      if(!row.recover){
        const button=document.createElement('button');button.className='record-voice-recover';button.textContent='结束待处理语音';row.card.append(button);row.recover=button;
      }
      row.voice.textContent='语音尚未保存；若原录制页面已关闭，可确认本次无录音';
      row.recover.onclick=()=>confirmBox({title:'结束待处理语音',body:'确认本包不再保存语音？后续上传会明确记录无录音的原因，数据包会保留。',ok:'确认无录音',onOk:async()=>{
        try{
          const response=await fetch(`/api/recordings/${encodeURIComponent(item.name)}/audio-status`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state:'unavailable',reason:'用户在结果列表确认本次无录音',request_id:item.request_id}),signal:AbortSignal.timeout(15000)});
          if(!response.ok)throw Error((await response.text()).slice(0,160));
          refresh();
        }catch(e){get('recordListStatus').textContent=`语音状态更新失败：${e.message}`;}
      }});
    }else if(row.recover){row.recover.remove();row.recover=null;}
    return row.card;
  });
  if(!nodes.length){const empty=document.createElement('p');empty.className='record-empty';empty.textContent='还没有录包。按下底部按钮，保存当前事件。';nodes.push(empty);}
  const container=get('recordResults'), keep=new Set(items.map(item=>item.name));
  for(const [name,row] of cards)if(!keep.has(name)){row.player?.pause();row.card.remove();cards.delete(name);}
  container.querySelector('.record-empty')?.remove();
  nodes.forEach((node,i)=>{if(container.children[i]!==node)container.insertBefore(node,container.children[i]||null);});
}

async function refresh(){
  if(loading)return;loading=true;
  try{
    let items;
    if(mock)items=store.bags.map(b=>({...b,audio:null}));
    else{
      const response=await fetch('/api/recordings',{cache:'no-store',signal:AbortSignal.timeout(10000)});
      if(!response.ok)throw Error(`HTTP ${response.status}`);
      items=(await response.json()).items;
    }
    showResults(items);get('recordListStatus').textContent='';
  }catch(e){get('recordListStatus').textContent=`结果列表刷新失败：${e.message}；连接恢复后自动重试`;}
  finally{loading=false;}
}
refresh();
setInterval(()=>{src.tick(.25);render();},250);
setInterval(()=>{if(!document.hidden)refresh();},3000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
