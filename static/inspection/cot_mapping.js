export const INSPECTION_NODES = [
  { code:'01', name:'起点' },
  { code:'02', name:'电动车存放处' },
  { code:'03', name:'大厦出口' },
  { code:'04', name:'返回起点' },
];

// 云端按机器狗 task.pointNote 上报的备注枚举映射 CoT。
// nodeIndex 对应上方固定的 4 个巡检节点，巡检节点名称不随巡航点名称变化。
export const COT_NOTE_DEFINITIONS = [
  {
    note:'position_starting',
    cruiseName:'起点',
    nodeIndex:0,
    lines:['已获取导航路径，立即前往【电动车存放处】'],
  },
  {
    note:'position_entrance',
    cruiseName:'大厅门禁',
    nodeIndex:0,
    lines:['已识别门禁打开，正在通过'],
  },
  {
    note:'position_lobby',
    cruiseName:'大厦正门内',
    nodeIndex:0,
    lines:['已识别大门打开，正在通过'],
  },
  {
    note:'position_step',
    cruiseName:'下台阶',
    nodeIndex:0,
    lines:['识别到台阶，正在通过'],
  },
  {
    note:'position_monitoring',
    cruiseName:'电动车棚',
    nodeIndex:1,
    lines:[
      '已到达巡逻点位【电动车存放处】',
      {
        text:'定点巡逻中（30s）',
        delayMs:700,
        timer:{
          type:'countdown',
          from:30,
          to:0,
          intervalMs:1000,
          formatKey:'dwell',
          format:seconds => `定点巡逻中（${seconds}s）`,
        },
      },
      { text:'巡逻结束，出发前往【大厦出口】', delayMs:30000 },
    ],
  },
  {
    note:'position_footway',
    cruiseName:'大厦出口',
    nodeIndex:1,
    // 阻塞文案由巡检页根据实时速度条件生成，避免无阻塞时固定播报。
    lines:[],
  },
  {
    note:'position_faregate',
    cruiseName:'步道闸机',
    nodeIndex:1,
    // 暂时停用该备注的 CoT 输出，仅保留巡检节点归属。
    lines:[],
  },
  {
    note:'position_trail',
    cruiseName:'门前步道',
    nodeIndex:2,
    lines:[
      '已到达巡逻点位【大厦出口】',
      {
        text:'定点巡逻中（30s）',
        delayMs:700,
        timer:{
          type:'countdown',
          from:30,
          to:0,
          intervalMs:1000,
          formatKey:'dwell',
          format:seconds => `定点巡逻中（${seconds}s）`,
        },
      },
      { text:'巡逻结束，返回待机位置', delayMs:30000 },
    ],
  },
  {
    note:'position_end',
    cruiseName:'终点',
    nodeIndex:3,
    lines:['巡逻结束，返回待机位置'],
  },
];

export const COT_BY_NOTE = new Map(
  COT_NOTE_DEFINITIONS.map(definition => [definition.note, definition]),
);

export const COT_BY_NAME = new Map(
  COT_NOTE_DEFINITIONS.map(definition => [definition.cruiseName, definition]),
);

export function inspectionNodeIndexForTask(task, points = []){
  if (!task || (task.type && task.type !== 'route')) return -1;
  if (task.back) return COT_BY_NOTE.get('position_end')?.nodeIndex ?? -1;

  const directNote = String(task.pointNote || '').trim().toLowerCase();
  const direct = directNote ? COT_BY_NOTE.get(directNote) : null;
  if (direct) return direct.nodeIndex;

  const current = Number(task.point) - 1;
  if (!Number.isInteger(current) || current < 0) return -1;
  const mappedAt = index => {
    const note = String(points[index]?.note || '').trim().toLowerCase();
    return note ? COT_BY_NOTE.get(note) || null : null;
  };

  // 中途接续或途经未定义 CoT 的中间点时，沿用最近一个已映射节点。
  for (let index = Math.min(current, points.length - 1); index >= 0; index -= 1){
    const definition = mappedAt(index);
    if (definition) return definition.nodeIndex;
  }
  // 起点附近还没有经过已映射节点时，提前归入下一个已映射节点。
  for (let index = current + 1; index < points.length; index += 1){
    const definition = mappedAt(index);
    if (definition) return definition.nodeIndex;
  }
  return -1;
}

export function inspectionLocalizationHints(route){
  const point = route?.points?.[0];
  if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) return {};
  return { hints:[{ x:point.x, y:point.y, label:'第 1 站' }] };
}

export function cotEventFamilyKey(event){
  return [
    event?.routeId || '',
    Number(event?.loop) || 1,
    event?.back ? 'back' : 'forward',
    event?.nodeIndex,
    event?.note || '',
  ].join(':');
}

export function inspectionTaskRestarted(previous, next){
  if (next?.status !== 'active' || previous?.status !== 'active' || next.routeId !== previous.routeId){
    return false;
  }
  const elapsed = Number(next.elapsed);
  const previousElapsed = Number(previous.elapsed);
  return Number.isFinite(elapsed) && Number.isFinite(previousElapsed) && elapsed + 2 < previousElapsed;
}
