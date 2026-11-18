# 示教采集与地图编辑工作区

## 交付与边界

右上任务增加「示教采集」，按选图→加载定位→自检→遥控采集→校验保存运行，支持暂停、结束、浏览器或后端中断后的人工恢复、末尾追加、整条替换、选中区间替换。中央地图显示原参考与当前采样，左侧每地图仅一条示教图层。另提供`/taught-editor?map=地图名`独立离线编辑页面，能创建、拖点、插点、删点、撤销/重做和保存。编辑离线文件不切换机器人正在使用的地图。

本轮仅本地开发测试，不连接真机启动/解锁/运动，不部署、不push、不启动训练。产品中的运动继续由用户明确遥控操作触发；准备和自检不自动开走。

## 数据与运行衔接

- 地图`<name>.yaml`是活动数据权威源，增加唯一`go2w_taught_route`字段，保留原image/resolution/origin等Nav2字段；PGM图像保持标准格式。无字段且无旧版有效默认即无示教，不以文件时间猜选。兼容迁移仅针对原RouteStore里未标为bundle派生缓存的有效默认：读取所选地图时一次性写入YAML；派生缓存不能在字段消失后被当作旧路线重新导入。
- 字段值为包络`{schema:1, revision:正整数, map_id:地图指纹, reference:处理后的route字典, source:来源}`。每图一个包络，历史修订、采集日志在`<name>.taught/`，不作为第二条活动轨迹。
- 保存先验证底图版本和路线修订，再用现有process_route检查完整足迹、平滑与连续性，通过后原子写入地图YAML。失败不覆盖原有效轨迹。原始数据、手绘几何与经检查的恢复连接分别标注来源。
- 主导航包增加共享map_bundle模块；网页直接复用算法，不复制一份平滑/碰撞算法。导航启动传入map_yaml，普通目标接受时从选定文件读取固定快照，再生成不可变运行缓存。未设置map_yaml的旧启动方式继续使用原默认；显式选择地图后的无字段不回退派生缓存。CLI录制完成时若已设置map_yaml且文件地图身份匹配，也发布同一包络。
- 机器人换图时同步更新示教节点map_yaml参数。离线编辑不调用LoadMap，不发布初始位姿或速度。
- 准备使用navigation栈（仅加载定位和控制服务，无活动导航目标），通过已有GO2W_TAUGHT_ROUTE=true启用普通导航分流；站立和遥控仍由用户明确操作。自检沿用当前界面的定位质量门槛：位置标准差≤0.3m、scan匹配率≥60%，另检查地图、同测量时刻TF、测量/接收新鲜度、定位来源已确认和静止。未知项不按通过处理。
- 暂停/断线即保留草稿并停止采样；重连不自动恢复。继续前重做自检，当前位姿需接近原断点（0.2m内）并通过短连接足迹检查。原始连续段断口保留，获准短连接只加入处理几何，不伪装为实测。
- append在旧末端接入；replace_segment须选start/end两个索引，起止都接近锚点，旧区间外固定不改变。replace整体替换也等新轨迹校验通过后才生效。

## 共享核心接口（go2w仓库）

`go2w_taught_route.map_bundle`：

```python
read_bundle(yaml_path, expected_map_id=None) -> dict | None
inspect_bundle(yaml_path) -> dict | None  # 编辑显示旧几何；不能作为导航通过证明
map_source_token(yaml_path) -> str  # 忽略示教字段，对地图配置+图像做一致性标识
load_map_asset(yaml_path) -> dict  # 标准PGM/trinary，grid二维int8，res，origin=[x,y,yaw]
map_identity(grid, resolution, origin=(0,0,0), frame='map') -> str
write_bundle(yaml_path, reference, *, expected_revision, expected_token, source='web') -> dict
prepare_bundle_route(store, yaml_path, expected_map_id, *, activate=False) -> dict | None
```

write返回上述包络，保留地图其他原文、用文件锁和原子保存、写历史；reference必须executable且map_id正确。map_identity规范化为实际ROS float32分辨率与四元数往返yaw，防止离线/ROS指纹不一致。prepare生成/复用不可变导航缓存，带bundle摘要/版本来源；任务快照读取不得倒退全局默认，activate只在明确发布当前有效文件时用。

TaughtRouteNode声明可更新`map_yaml`参数；`route_for_map(map_id)`按指定文件严格准备包络。无字段时仅符合上述限定迁移规则的旧原始默认可一次迁入，否则走普通导航；不能回退派生缓存。仅未设置map_yaml的旧接口保留原RouteStore默认选择。损坏和错图明确拒绝。AutomaticNavigation接受时调用这个入口，原已接受地图/版本固定规则保留。navigation.launch和simulation.launch传文件名，数据根与GO2W_DATA一致。

## 网页协议（go2w-web仓库）

已有WS ack格式保留`{t:'ack',id,ok,msg,data}`。新增操作：

