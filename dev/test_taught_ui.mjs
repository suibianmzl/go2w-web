// 主任务面板真实 render 的门槛测试；DOM/对话框只提供容器，不运行浏览器或运动数据源。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const util = moduleUrl(await readFile(new URL('../static/js/util.js',import.meta.url),'utf8'));
const geometry = moduleUrl(await readFile(new URL('../static/js/taught_geometry.js',import.meta.url),'utf8'));
const ui = moduleUrl('export function toast(){}; export function stillCheck(){}; export function confirmBox(){}; export function formBox(value){globalThis.taughtForms.push(value);}');
const source = (await readFile(new URL('../static/js/taught.js',import.meta.url),'utf8'))
  .replace('./util.js',util).replace('./ui.js',ui).replace('./taught_geometry.js',geometry);
const elements = new Map();
class Element {
  constructor(){this.value='';this.textContent='';this.dataset={};this.classList={toggle(){}};}
  setAttribute(){}
  replaceChildren(...children){this.children=children;if(children.length && 'value' in children[0])this.value=children[0].value;}
  appendChild(child){this.children??=[];this.children.push(child);}
}
const el = id => {if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
globalThis.document = {querySelector:s=>el(s.slice(1)),querySelectorAll:()=>[],createElement:()=>new Element()};
globalThis.window = {addEventListener(){}};
globalThis.Option = class {constructor(label,value){this.textContent=label;this.value=value;}};
globalThis.taughtForms=[];
const { setupTaught } = await import(moduleUrl(source));
const store = {source:'ros',uiTask:'nav',maps:[{name:'one'}],mapCurrent:'one',taughtMap:'one',taughtByMap:{},link:{ok:true},
  profiles:{mapping:'stop'},task:{type:'idle'},estop:false,armed:false};
el('taughtMode').value='replace';
const calls=[];
const src = {can:()=>true,cmd:{taughtPrepare:async map=>{calls.push(['prepare',map]);return {ok:true,data:store.taught};},
  arm:()=>calls.push(['arm']),standUp:()=>calls.push(['stand'])}};
const panel = setupTaught(store,src,()=>{});
function render(state,map='one',checkState='idle'){
  store.taught={map:'one',revision:1,map_token:'token',state:'ready',executable:true,points:[{x:1,y:1},{x:2,y:1}],length:1,
    check:{state:checkState,ok:false,checks:[]},capture:state?{session_id:'session-1',map,state,mode:'replace',points:[],sample_count:0,length:0,can_resume:false}:null};
  store.taughtByMap.one=store.taught;panel.render();
}
for(const state of ['paused','interrupted']){
  render(state);
  assert.equal(el('taughtPrepare').disabled,false,`${state} 同图草稿可以重新准备`);
  assert.equal(el('taughtMapSel').disabled,true,'草稿选图保持锁定');
  assert.equal(el('taughtMode').disabled,true,'草稿模式保持锁定');
}
for(const state of ['recording','processing','failed']){render(state);assert.equal(el('taughtPrepare').disabled,true,state);}
render('failed');
assert.equal(el('taughtRecover').hidden,false,'校验失败草稿允许送到独立编辑器修复');
assert.match(el('taughtRecover').href,/map=one&capture=session-1/,'恢复链接带原地图与会话');
assert.equal(el('taughtResume').hidden,true,'校验失败不开放继续采样');
assert.equal(el('taughtResume').disabled,true,'can_resume=false 不可继续');
assert.equal(el('taughtPrepare').disabled,true,'失败草稿不重新准备');
for(const state of ['recording','processing','finished','abandoned']){render(state);assert.equal(el('taughtRecover').hidden,true,`${state} 不展示编辑恢复`);}
render('paused','other');assert.equal(el('taughtPrepare').disabled,true,'不允许为另一张草稿地图准备');
render('paused','one','preparing');assert.equal(el('taughtPrepare').disabled,true,'准备中不重复派发');
render('interrupted');el('taughtPrepare').onclick();
await globalThis.taughtForms.pop().onOk();
assert.deepEqual(calls,[['prepare','one']],'准备只请求加载定位，不自动站起或解锁');
assert.equal(el('taughtStart').disabled,true,'没有后端自检通过仍不可新采集');
console.log('PASS 暂停/中断同图准备、保留草稿锁与禁止自动站起解锁');
