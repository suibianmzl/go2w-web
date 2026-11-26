import assert from 'node:assert/strict';

import {
  createFootwayBlockageWatch,
  FOOTWAY_BLOCK_MS,
  FOOTWAY_BLOCK_SPEED,
  footwayLowSpeedActive,
} from '../static/inspection/blockage_watch.js';
import { COT_BY_NOTE } from '../static/inspection/cot_mapping.js';

const task = {
  type:'route',
  status:'active',
  routeId:'route-1',
  loop:1,
  point:5,
  back:false,
};
const pose = v => ({ valid:true, v });
const watch = createFootwayBlockageWatch();

assert.deepEqual(COT_BY_NOTE.get('position_footway').lines, [], '阻塞 CoT 不应静态播报');

assert.equal(watch.observe(task, 'position_monitoring', pose(0), 0), null, '其他点位不触发');
assert.equal(watch.observe(task, 'position_footway', pose(0.29), 1000), null, '开始低速度计时');
for (let now = 2000; now <= 10000; now += 1000){
  assert.equal(watch.observe(task, 'position_footway', pose(0.29), now), null);
}
assert.equal(watch.observe(task, 'position_footway', pose(0.29), 1000 + FOOTWAY_BLOCK_MS - 1), null);
assert.equal(
  watch.observe(task, 'position_footway', pose(0.29), 1000 + FOOTWAY_BLOCK_MS),
  'route-1:1:5',
  '连续 10 秒低速度后触发一次',
);
assert.equal(watch.observe(task, 'position_footway', pose(0.29), 12000), null, '同一点位不重复触发');

assert.equal(watch.observe(task, 'position_footway', pose(FOOTWAY_BLOCK_SPEED), 13000), null);
assert.equal(watch.observe(task, 'position_footway', pose(0.29), 14000), null, '速度恢复后重新计时');
assert.equal(watch.observe(task, 'position_footway', pose(0.29), 23000), null);
assert.equal(watch.observe(task, 'position_footway', pose(0.29), 24000), null, '同一点位仍只报一次');

const nextLoop = { ...task, loop:2 };
assert.equal(watch.observe(nextLoop, 'position_footway', pose(0.29), 25000), null, '下一圈重新计时');
for (let now = 26000; now <= 34000; now += 1000){
  assert.equal(watch.observe(nextLoop, 'position_footway', pose(0.29), now), null);
}
assert.equal(
  watch.observe(nextLoop, 'position_footway', pose(0.29), 35000),
  'route-1:2:5',
  '下一圈可以再次触发',
);

watch.resetRun();
assert.equal(watch.observe({ ...task, back:true }, 'position_footway', pose(0), 0), null, '返回阶段不触发');
assert.equal(watch.observe({ ...task, status:'canceled' }, 'position_footway', pose(0), 100), null, '非执行任务不触发');

const gapped = createFootwayBlockageWatch();
assert.equal(gapped.observe(task, 'position_footway', pose(0.29), 0), null);
assert.equal(gapped.observe(task, 'position_footway', pose(0.29), 12000), null, 'pose 中断后重新计时');
for (let now = 13000; now <= 21000; now += 1000){
  assert.equal(gapped.observe(task, 'position_footway', pose(0.29), now), null);
}
assert.equal(gapped.observe(task, 'position_footway', pose(0.29), 22000), 'route-1:1:5');
assert.equal(footwayLowSpeedActive(task, 'position_footway', pose(0.29)), true);
assert.equal(footwayLowSpeedActive(task, 'position_footway', pose(0.3)), false);
assert.equal(footwayLowSpeedActive({ ...task, status:'canceled' }, 'position_footway', pose(0.1)), false);

console.log('PASS inspection blockage: position_footway low speed is emitted once after 10s');
