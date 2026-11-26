// 点限速、2 rad/s 和任务终止。只能对 dev/run.sh 的假 ROS 服务运行：node dev/test_route_limits.mjs [ws://127.0.0.1:8080/ws]
import assert from 'node:assert/strict';
import { setTimeout as sleep } from 'node:timers/promises';

const url = process.argv[2] || 'ws://127.0.0.1:8080/ws';
assert.match(url, /^ws:\/\/(127\.0\.0\.1|localhost):\d+\/ws$/);
const map = 'lab_20261021';
const points = [
  { x:0,y:0,th:0,vmax:.3 },
  { x:.9,y:0,th:0,vmax:.2 },
  { x:1.8,y:0,th:0,vmax:.4 },
  { x:2.7,y:0,th:0 },
];
async function client(){
  const ws = new WebSocket(url), replies = new Map(), tasks = [];
  const c = { ws, tasks, ping: true };
  let id = 0;
  ws.binaryType = 'arraybuffer';
  ws.addEventListener('message', ({ data }) => {
    if (typeof data !== 'string') return;
    const m = JSON.parse(data);
    if (m.t === 'ack') replies.set(m.id, m);
    if (m.t === 'state') c.state = m;
    if (m.t === 'routes') c.routes = m.items;
    if (m.t === 'task'){
      c.task = m.task;
      if (m.task?.type === 'route') tasks.push(m.task);
    }
    if (m.t === 'pose') c.pose = m;
  });
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  const ping = () => {
    if (c.ping && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t:'ping', ts:0 }));
  };
  ping();
  const timer = setInterval(ping, 350);
  c.close = () => { clearInterval(timer); ws.close(); };
  c.cmd = async (op, args = {}) => {
    const key = ++id;
    ws.send(JSON.stringify({ t:'cmd', id:key, op, ...args }));
    await until(() => replies.has(key), op);
    const reply = replies.get(key);
    replies.delete(key);
    return reply;
  };
  return c;
}

async function until(check, label, timeout = 5000){
  const end = Date.now() + timeout;
  while (!check()){
    assert.ok(Date.now() < end, `超时：${label}`);
    await sleep(30);
  }
}


const c = await client(), observer = await client();
try {
  await until(() => c.state?.mapCurrent === map && c.pose?.frame === 'map', '地图和定位',20000);
  const cmd=async (op,args={})=>{const r=await c.cmd(op,args);assert.equal(r.ok,true,`${op}: ${r.msg}`);return r;};
  const route=(await cmd('route_save',{map,name:'逐段限速验收',points,opts:{vmax:.5,wmax:2,through:true}})).data;
  assert.deepEqual(route.points.map(p=>p.vmax),[.3,.2,.4,undefined]);
  await cmd('initial_pose',{x:-1,y:0,th:0});
  await cmd('arm',{on:true});
  await cmd('route_start',{route_id:route.id});
  await until(()=>c.task?.name===route.name && c.task.status==='active' && c.state.params.wmax===2,'任务上限写入',10000);
  await until(()=>c.task?.point===1 && c.state.params.vmax===.3,'首次接近首站限速',5000);
  await until(()=>c.task?.point===2 && c.state.params.vmax===.2,'起终点最小限速',15000);
  assert.equal((await c.cmd('set_param',{key:'vmax',value:1.2})).ok,false,'临时调速不能绕过点限速');
  assert.equal((await c.cmd('route_terminate',{route_id:route.id})).ok,false,'活动任务不能清断点');
  await until(()=>c.task?.point===3,'经过第二站',15000);
  await cmd('cancel');
  await until(()=>observer.routes?.find(r=>r.id===route.id)?.resume?.point===3,'观察方收到断点');
  const result=await cmd('route_terminate',{route_id:route.id});
  assert.match(result.msg,/路线保留/);
  await until(()=>observer.routes?.find(r=>r.id===route.id)?.resume===null,'观察方收到终止');
  assert.deepEqual(observer.routes.find(r=>r.id===route.id).points,route.points,'终止后保留路线');
  assert.equal((await c.cmd('route_start',{route_id:route.id,resume:'continue'})).ok,false,'终止后无法继续旧进度');
  await cmd('route_start',{route_id:route.id});
  await until(()=>c.task?.name===route.name && c.task.status==='active','可以重新出发',5000);
  await until(()=>c.task?.status==='succeeded','按点限速跑完',40000);
  await until(()=>c.state.params.vmax===.5,'完成后恢复任务最大 vx');
  console.log('PASS 真实WebSocket：点限速持久化、首次/站间限速、wz=2、活动保护、终止广播与重新出发');
  await cmd('route_delete',{route_id:route.id});
} finally {
  await c.cmd('cancel').catch(()=>{});
  c.close();observer.close();
}
