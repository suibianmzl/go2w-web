// 执行真实 setupFlow/prepTick：只替代 DOM 与外部动作，不启动后端或机器人。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const util = moduleUrl(await readFile(new URL('../static/js/util.js',import.meta.url),'utf8'));
const ui = moduleUrl('export function toast(){};export function stillCheck(){};export function formBox(value){globalThis.flowForms.push(value);}');
const source = (await readFile(new URL('../static/js/flow.js',import.meta.url),'utf8')).replace('./util.js',util).replace('./ui.js',ui);
const { createStore } = await import(moduleUrl(await readFile(new URL('../static/js/store.js',import.meta.url),'utf8')));
const { setupFlow } = await import(moduleUrl(source));
let time = 100000;
const oldNow = Date.now;Date.now=()=>time;
class Element {
  constructor(){this.value='';this.textContent='';this.dataset={};this.classList={toggle(){}};this.parts=new Map();}
  querySelector(selector){if(!this.parts.has(selector))this.parts.set(selector,new Element());return this.parts.get(selector);}
  replaceChildren(...children){this.children=children;if(children.length&&'value'in children[0])this.value=children[0].value;}
}
globalThis.Option=class {constructor(label,value){this.textContent=label;this.value=value;}};
globalThis.document={body:{classList:{contains:()=>false}}};
function rig({available=true,navigation=false,profile='run',reply={ok:true}}={}){
  const elements=new Map(),el=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  document.querySelector=s=>el(s.startsWith('#')?s.slice(1):s);document.querySelectorAll=()=>[];
  globalThis.flowForms=[];
  const store=createStore(),calls=[];
  Object.assign(store,{source:'ros',mapCurrent:'one',mapLive:true,maps:[{name:'one'}],taughtAvailable:available,taughtNavigation:navigation,
    odomOk:true,fixed:'map',ctrl:{on:false}});
  Object.assign(store.robot,{valid:true,x:1,y:2,th:0});store.profiles.navigation=profile;
  const route={id:'route1',name:'普通路线',map:'one',points:[{x:1,y:2},{x:3,y:2}],opts:{}};
  store.routes=[route];el('routeSelect').value=route.id;
  const src={now:()=>0,can:()=>false,cmd:{setMode:async(mode,map)=>{calls.push(['mode',mode,map]);return reply;},loadMap:async map=>{calls.push(['map',map]);return {ok:true};}}};
  const actions={routeGoBlocker:()=>'',startRoute:async r=>{calls.push(['route',r.id]);return {ok:true};},
    autoLocalize:async()=>{calls.push(['localize']);return {ok:true};}};
  const flow=setupFlow(store,src,actions,()=>{},{changed:()=>false,render(){},on:false},{render(){},enter(){},leave:()=>true},{leaveEdit:()=>true});
  return {store,route,calls,flow,el,start(){store.startGo(route);}};
}
const flush=async()=>{await Promise.resolve();await Promise.resolve();};
try{
  const r=rig();r.flow.render();assert.deepEqual(r.calls,[],'仅查看页面不得升级导航栈');
  r.store.initPose={map:'one'};r.start();
  assert.deepEqual(r.calls,[['mode','navigation','one']],'用户点击普通任务出发后升级旧导航栈');
  assert.equal(r.store.prep().step,'nav');assert.equal(r.store.initPose,null,'重启不能沿用旧定位确认');
  for(let i=0;i<10;i++){time+=100;r.flow.render();}
  assert.equal(r.calls.length,1,'旧状态连续推送不能重复派发重启');
  r.store.busy=true;r.store.profiles.navigation='stopping';r.flow.render();
  r.store.profiles.navigation='starting';r.flow.render();assert.equal(r.store.prep().fail,null,'计划内停栈不当作导航掉线');
  r.store.busy=false;r.store.profiles.navigation='run';r.store.taughtNavigation=true;r.flow.render();
  assert.equal(r.store.prep().step,'loc');assert.equal(r.calls.length,1,'节点就绪后仍等待既有定位流程');
  r.store.autoLoc={seq:2,state:'ok',map:'one'};r.flow.render();await flush();
  assert.deepEqual(r.calls,[['mode','navigation','one'],['route','route1']]);assert.equal(r.store.prep(),null);

  const stuck=rig();stuck.start();time+=8100;stuck.flow.render();
  assert.equal(stuck.calls.length,1);assert.equal(stuck.store.prep().fail.k,'nav','旧栈持续在线但示教节点未就绪也会结算超时');
  time+=20000;stuck.flow.render();assert.equal(stuck.calls.length,1,'超时后不自动重复启停');
  for(const flags of [{available:true,navigation:true},{available:false,navigation:false},{available:null,navigation:null},{available:true,navigation:null}]){
    const old=rig(flags);old.store.initPose={map:'one'};old.start();await flush();
    assert.deepEqual(old.calls,[['route','route1']],`已就绪/不支持/旧后端沿用原逻辑 ${JSON.stringify(flags)}`);
  }
  const stopped=rig({profile:'stop',available:null,navigation:null});stopped.start();
  assert.deepEqual(stopped.calls,[],'导航未开时保留原出发确认框');
  globalThis.flowForms.pop().onOk();assert.deepEqual(stopped.calls,[['mode','navigation','one']]);
  stopped.flow.render();assert.equal(stopped.calls.length,1);
  console.log('PASS 普通出发按需启用示教、一次请求、计划重启等待、定位门槛、超时与旧后端兼容');
}finally{Date.now=oldNow;}
