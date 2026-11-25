# 示教工作区本地验收

核对时间：2026-11-19。范围是本地源码、文件、HTTP/WS、浏览器和隔离二维ROS；没有部署到机器狗，没有真机站起/解锁/运动、push或训练。

## 功能与证据

| 交付 | 实际核验 |
| --- | --- |
| 右上示教任务、中央采样、左侧唯一图层 | Chrome实际点击准备/采集/暂停/恢复/结束及切任务；模拟源明确标记，断线不自动恢复 |
| 独立编辑页面 | 实际绘制、拖点、插删点、区间、撤销重做、±90°旋转和键盘编辑；HTTP保存、刷新回读及双页版本冲突 |
| 地图一一保存 | YAML唯一包络，PGM不改；新建/更新、碰撞不覆盖、底图变化/CAS竞争拒绝、旧错图/坏几何可显示修复 |
| 采集恢复 | 真实网页后端 + 假ROS：强制SIGKILL，恢复全部13个已确认点，只进入interrupted；自检后人工继续，原始断口保留 |
| 所有权与停止 | 旁观页关闭不中断；采集页关闭中断并上锁；其他页面不能抢解锁；异步start/resume断线窗口回归；持久化失败不阻止停止 |
| 原轨迹更新 | 末尾追加与局部替换，原区间外点数/坐标/yaw不变；过渡和原始姿态仍做完整足迹检查 |
| 普通导航自动使用 | 旧导航栈准备/出发时启用代理；实际ROS普通目标读取YAML，运行中固定旧版，新目标使用新版；无字段走普通链 |

## 软件检查

- 网页按真实unittest模块运行 **102项，1项跳过，无失败**：地图、示教仓储/恢复/竞态、假ROS消息和运行工具。另运行独立`dev/test_procs.py`全部通过。跳过项为没有提供真实地图的旧地图编辑测试。
- 前端5个逻辑检查通过：`test_taught_geometry.mjs`、`test_taught_mock.mjs`、`test_taught_source.mjs`、`test_taught_ui.mjs`、`test_taught_flow.mjs`。覆盖断口、坐标、状态隔离、过期会话、超时、普通出发升级及旧后端兼容。
- 最终`dev/check_teaching_protocol.py`自建本地假ROS后端 **7组通过**；结束并保存17个原始点，参考长0.85m。SIGKILL前13个已确认点全部恢复。此长度不是实际导航跟随成绩。
- 导航主仓库完整`python3 -m pytest tests -q`：**746 passed，56 skipped，无失败**。根agent另外复跑本次4个相关文件 **135 passed**。跳过项没有计为通过。
- 主导航包实际增量安装通过，覆盖层来源检查通过；真实`libmap_io`与共享加载器对照12张图、3072格、零差异。
- 已完成两轮独立审查；原始朝向扫掠、严格读取校验、CLI基线时点、PGM边界及网页4项竞态/身份问题均红→绿并复核。差异、Python语法、shell语法和技能参考链接检查通过。

网页仓库包含有顶层执行/退出的独立测试脚本，不支持将全部`dev/test_*.py`一概交给unittest discover。曾以该入口执行出现导入/独立脚本SystemExit与专用Playwright环境缺依赖；未据此修改无关旧脚本。实际unittest命令为：

```bash
python -m unittest \
  dev.test_map_alignment dev.test_map_edit dev.test_mapcontinue \
  dev.test_mapmeta dev.test_map_overlap_check dev.test_teaching \
  dev.test_teaching_web dev.test_taught_fake_ros dev.test_runtime_tools
```

浏览器使用独立Playwright解释器与已安装Chrome，8组主验收、6组边界检查通过。900px/375px布局无水平溢出；编辑器没有WS连接，错误/空数据禁保存。后端最终重启后再次实际打开主页面和编辑器，读取持久化采集结果，版本及代理状态正确，无页面异常。

## 隔离ROS实际跟随

Docker网络为none、私有IPC、独立ROS域87，接口脚本断言只有lo，无设备桥。地图由共享write_bundle产生，**不是浏览器生成产物的一体化真机测试**；网页实际保存协议单独验证。

| 普通目标使用的参考 | 实际覆盖 | 横向误差95分位 | 真实目标误差 | 碰撞与停止 |
| --- | --- | --- | --- | --- |
| 原任务固定第1版 | 96.26% | 0.158m | 0.215m | 129次连续观测，0碰撞，停稳 |
| 新任务读取第2版 | 96.00% | 0.178m | 0.224m | 129次连续观测，0碰撞，停稳 |

沿用已有目标0.25m门槛，没有为通过放宽容差。独立回读原始真值、接受路径及碰撞JSONL完成覆盖、时间间隔、速度、停止与版本审计。空字段和移除字段后遗留派生默认的两例均实际走普通导航。

## 记录与限制

完整记录在主仓库忽略目录`data/development/web-taught-20261118/`：`protocol-final/result.json`、`frontend-browser-report.md/json`、`frontend-browser-edges.json`、`core-agent.md`、`ros-bundle/run-03/offline-audit.json`、`ros-bundle/pgm-native/result.json`及截图。早期失败证据保留，没有用后续结果覆盖。

这些结果支持本地功能和所述二维场景。真实AMCL、真实传感器/网络时序、轮腿控制、Orin计算预算及实际机器人跟随仍须部署后独立验收。