- `taught_get {map}` → 文档；`taught_save {map,expected_revision,map_token,points}` → 保存后的文档，120s预算，校验在工作线程。
- `taught_prepare {map}` → 立即回准备状态，后台加载/定位/自检；`taught_check {map}`重试自检。
- `taught_start {map,mode:'replace'|'append'|'replace_segment',expected_revision,map_token,start_index?,end_index?}`。
- `taught_pause {session_id}`、`taught_resume {session_id}`、`taught_finish {session_id}`、`taught_abandon {session_id}`、`taught_status {map?}`。旧页面的过期session命令拒绝；仅拥有采集的页面断开才中断采集，普通旁观页断开不影响。

周期推送`{t:'taught', ...文档}`，当前显示地图变化或新客户端连接会发对应文档。文档：

```javascript
{map, map_id, map_token, revision, points:[{x,y,yaw,s,continuous_id}], length,
 executable, report, state:'empty'|'ready'|'invalid', message,
 check:{state:'idle'|'preparing'|'loading'|'localizing'|'ready'|'failed',ok,message,checks:[]},
 capture:null|{session_id,map,state:'recording'|'paused'|'interrupted'|'processing'|'finished'|'failed'|'abandoned',
              mode,sample_count,length,points:[{x,y,yaw,continuous_id}],message,can_resume,
              base_revision,start_index?,end_index?}}
```

独立编辑页面使用HTTP，不连接运动控制：

- `GET /api/taught/maps` → `{maps:[{name,...}]}`
- `GET /api/taught/maps/{name}` → 文档；可带`?capture=session_id`取恢复草稿点（响应加draft_points）。
- `GET /api/taught/maps/{name}/grid` → 与现有WS相同的T_MAP二进制帧：uint8 kind，uint32LE JSON长度，meta `{w,h,res,ox,oy,oyaw}`，随后deflate uint8格子（未知255）。
- `POST /api/taught/maps/{name}` body `{expected_revision,map_token,points}` → `{ok,msg,data:文档}`，失败也给明确ok=false且不覆盖文件。

主页面数据源新增taughtGet/Save/Prepare/Check/Start/Pause/Resume/Finish/Abandon/Status；store.taught保存文档。可用现有src.cmd方法命名，协议op一律下划线。地图旋转后仍在map世界坐标编辑。主导航多站任务不是示教轨迹，两者数据不混用。

既有地图操作：修改栅格另存/旋转导出的新图不盲目复制活动示教，需重新保存校验；原图不动。回收地图时连同name.taught目录移动，未完成采集地图列为使用中，避免恢复数据丢失。

## 视觉与实现分工

沿用当前控制台字体和色板：背景#04070c、面板#09101a、正文#dbe6f3、控制强调#38e1dc；示教参考使用琥珀#f3b13f，采集中亮线与已保存虚线明确区分，断口不连。四个任务入口适配窄栏，状态与错误就地显示；编辑器显示当前地图、唯一轨迹、未保存状态与保存结果。

独立分工：核心包络/导航衔接与测试；静态界面/独立编辑器；网页仓储/采样/持久化/ROS及协议（root）。文件不交叉编辑，按合同集成后分别审查，再做整体浏览器与隔离ROS验证。

验收包含：一图一条、刷新/进程重启恢复、未定位禁止采集、暂停和失联停采、接入点错误拒绝、追加/局部替换、碰撞或版本冲突不覆盖、编辑器旋转坐标/撤销/新增保存、正常导航不需execute。开发日志与截图在主仓库data/development/web-taught-20261118，两个仓库分别提交本任务已完成内容。

## 最终实施约定（2026-11-19核对）

- ROS输入地图直接`map_fingerprint`，离线YAML才`map_identity`做一次规范化。当前支持maxval255的P2/P5/trinary；真实Nav2 Q16逐灰度及精确阈值对照通过，其他maxval明确拒绝。
- 专属scan的接收和ROS测量年龄均≤0.5s；odom均≤0.2s，健康接收≤0.75s；定位修正后只接受新的测量。采样去重按最后已检查帧，不能只看已保留点。
- start/resume在工作线程之前预留控制方，返回后再次检查连接、心跳和所有权；断线窗口中产生的采样恢复到中断态。其他页面不能接管未暂停采集的解锁/遥控。
- 物理停止不依赖检查点能否写盘。存储错误明确报告并停止采样；不会为日志失败跳过取消和上锁。
- 原始新姿态在固定锚点合并之前也扫掠；append/replace_segment显式使用preserve_fixed_points。编辑器用inspect保留旧几何，只有strict读取实际足迹也通过才显示ready。
- 保存始终把用户原map_token带到最后CAS，不能用第二次读图得到的新token追认用户基线。CLI FINISH同样在接受/入队前冻结文件与版本。
- 普通导航出发依据`taughtAvailable`与`taughtNavigation`在既有出发流程按需启用代理，缺字段的旧网页后端保留原行为；开始新目标前同步map_yaml。服务端拒绝静默忽略已保存示教。
- 使用入口和文件布局见[示教轨迹使用说明](../示教轨迹使用说明.md)。验收结果见同目录`2026-11-19-taught-route-verification.md`；完整动态原始记录仍位于忽略的任务目录。
