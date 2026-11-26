import assert from 'node:assert/strict';

import {
  COT_BY_NOTE,
  INSPECTION_NODES,
  cotEventFamilyKey,
  inspectionLocalizationHints,
  inspectionNodeIndexForTask,
  inspectionTaskRestarted,
} from '../static/inspection/cot_mapping.js';

const points = [
  { note:'position_starting' },
  { note:'' },
  { note:'position_monitoring' },
  { note:'' },
  { note:'position_footway' },
  { note:'' },
  { note:'' },
  { note:'position_trail' },
  { note:'position_end' },
];

const task = (point, extra = {}) => ({
  type:'route',
  status:'active',
  routeId:'route-1',
  point,
  pointNote:'',
  back:false,
  ...extra,
});

assert.equal(inspectionNodeIndexForTask(task(1), points), 0, '起点直接映射');
assert.equal(inspectionNodeIndexForTask(task(3), points), 1, 'position_monitoring 映射');
assert.equal(inspectionNodeIndexForTask(task(5), points), 1, 'position_footway 映射');
assert.equal(inspectionNodeIndexForTask(task(6), points), 1, 'footway 后空点位沿用最近节点');
assert.equal(inspectionNodeIndexForTask(task(7), points), 1, '中途接续继续沿用最近节点');
assert.equal(inspectionNodeIndexForTask(task(8), points), 2, '下一个已映射点及时切换');
assert.equal(inspectionNodeIndexForTask(task(1, { back:true }), points), 3, '返回阶段映射最后节点');
assert.equal(inspectionNodeIndexForTask(task(2, { pointNote:'position_lobby' }), points), 0, '当前备注优先');
assert.equal(inspectionNodeIndexForTask(task(1, { pointNote:'position_starting' }), []), 0, '无路线点时保留当前备注映射');
assert.equal(inspectionNodeIndexForTask(task(6), []), -1, '无路线点且无当前备注时不能猜测');
assert.equal(inspectionNodeIndexForTask({ routeId:'route-1', point:7 }, points), 1, '异常任务缺少 type 时仍可归属节点');

const dwellLine = COT_BY_NOTE.get('position_monitoring').lines.find(line => line.timer);
assert.equal(dwellLine?.timer?.type, 'countdown');
assert.equal(dwellLine?.timer?.from, 30);
assert.equal(dwellLine?.timer?.format(17), '定点巡逻中（17s）');

const trailDwellLine = COT_BY_NOTE.get('position_trail').lines.find(line => line.timer);
assert.equal(trailDwellLine?.timer?.type, 'countdown');
assert.equal(trailDwellLine?.timer?.from, 30);
assert.equal(trailDwellLine?.timer?.format(17), '定点巡逻中（17s）');
assert.equal(INSPECTION_NODES[2].name, '大厦出口');
assert.equal(COT_BY_NOTE.get('position_trail').lines[0], '已到达巡逻点位【大厦出口】');
assert.deepEqual(
  inspectionLocalizationHints({ points:[{ x:1.2, y:3.4 }] }),
  { hints:[{ x:1.2, y:3.4, label:'第 1 站' }] },
);
assert.deepEqual(inspectionLocalizationHints({ points:[] }), {});
assert.equal(
  cotEventFamilyKey({ routeId:'route-1', loop:1, nodeIndex:2, note:'position_trail', point:9 }),
  cotEventFamilyKey({ routeId:'route-1', loop:1, nodeIndex:2, note:'position_trail', point:10 }),
  '同一轮同一固定节点的 CoT 归为一组，点数变化不产生第二次倒计时',
);
assert.equal(inspectionTaskRestarted(
  { status:'active', routeId:'route-1', elapsed:42 },
  { status:'active', routeId:'route-1', elapsed:1 },
), true);

console.log('PASS inspection state: CoT dedupe, localization hints and both dwell timers');
