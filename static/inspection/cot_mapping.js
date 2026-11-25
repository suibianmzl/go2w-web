export const INSPECTION_NODES = [
  { code:'01', name:'起点' },
  { code:'02', name:'电动车存放处' },
  { code:'03', name:'人行步道' },
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
      { text:'定点巡逻中（30s）', delayMs:700 },
      { text:'巡逻结束，出发前往【人行步道】', delayMs:30000 },
    ],
  },
  {
    note:'position_footway',
    cruiseName:'人行步道',
    nodeIndex:1,
    lines:[
      '检测到阻塞（> 10s），无通行空间...',
      { text:'推理其他可通行路径...', delayMs:30000 },
      '找到可用路径，正在翻越台阶...',
    ],
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
      '已到达巡逻点位【人行步道】',
      '定点巡逻中（30s）',
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
