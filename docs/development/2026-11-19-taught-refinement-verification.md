# 示教准备、样条与平滑展示验收

状态时点：2026-11-19，最终本地浏览器核对到13:39（Asia/Shanghai）。本轮基于网页`9e2249f`、导航`d5edb22`开发。范围是产品代码、软件验证及本地假机器人预览；没有真机动作、远端部署、push、训练或自动优化。

配套导航改动提交为`d93bf4e`；新版网页需要该提交提供的`spline`模块，不能仅更新静态资源或沿用旧算法安装包。

## 最终行为

1. 趴姿/站姿都可点“准备示教”。趴姿复用已有`stand_up`，等策略就绪并连续静止至少2秒，再启栈/载图/定位；自检通过后用户明确开始采集。准备不自动解锁或行走。
2. 编辑器保留稀疏控制点，后端用弦长参数化的自然三次样条生成路线；闭合段使用周期边界，两点段为直线。控制点位置精确保留，朝向来自切线，导航点相邻距离默认不超过5厘米。曲线须通过完整足迹及扫掠检查，断口不自动连接。
3. 预览与保存共用`TeachingRepository._build_reference()`，预览不发布文件。手绘样条不再额外进行二次偏移；录制继续走原重采样/二次平滑。结束按钮为“结束并平滑保存”，页面展示真实处理报告，原始采样日志保持。
4. YAML仍为每图唯一活动参考的权威来源。`reference.editing.control_points`保存编辑点，`reference.points`供导航，`reference.processing`保存处理摘要。版本冲突、底图变化或碰撞不能覆盖旧参考。

## 实际结果与证据边界

| 检查 | 实测结果 |
| --- | --- |
| 导航主仓库完整软件回归 | `791 passed, 56 skipped in 121.24s`；含45项新样条测试 |
| 样条安装 | 增量构建`go2w_taught_route`成功；从实际安装前缀导入新模块成功，非仅测试源码注入 |
| 网页后端完整回归 | `Ran 115 tests in 8.683s; OK (skipped=1)`，含8项准备和5项样条/处理测试 |
| Node检查 | 几何、mock、真实source协议、UI、普通导航flow、处理摘要6个脚本全部通过 |
| 进程管理 | `dev/test_procs.py`全部通过，含示教运行开关与原有档位/控制组行为 |
| 真实HTTP/WS、假ROS后端 | 初始`Passive`与`Velocity`各8组通过，含准备、原始采样、SIGKILL恢复、人工续采、CAS冲突和控制所有权 |
| Chrome | 原有主流程8组、边界6组通过；新增9组受控接口检查和2组实际假ROS后端检查通过 |

两种姿态的最终协议记录均有17个原始样本，强制结束重启后保住13个已确认样本，最终参考修订1。保存报告均为`recorded`、17输入点→16输出点、2次迭代、最大调整约**1.58厘米**、`full_sweep_checked=true`。这证明本地真实结束协议执行了处理器；不能据此推断实机跟踪误差或路线质量提升。

最终实际Chrome编辑页使用已保存的`editor`地图修订2，修改4个控制点后产生113个曲线点，完整足迹检查通过；预览前后修订保持2。页面显示4个编辑句柄、后端曲线、零额外偏移。已检查1440px、900px及375px布局，错误曲线/服务故障/迟到响应不会把旧通过状态带到新草稿；保存及刷新保留控制点。

最终实际主页从假`Passive`观察到`standing → settling → localizing → ready`；加载阶段很短时不保证被定时广播捕获，逻辑顺序另由准备测试验证。完成后`capture=null`、`armed=false`、`lab`修订0。准备阶段实际前端只发`taught_get`与`taught_prepare`，没有额外`stand_up`、开始采集或遥控命令。

## 审查发现及修复

- 原先静止判断可能借用移动前的策略ready时间：移动后从重新静止的时刻开始连续计时，实测至少2秒后才载图。
- 实际`stand_up`中途控制页失效，原先仍可能发送下一阶段启动策略请求：`ctrl_switch`在每轮和完成判断前检查准备凭据。控制权变化、同页cancel、急停、趴下及上锁先使凭据失效，正在up时同时置中断标记。
- 准备任务保留到实际退出，不提前开放第二次准备。已发出的站起请求不会被软件取消倒回趴姿，产品说明明确区分“中止后续阶段”和“趴下”。

独立只读审查已复核以上问题；最终定向执行同页cancel、跨页控制权变化和移动后静止计时3项测试通过，未发现新增阻塞。

## 复查入口

```bash
# 网页开发venv，已包含numpy/scipy/PyYAML/aiohttp
python -m unittest dev.test_map_alignment dev.test_map_edit dev.test_mapcontinue dev.test_mapmeta dev.test_map_overlap_check dev.test_teaching dev.test_teaching_web dev.test_taught_fake_ros dev.test_runtime_tools dev.test_taught_prepare dev.test_teaching_spline
python dev/check_teaching_protocol.py --output /tmp/taught-passive-new --initial-posture Passive --port 8840
python dev/check_teaching_protocol.py --output /tmp/taught-standing-new --initial-posture Velocity --port 8841

# 本地假后端需有lab与editor测试地图；浏览器解释器需有Playwright和Chrome
python dev/check_taught_refine_browser.py --url http://127.0.0.1:8838 --out /tmp/taught-browser-new --live-backend

# 导航主仓库既有Docker环境
sg docker -c './run.sh exec python3 -m pytest tests -q'
sg docker -c './run.sh build-nav --packages-select go2w_taught_route --parallel-workers 1'
```

原始证据在主仓库已忽略目录`data/development/web-taught-refine-20261119/`：`core.md`、`frontend.md`、`protocol-{lying,standing}-final/result.json`、`final-browser/frontend-refine-browser.json`与截图。早期测试假设修正（协议命名、短状态广播）保留在`frontend.md`，不算产品失败。

导航skill参考已同步仓库版与本机安装版，覆盖准备顺序、样条/预览/保存、控制点分存、断口和真实处理摘要，并通过结构、链接及独立检索验证。软件结果不替代真机定位质量、轮腿动力学、实际路径跟踪或Orin性能验收。
