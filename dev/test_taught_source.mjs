// 验证真实数据源的示教协议封装；只使用内存 WS，不连接机器人。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const util = moduleUrl(await readFile(new URL('../static/js/util.js',import.meta.url),'utf8'));
const geometry = moduleUrl(await readFile(new URL('../static/js/taught_geometry.js',import.meta.url),'utf8'));
const code = (await readFile(new URL('../static/js/source/ros.js',import.meta.url),'utf8')).replace('../util.js',util).replace('../taught_geometry.js',geometry);
globalThis.location = {protocol:'http:',host:'localhost:8080'};
globalThis.Worker = class {};
let socket, sent=[];
globalThis.WebSocket = class {
  constructor(){this.readyState=1;socket=this;}
  send(raw){const m=JSON.parse(raw);sent.push(m);if(m.t==='cmd')queueMicrotask(()=>this.onmessage({data:JSON.stringify({t:'ack',id:m.id,ok:true,data:null})}));}
};
const realTimeout = globalThis.setTimeout;
globalThis.setTimeout=(...args)=>{const timer=realTimeout(...args);timer.unref();return timer;};
const { createRosSource } = await import(moduleUrl(code));
const { createStore } = await import(moduleUrl(await readFile(new URL('../static/js/store.js',import.meta.url),'utf8')));
const store = createStore();store.taughtMap='a';
const src = createRosSource(store,{emit:()=>{}});
for(const [method,args,op,expected] of [
  ['taughtGet',['a'],'taught_get',{map:'a'}],
  ['taughtSave',[{map:'a',points:[],expected_revision:3,map_token:'t'}],'taught_save',{map:'a',points:[],expected_revision:3,map_token:'t'}],
  ['taughtPrepare',['a'],'taught_prepare',{map:'a'}],['taughtCheck',['a'],'taught_check',{map:'a'}],
  ['taughtStart',[{map:'a',mode:'replace_segment',expected_revision:3,map_token:'t',start_index:2,end_index:8}],'taught_start',{map:'a',mode:'replace_segment',expected_revision:3,map_token:'t',start_index:2,end_index:8}],
  ...['Pause','Resume','Finish','Abandon'].map(name=>['taught'+name,['session-42'],'taught_'+name.toLowerCase(),{session_id:'session-42'}]),
  ['taughtStatus',['a'],'taught_status',{map:'a'}],
]){
  assert.equal(src.can(method),true,method);
  assert.equal((await src.cmd[method](...args)).ok,true);
  const {t,id,op:actual,...payload}=sent.at(-1);assert.equal(t,'cmd');assert.ok(id>0);assert.equal(actual,op);assert.deepEqual(payload,expected);
}
socket.onmessage({data:JSON.stringify({t:'taught',map:'a',revision:2,map_token:'t',points:[],check:{ok:false}})});
assert.equal(store.taught.map,'a');assert.equal(store.taught.check.ok,false,'源不补造自检通过');
socket.onmessage({data:JSON.stringify({t:'taught',map:'b',revision:1,map_token:'b',points:[]})});
assert.equal(store.taught.map,'a');assert.equal(store.taughtByMap.b.map,'b');
const state={t:'state',profiles:{mapping:'stop',localization:'stop',navigation:'run'},topics:[],maps:[],nodes:[],mapCurrent:'a'};
socket.onmessage({data:JSON.stringify({...state,taughtAvailable:true,taughtNavigation:false})});
assert.equal(store.taughtAvailable,true);assert.equal(store.taughtNavigation,false,'如实保存当前旧栈未启用示教节点');
socket.onmessage({data:JSON.stringify({...state,taughtAvailable:true,taughtNavigation:true})});
assert.equal(store.taughtNavigation,true);
socket.onmessage({data:JSON.stringify({...state,taughtAvailable:false,taughtNavigation:false})});
assert.equal(store.taughtAvailable,false);
socket.onmessage({data:JSON.stringify(state)});
assert.equal(store.taughtAvailable,null);assert.equal(store.taughtNavigation,null,'旧后端缺字段保持未知，沿用旧出发逻辑');
const cleared=[];globalThis.setInterval=()=>42;globalThis.clearInterval=id=>cleared.push(id);
src.cmd.teleop({v:.1,vy:0,w:0});socket.onclose();
assert.deepEqual(cleared,[42],'WS 断线必须清掉遥控续发器，重连不重发旧速度');
console.log('PASS 真实源全部示教操作、会话控制参数与多地图状态隔离');
