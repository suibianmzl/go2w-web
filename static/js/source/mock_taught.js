// ?mock 专用流程演示。所有文档都标 simulated，参考只存当前浏览器；不宣称完成真实足迹校验。
import { captureIsOpen, cleanTaughtPoints, rebuildTaughtPoints, taughtLength, taughtStartBlocker, storeTaughtDocument } from '../taught_geometry.js';

export function createMockTaught(store, {prepare,halt}){
  const key = 'go2w.mock.taught'; let serial = 0, elapsed = 0;
  try {
    const saved = JSON.parse(localStorage.getItem(key) || '{}');
    for (const [map,d] of Object.entries(saved)){
      if (!store.maps.some(m=>m.name === map)) continue;
      cleanTaughtPoints(d.points); d.check = {state:'idle',ok:false,message:'模拟：刷新后需重新自检'};
      if (d.capture?.state === 'recording') Object.assign(d.capture,{state:'interrupted',can_resume:true,message:'模拟页面已中断，需人工恢复'});
      storeTaughtDocument(store,d);
    }
  } catch { /* 损坏的模拟数据不阻止控制台打开；真实数据始终由后端管理。 */ }
  const copy = v => JSON.parse(JSON.stringify(v));
  const persist = () => { try{ localStorage.setItem(key,JSON.stringify(store.taughtByMap)); }catch{} };
  function get(map){
    if (!store.maps.some(m=>m.name === map)) return null;
    if (!store.taughtByMap[map]) storeTaughtDocument(store,{map,map_id:`mock:${map}`,map_token:`mock-grid:${map}`,revision:0,points:[],length:0,
      state:'empty',executable:false,report:null,message:'纯前端模拟：不连接机器人，不做真实足迹校验',simulated:true,check:{state:'idle',ok:false,message:'先明确准备与自检（模拟）'},capture:null});
    return store.taughtByMap[map];
  }
  function result(d,msg){storeTaughtDocument(store,d);persist();return {ok:true,msg:msg || '纯前端模拟',data:copy(d)};}
  const fail = msg => ({ok:false,msg});
  function check(map){
    const d=get(map);if(!d)return fail('模拟地图不存在');
    const ok=store.link.ok && map === store.mapCurrent && store.profiles.mapping === 'stop';
    d.check={state:ok?'ready':'failed',ok,message:ok?'模拟自检通过；真实 TF、定位新鲜度与足迹未在此模式核验':'先准备所选地图（模拟）',checks:[{label:'流程演示',ok,message:'仅模拟，不代替真实定位与足迹检查'}]};
    return result(d,d.check.message);
  }
  function session(id){
    const d=Object.values(store.taughtByMap).find(d=>d.capture?.session_id === id);
    return d && captureIsOpen(d.capture) ? d : null;
  }
  function interrupt(){
    let interrupted=false;
    for(const d of Object.values(store.taughtByMap))if(d.capture?.state === 'recording'){
      interrupted=true;
      Object.assign(d.capture,{state:'interrupted',can_resume:true,message:'模拟中断：草稿保留，重连后不会自动采集'});result(d);
    }
    if(interrupted)halt();
  }
  const cmd={
    taughtGet:map=>{const d=get(map);return d?result(d):fail('模拟地图不存在');},
    taughtStatus:map=>{const d=get(map || store.taughtMap || store.mapCurrent);return d?result(d):fail('没有地图');},
    taughtPrepare(map){
      const d=get(map);if(!d)return fail('模拟地图不存在');
      if(Object.values(store.taughtByMap).some(other=>captureIsOpen(other.capture) &&
        !(other.map===map && ['paused','interrupted'].includes(other.capture.state))))return fail('先结束或放弃现有采集草稿');
      prepare(map);return check(map);
    },
    taughtCheck:check,
    taughtStart(p){
      const d=get(p.map),why=taughtStartBlocker(store,d,p.mode,{start:p.start_index,end:p.end_index});if(why)return fail(why);
      if(d.revision !== p.expected_revision || d.map_token !== p.map_token)return fail('模拟版本冲突，请重新读取');
      const anchor=p.mode === 'append'?d.points.at(-1):p.mode === 'replace_segment'?d.points[p.start_index]:null;
      if(anchor && Math.hypot(anchor.x-store.robot.x,anchor.y-store.robot.y)>.2)return fail('距离接入点超过 0.2 米（模拟）');
      const R=store.robot;
      d.capture={session_id:`mock-${Date.now()}-${++serial}`,map:p.map,state:'recording',mode:p.mode,sample_count:1,length:0,
        points:[{x:R.x,y:R.y,yaw:R.th,continuous_id:0}],message:'模拟采集中：请明确进入键盘遥控；没有真实足迹检查',can_resume:false,
        base_revision:d.revision,start_index:p.start_index,end_index:p.end_index,continuous_id:0};
      return result(d,'已开始采集（纯前端模拟）');
    },
    taughtPause(id){const d=session(id);if(!d || d.capture.state !== 'recording')return fail('采集会话已过期或不在采样');
      halt();Object.assign(d.capture,{state:'paused',can_resume:true,message:'已暂停（模拟）。回到断点附近再人工继续。'});return result(d);},
    taughtResume(id){
      const d=session(id);if(!d || !['paused','interrupted'].includes(d.capture.state))return fail('没有可恢复的采集会话');
      if(!check(d.map).data?.check?.ok)return fail('模拟自检未通过');
      const p=d.capture.points.at(-1),R=store.robot;if(!p || Math.hypot(p.x-R.x,p.y-R.y)>.2)return fail('距离断点超过 0.2 米（模拟）');
      Object.assign(d.capture,{state:'recording',can_resume:false,continuous_id:d.capture.continuous_id+1,message:'已恢复（模拟），实测断口仍保留'});return result(d);
    },
    taughtFinish(id){
      const d=session(id);if(!d)return fail('采集会话已过期');halt();
      const c=d.capture;if(c.points.length<2 || taughtLength(c.points)<.05){c.state='failed';result(d);return fail('模拟采样太短，草稿保留');}
      let next=c.points;
      if(c.mode==='append')next=[...d.points,...next.map(p=>({...p,continuous_id:`new:${p.continuous_id}`}))];
      if(c.mode==='replace_segment'){
        const end=d.points[c.end_index],last=next.at(-1);
        if(Math.hypot(last.x-end.x,last.y-end.y)>.2){c.state='failed';result(d);return fail('尚未到达选中终点（模拟）');}
        next=[...d.points.slice(0,c.start_index),...next.map(p=>({...p,continuous_id:`new:${p.continuous_id}`})),...d.points.slice(c.end_index+1)];
      }
      d.points=rebuildTaughtPoints(next);d.length=taughtLength(d.points);d.revision++;d.state='ready';d.executable=true;
      Object.assign(c,{state:'finished',can_resume:false,message:'仅保存在浏览器；真实足迹未检查'});return result(d,'已保存流程演示轨迹（模拟，未做真实足迹检查）');
    },
    taughtAbandon(id){const d=session(id);if(!d)return fail('采集会话已过期');halt();d.capture.state='abandoned';d.capture.can_resume=false;return result(d,'已放弃草稿（模拟）');},
    taughtSave(p){
      const d=get(p.map);if(!d)return fail('模拟地图不存在');
      if(d.revision!==p.expected_revision || d.map_token!==p.map_token)return fail('模拟版本冲突，原参考保留');
      let next;try{next=rebuildTaughtPoints(p.points);}catch(e){return fail(e.message);}
      if(next.length<2)return fail('至少需要两个路线点');
      d.points=next;d.length=taughtLength(next);d.revision++;d.state='ready';d.executable=true;return result(d,'已保存到浏览器（模拟，未做真实足迹检查）');
    },
  };
  return {cmd,interrupt,tick(dt){
    elapsed+=dt;
    for(const d of Object.values(store.taughtByMap)){
      const c=d.capture;if(c?.state !== 'recording')continue;
      if(!store.link.ok || store.mapCurrent !== d.map){interrupt();return;}
      const R=store.robot,last=c.points.at(-1);
      if(last && Math.hypot(last.x-R.x,last.y-R.y)<.05)continue;
      c.points.push({x:R.x,y:R.y,yaw:R.th,continuous_id:c.continuous_id});c.sample_count=c.points.length;c.length=taughtLength(c.points);
      storeTaughtDocument(store,d);
    }
    if(elapsed>.5){elapsed=0;persist();}
  }};
}
