// 雷达无数据提醒（v1.0.8）：档位跑稳后 /scan 断了 → web.log ERROR + 持续异常 lidar_lost（不存包）+ 网络诊断；
// 恢复 → INFO + 异常结束；断着的时候停档位 → 异常收掉。只能对本地假 ROS 运行（会切档位）：
//   D=$(mktemp -d); mkdir -p $D/maps; cp ../data/maps/* $D/maps/
//   printf '{"lidar_configs":[{"ip":"10.9.9.9"}]}' > $D/mid360.json
//   GO2W_DATA=$D GO2W_LIDAR_GRACE=3 GO2W_LIDAR_STALE=2 GO2W_LIDAR_CONFIG=$D/mid360.json GO2W_LIDAR_ARP=$D/arp dev/run.sh
//   GO2W_DATA=$D node dev/test_lidar_watch.mjs [ws://127.0.0.1:8080/ws]
import { writeFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
const URL_ = process.argv[2] || 'ws://127.0.0.1:8080/ws';
const DATA = process.env.GO2W_DATA;
if (!DATA) { console.error('要设置 GO2W_DATA（和 run.sh 用同一个数据目录）'); process.exit(2); }
const SCAN_OFF = process.env.GO2W_FAKE_SCAN_OFF || '/tmp/go2w_fake_scan_off';
const ARP = join(DATA, 'arp');
const arp = flags => writeFileSync(ARP, 'IP address       HW type     Flags       HW address            Mask     Device\n'
  + `10.9.9.9         0x1         ${flags}         ${flags === '0x2' ? '8c:58:23:66:78:80' : '00:00:00:00:00:00'}     *        eth0\n`);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const rm = f => { try { unlinkSync(f); } catch {} };

const ws = new WebSocket(URL_); ws.binaryType = 'arraybuffer';
let state = null; const logs = []; const incs = new Map(); const acks = new Map(); let seq = 0;
ws.onmessage = e => { if (typeof e.data !== 'string') return; const m = JSON.parse(e.data);
  if (m.t === 'state') state = m;
  if (m.t === 'logs') for (const it of m.items) logs.push(it);
  if (m.t === 'incidents') for (const it of m.items) incs.set(it.id, it);
  if (m.t === 'incident') incs.set(m.item.id, m.item);
  if (m.t === 'ack') { acks.get(m.id)?.(m); acks.delete(m.id); } };
const timer = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping","ts":0}'); }, 500);
const cmd = (op, a = {}) => new Promise(r => { const id = ++seq; acks.set(id, r); ws.send(JSON.stringify({ t: 'cmd', id, op, ...a })); });
const until = async (fn, ms, what) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(100); } throw new Error('超时：' + what); };
const results = []; const ok = (name, cond, extra = '') => results.push(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  · ' + extra : ''}`);
const logAt = (re, lvl) => logs.find(it => re.test(it[2]) && (!lvl || it[0] === lvl || it[1] === lvl || String(it).includes(lvl)));
const lidarIncs = () => [...incs.values()].filter(i => i.type === 'lidar_lost');

try {
  rm(SCAN_OFF); arp('0x0');
  await new Promise(r => ws.onopen = r);
  await until(() => state, 8000, '首个状态');
  await cmd('set_mode', { mode: 'mapping' });
  await until(() => state.profiles.mapping === 'run' && !state.busy, 30000, '建图起来');

  // 1. 档位刚起来的宽限期内断激光：不报
  writeFileSync(SCAN_OFF, '1');
  await sleep(1500);
  ok('宽限期内不报', !logAt(/雷达无数据/) && state.lidar == null);

  // 2. 断够了：ERROR 日志 + 持续异常（不存包）+ 网络诊断（ARP 不通）
  await until(() => logAt(/雷达无数据：.*ARP 不通/), 20000, '雷达无数据 + 诊断');
  ok('web.log 写了 ERROR「雷达无数据」', !!logAt(/雷达无数据：\/scan 已经 \d+ s 没有数据/));
  ok('诊断：ARP 不通 → 提示供电 / 网线', !!logAt(/雷达 10\.9\.9\.9 在网络上没有回应（ARP 不通）/));
  await until(() => lidarIncs().length >= 1, 5000, '异常记录');
  let inc = lidarIncs().at(-1);
  ok('记了持续中的「雷达无数据」异常', inc.ongoing && inc.label === '雷达无数据', inc.msg);
  ok('这类异常不存回溯包', inc.bag?.state === 'skipped', inc.bag?.msg);
  await until(() => /ARP 不通/.test(lidarIncs().at(-1).msg), 5000, '异常里带上诊断');
  ok('异常消息里带了诊断结论', true);
  await until(() => /ARP 不通/.test(state.lidar?.msg || ''), 3000, '状态里的 lidar 带诊断');   // 状态 2 Hz 推送，等下一帧
  ok('状态里有 lidar（网页红条用）', true, state.lidar.msg);
  const n1 = logs.filter(it => /雷达无数据/.test(it[2])).length;
  await sleep(3000);
  ok('断着时不重复刷日志', logs.filter(it => /雷达无数据/.test(it[2])).length === n1);

  // 3. 恢复：INFO + 异常结束 + 状态清掉
  rm(SCAN_OFF);
  await until(() => logAt(/雷达已恢复/), 10000, '雷达恢复');
  await until(() => !lidarIncs().at(-1).ongoing && state.lidar == null, 5000, '异常结束、状态清掉');
  ok('恢复：日志 + 异常结束 + 状态清掉', lidarIncs().at(-1).dur > 0 && state.lidar == null, `持续 ${lidarIncs().at(-1).dur} s`);

  // 4. 网络是通的但没点云（ARP 有地址）
  arp('0x2'); writeFileSync(SCAN_OFF, '1');
  await until(() => logAt(/雷达 10\.9\.9\.9 网络是通的，但没有点云/), 20000, '诊断：网络通');
  ok('诊断：网络通 → 提示看驱动日志', true);

  // 5. 断着时停档位：异常收掉，提醒结束
  await cmd('set_mode', { mode: null });
  await until(() => state.profiles.mapping === 'stop' && !state.busy, 30000, '建图停下');
  await until(() => logAt(/档位已停，雷达无数据的提醒结束/), 5000, '停档位收尾');
  ok('停档位：异常结束、状态清掉', !lidarIncs().at(-1).ongoing && state.lidar == null);
  rm(SCAN_OFF);
} catch (e) {
  ok(String(e.message || e), false);
} finally {
  rm(SCAN_OFF); clearInterval(timer); ws.close();
}
for (const r of results) console.log(r);
const fails = results.filter(x => x.startsWith('FAIL')).length;
console.log(fails ? `\n${fails} 项失败` : `\n全部通过（${results.length} 项）`);
process.exit(fails ? 1 : 0);
