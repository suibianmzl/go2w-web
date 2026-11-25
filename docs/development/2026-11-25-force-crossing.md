# 导航站点强制跨越：开发与验收

## 交付与范围

用户授权在独立工程基于 `dhb/param_api` 开发：站点可不选强制跨越，或至少选两个点；到第一个选中点后 true，到最后一个选中点后 false。用户确认取消、失败或接口失败时停止后续导航并尝试 false。

工作目录 `/home/tink/program/go2w-web-force-crossing`，分支 `dhb/param_api`，基线 `111591427aa1a5fd84bbbad1995018894506526a`。从原工程只读取远程配置与已有源码；新克隆使用本地已有 Git 对象后执行 dissociate，新仓库不依赖原仓库的对象目录。原工程未写入本任务修改。仅本地开发和隔离软件测试，没有真实 ROS / 机器狗调用、训练、部署或 push。

## 实现

- `server/go2w_web.py`：保存 `points[].forceCrossing`，前后端拒绝单选及非布尔值；配置变更使旧断点失效。普通未配置任务保持旧字段和断点指纹。
- 逐站和连续通过在首尾边界使用原有到达确认；边界不能用“提前经过”代替，导航失败或缺少位置时不能切换为开启。连续通过在边界分段，命令完成后才发下一段。
- 循环和断点继续重建区间状态。到点切换发生在该站停留和播报之前；首点已经在附近时沿用既有 `ROUTE_NEAR_START = 0.5 m` 规则。
- `server/obstacles_critic.py`：HTTP路由和路线共用命令执行服务；同一锁串行设置，并在锁内检查手动HTTP是否与任务冲突。超时或取消会终止并回收子进程。
- 退出时尝试关闭；关闭失败保留错误和进程内恢复标记，下一任务恢复失败则拒绝继续行驶。业务 true/false 映射不反转。
- `static/js/routes.js`、`static/index.html`、`static/css/console.css`：每站复选框、区间解释、单选拦截；保存重载、重排和删点保留或更新配置。模拟保存也执行校验。

## 验证证据

- `dev/test_route_force_crossing.py`：17项通过。运行真实 Server 任务协程，替换导航和参数外部依赖；覆盖旧数据、校验、逐站/连续分段、首点已在附近、循环两圈、接续、取消、启闭失败、边界导航失败及上次恢复失败。新增行为先观察失败，再实现并跑绿。
- `dev/test_obstacles_critic_api.py`：11项通过。覆盖原有HTTP状态及响应、跨源拒绝、命令缺失、超时、取消、命令串行、自动任务冲突和排队请求重新检查。排队竞态由代码审阅指出，专门回归先失败后通过。
- `dev/test_route_force_crossing_integration.py`：启动真实 aiohttp/WebSocket 服务、仓库自带假机器人、临时空旷地图及临时 `ros2` 替身。核对两个 HTTP 入口、真实 WebSocket 保存和执行、逐站/连续到点 true→false、区间取消 false，以及手动 HTTP 不覆盖任务。临时子进程和数据均由测试清理。
- `dev/test_route_force_crossing_browser.py`：实际 Chrome通过单选拦截、区间显示、保存刷新、重排、删除及390px移动宽度无横向溢出；无页面脚本错误。桌面/手机截图位于 `/tmp/go2w-force-crossing-browser/` 并已视觉查看。测试阻断外网字体，使用系统回退字体；产品字体配置未改。
- 既有 `test_teaching_web.py` 8项通过；`test_incidents.py` 直接运行全部通过；Python编译、JavaScript语法和 `git diff --check` 通过。
- 既有 `test_taught_prepare.py`：8项中7项通过，`test_same_page_cancel_during_real_stand_prevents_policy_stage` 因测试替身 `TaughtWeb` 缺少 `climb` 报错。已从未修改的基线归档到独立临时目录并复跑，同名测试同样失败，属于已有问题，本任务不修改该示教测试。

解释器：后端 `/home/tink/.cache/go2w_web_venv/bin/python`；Chrome `/home/tink/.codex/venvs/webapp-testing/bin/python`，浏览器 `/opt/google/chrome/chrome`。

## 证据边界

以上证明本地保存、任务流程、命令顺序、异常处理和界面交互。ROS命令由替身执行，没有核验真机节点最终参数值，也没有验收真实机器人跨越效果。突然断电或强杀不保证关闭动作执行；参数接口保留原先“命令成功，不另行读取参数”的语义。

## 审阅与决定

独立只读审阅确认修正后无剩余 Critical / Important / Minor。采用共享参数设置服务而非同进程HTTP回环，避免引入端口、TLS和停服调用依赖；原HTTP入口和成功响应仍保留。排队HTTP请求必须在持锁后重新检查任务状态，防止旧请求覆盖新的自动设置，该规则已由可复现回归验证。未配置路线不增加参数调用；本地测试和真机效果分别验收。
