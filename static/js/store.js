// 界面只读这个 store；模拟源和 ROS 源负责把它填满。字段为 null 表示“没有数据”，界面显示 --
export function createStore(){
  return {
    source: 'mock',                 // 'mock' | 'ros'
    link: { ok: false, ms: null },  // 浏览器 ↔ 后端
    mapGrid: null,                  // 网页收到的原始地图格子 { grid: Uint8Array（0..100，255 未知，行 0 在 origin 一侧）, w, h, res, ox, oy, oyaw }；地图编辑用
    webVersion: null,               // 机器人上（后端所在 release）的网页版本；和页面自己的 version.json 比
    fixed: 'map',                   // 位姿和 scan 所在坐标系（没有 map TF 时后端退回 odom）
    // 地图：img 画布覆盖世界坐标 [ox, ox+w] × [oy, oy+h]（米）
    map: null,                      // { img, w, h, ox, oy, res, name, cells }
    costmap: null,                  // 同上，全局代价地图
    lcostmap: null,                 // 局部代价地图（真机：odom 下的滚动窗口，带 yaw）；没有时用全局的裁一块（模拟模式）
    costAt: null,                   // (x, y) => 0..254，用于光标读数和目标校验
    robot: { valid: false, x: 0, y: 0, th: 0, v: 0, vy: 0, w: 0 },
    cmd: { v: 0, vy: 0, w: 0 },
    // 速度诊断（仅 ROS）：{ wall: [vx, vy, wz] 按真实时间, stamp: [...] 按消息时间戳, rate: 时间戳走速 / 真实时间 }
    speedCheck: null,
    imu: { roll: null, pitch: null },
    scan: new Float32Array(0),      // [x, y, ...]（fixed 坐标系）
    cloud: [],                      // 按高度分桶，每桶 [x, y, ...]
    plan: null,                     // { pts: [[x,y]], s: [], total }
    planIdx: 0,
    localPlan: null, mppi: [], particles: new Float32Array(0),
    trail: [],
    goal: null,
    routes: [],
    routeDraft: { id: null, map: null, points: [], confirmed: false },
    routeCheck: null,
    routeReplace: null,             // 正在重新选位置的站（下标）；null = 地图上点击是加新站
    // 每图只保留一条活动示教；采集草稿单独存在文档 capture 中，不与多站导航任务混用。
    taught: null, taughtByMap: {}, taughtMap: null, taughtRange: null,
    taughtNav: null,   // 「启用示教导航」开关 { on, forced, installed }
    taughtAvailable: null, taughtNavigation: null, // 未提供字段时保持未知，兼容旧后端；不得凭空判定节点就绪。
    // angle：地图显示方向（只转画面）。按地图名存在后端，见 mapViews；points / target：「沿墙选两点」
    alignment: { points: [], angle: 0, target: null, source: null },
    mapViews: {},
    mapLabels: {},   // 地图名 → 显示名（可中文）
    keepouts: {},    // 禁行区（v1.1.8）：地图名 → [{ kind: line/zone, pts, name? }]（世界坐标）
    keepoutState: null, // 导航里的禁行层：{ ready（有 filter_mask_server）, live（加载的是哪张图）, loading }
    mapSaving: false,
    mapBusy: false,
    // 续建（docs/地图续建.md）：{active, base, name, source, pose, note, warn, check:{ok,fit,...}, elapsed}
    cont: null,
    contPaused: false,
    contResult: null,
    // task.phase: plan | align | follow | final | dwell | arrived
    task: { type: 'idle' },
    armed: null, estop: false, odomOk: false,
    loc: { running: false, std: null, yaw: null, match: null },
    // 运行档位：mode 三选一，bag 可叠加；patrol 只用来识别命令行启动的 patrol.py
    profiles: { mapping: 'stop', localization: 'stop', navigation: 'stop', patrol: 'stop', bag: 'stop', bridge: 'stop' },
    since: {},                      // 档位启动时间（秒）
    nodes: [],                      // [{ id, name, up }]
    sys: { cpu: null, mem: null, memTotal: null },
    topics: [],                     // [{ name, type, hz, alive, d0 }]
    params: { vmax: .45, wmax: 1.2, tmo: .3 },
    rollback: { on: true, run: false, saving: false, seconds: 20 },
    bagLive: [], bagSegment: null,  // 正在写的包（列表里的名字）；分段录包的段长（秒）
    maps: [], bags: [], uploads: {},   // uploads：包名 → { state: run/done/fail, pct, msg }
    odo: null, battery: null, motorTemp: null, motorInfo: null,
  };
}
