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
  constructor(){this.value='';this.textContent='';this.dataset={};this.attributes={};this.classList={toggle(){}};}
  get textContent(){return this._text+(this.children || []).map(child=>child.textContent || '').join('');}
  set textContent(value){this._text=String(value);this.children=[];}
  setAttribute(key,value){this.attributes[key]=String(value);}
  getAttribute(key){return this.attributes[key];}
  replaceChildren(...children){this._text='';this.children=children;if(children.length && 'value' in children[0])this.value=children[0].value;}
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
const src = {can:()=>true,cmd:{taughtPrepare:async map=>{calls.push(['prepare',map]);return {ok:true,msg:'正在准备，保持机器人静止',data:store.taught};},
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
for(const state of ['standing','settling']){
  render(null,'one',state);
  assert.equal(el('taughtPrepare').disabled,true,`${state} 阶段不可重复准备`);
  assert.equal(el('taughtStart').disabled,true,`${state} 阶段不可开始采集`);
  assert.match(el('taughtCheckNote').textContent,/站/,'站起与站稳阶段有可读提示');
}
render('paused','other');assert.equal(el('taughtPrepare').disabled,true,'不允许为另一张草稿地图准备');
render('paused','one','preparing');assert.equal(el('taughtPrepare').disabled,true,'准备中不重复派发');
render('interrupted');el('taughtPrepare').onclick();
const prepare = globalThis.taughtForms.pop();
assert.match(prepare.body,/趴/,'准备说明允许初始趴姿');
assert.match(prepare.body,/自动站起/,'准备说明实际的站起动作');
assert.doesNotMatch(prepare.body,/已站立/,'准备不要求用户已经站立');
await prepare.onOk();
assert.deepEqual(calls,[['prepare','one']],'站起由后端准备流程处理，前端只派发一次准备请求');
assert.doesNotMatch(el('taughtMessage').textContent,/正在准备/,'准备进度由状态面板显示，不能留下过期的请求回执');
assert.equal(el('taughtStart').disabled,true,'没有后端自检通过仍不可新采集');
render('processing');assert.match(el('taughtProcessing').textContent,/平滑/,'结束处理阶段明确显示平滑步骤');
render('finished');store.taught.processing={input_count:3,output_count:81,interpolation:'recorded',max_offset:0,iterations:0,full_sweep_checked:true};panel.render();
assert.match(el('taughtProcessing').textContent,/3.*81/,'完成后显示真实输入与输出点数');
assert.match(el('taughtProcessing').textContent,/无需调整/,'零偏移不宣称改善');

// 已通过的自检应是结果，不能让用户再次派发整套站起/定位准备。
render(null);
const checks = [
  ['standing','站立就绪','趴下或运控状态未知时，请重新准备采集'],
  ['stack','导航与定位服务','需运行定位、导航、速度桥和示教节点'],
  ['map','底图一致','实际/map、所选地图文件和示教节点必须一致'],
  ['measurement','测量与坐标变换','测量与TF按同一时刻，均为新鲜数据'],
  ['localization_quality','定位质量','位置σ 0.030m，测量时刻匹配率 100.0%；需要≤0.3m、≥60%'],
  ['localized','定位来源已确认','换图后需重新定位'],
  ['still','机器人静止','开始和继续采集前保持静止'],
  ['exclusive','任务状态','没有其他导航任务或地图切换']
].map(([key,label,message])=>({key,label,message,ok:true}));
store.taught.check={state:'ready',ok:true,checks,message:'自检通过，可明确开始采集并进入遥控'};
panel.render();
assert.equal(el('taughtPrepare').disabled,true,'自检通过后准备按钮置灰');
assert.equal(el('taughtPrepare').textContent,'已准备','按钮明确说明已完成');
assert.equal(el('taughtCheckNote').textContent,'自检通过','结果使用短标题');
assert.equal(el('taughtCheckResult').dataset.s,'ok','通过结果单独高亮');
assert.equal(el('taughtChecks').children.length,8,'八项状态均保留');
assert.ok(el('taughtChecks').children.every(item=>item.getAttribute('aria-label').includes('通过')),'状态图标有可读名称');
assert.ok(el('taughtChecks').children.every(item=>item.children.some(child=>child.innerHTML?.includes('<svg'))),'状态使用SVG图标');
assert.ok(el('taughtChecks').children.every(item=>item.textContent.length<=8),'默认只显示短检测项名称');
assert.match(el('taughtCheckMessages').textContent,/0.030m.*100.0%/,'折叠详情保留真实数据和条件');
assert.equal(el('taughtCheckProblems').hidden,true,'全部通过时不展开冗长原因');
assert.notEqual(el('taughtCheckDetails').open,true,'通过时详情默认折叠');
assert.match(el('taughtCheckDetailsLabel').textContent,/8\/8/,'显示通过数');
store.ctrl={on:true,state:'Passive',ready:true};panel.render();
assert.equal(el('taughtPrepare').disabled,false,'准备后趴下，允许重新准备而非一直置灰');
assert.equal(el('taughtStart').disabled,true,'站立状态变化后不能用旧自检开始');
assert.notEqual(el('taughtCheckResult').dataset.s,'ok','旧自检不覆盖当前趴姿');
store.ctrl={on:true,state:'Velocity',ready:true};panel.render();
assert.equal(el('taughtPrepare').disabled,true,'站立且检查有效时维持已完成状态');
store.taught.check={...store.taught.check,state:'failed',ok:false,checks:checks.map(item=>({...item,ok:item.key!=='localization_quality'}))};
panel.render();
assert.equal(el('taughtPrepare').disabled,false,'自检失效后允许重新准备');
assert.equal(el('taughtCheckResult').dataset.s,'crit','失败结果有独立颜色');
assert.equal(el('taughtCheckProblems').hidden,false,'失败原因直接显示');
assert.match(el('taughtCheckProblems').textContent,/定位质量.*0.030m/,'问题列表保留具体证据');
assert.doesNotMatch(el('taughtCheckProblems').textContent,/没有其他导航/,'问题列表只展开失败项');
store.taught.check={state:'failed',ok:false,checks,message:'运行服务已切换，请重新准备'};panel.render();
assert.match(el('taughtCheckProblems').textContent,/运行服务已切换/,'整体失效原因直接保留，不能被旧通过项掩盖');
assert.ok(el('taughtChecks').children.every(item=>item.dataset.s==='pending'),'后端作废旧自检后不再显示八个绿灯');
store.link.ok=false;panel.render();
assert.equal(el('taughtPrepare').disabled,true,'断线时不能重新准备');
assert.ok(el('taughtChecks').children.every(item=>item.dataset.s==='pending'),'断线后旧绿灯不充当当前通过状态');
console.log('PASS 准备门槛、草稿恢复、平滑结果、紧凑自检状态与完成后置灰');
