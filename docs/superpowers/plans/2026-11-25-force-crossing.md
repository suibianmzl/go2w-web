# Force Crossing Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in this session. Complete tests before commit.

**Goal:** 配置路线强制跨越区间并在真实到点后由后端启闭参数。
**Architecture:** HTTP和路线复用参数服务；站点保存布尔标记；在首尾边界分段并等待参数结果。
**Tech Stack:** Python asyncio/aiohttp、原生 JavaScript、unittest、Playwright/Chrome。
**Spec:** `docs/superpowers/specs/2026-11-25-force-crossing-design.md`

## Global Constraints

仅改独立克隆；保留现有HTTP兼容性；不下发真机请求；不新增依赖；测试后主动commit，不push。

## Review Focus

- 参数调用期间取消：不得继续发送导航目标，最终尝试false。
- 连续通过不能提前越过参数边界，失败不得当作成功。
- 循环终点返回首站不能在任务结束时遗留true。
- 接续和跳站必须重建所在区间的设置。
- 改选、删点和重排后，界面和断点必须反映新边界。

### Task 1: 参数服务与路线执行

Files: `server/obstacles_critic.py`, `server/go2w_web.py`, `dev/test_route_force_crossing.py`, `dev/test_obstacles_critic_api.py`。
Interface: `ObstaclesCriticAPI.apply(enabled)`异步返回命令输出；路线标记为 `points[].forceCrossing`。

- [x] 写真实Server任务协程的失败测试，核对没有功能时失败。
- [x] 抽取参数服务，保留HTTP适配与错误状态；串行执行参数命令。
- [x] 增加字段校验、区间到点切换、分段、接续和退出恢复。
- [x] 跑新增测试及现有HTTP回归，预期全部通过。

### Task 2: 编辑交互与交付

Files: `static/js/routes.js`, `static/js/source/mock.js`, `static/css/console.css`, `dev/test_route_force_crossing_browser.py`, `README.md`。
Interface: 复用站点字段，单选时禁用确认/保存/出发并显示原因。

- [x] 编写Chrome交互测试并观察缺少控件的失败。
- [x] 在现有站点卡增加复选框、区间说明及校验，保持视觉风格。
- [x] 验证编辑、保存重载、删点和重排，检查桌面和移动布局。
- [x] 跑相关回归、审阅差异、记录实际验证边界并commit。

## 执行结果

按用户既有的直接开发授权在本会话实施，没有修改原工程。异常恢复规则已由用户明确确认。新增真实HTTP/WebSocket假机器人集成测试 `dev/test_route_force_crossing_integration.py`。独立代码审阅发现手动HTTP排队竞态，已用失败回归复现并将任务冲突检查移到参数锁内，修正后11项HTTP测试通过。完整验证与已复现的基线测试问题见 `docs/development/2026-11-25-force-crossing.md`。

2026-11-25 用户更正参数映射：首边界false、末边界true，异常恢复true。业务开关与原始ROS参数分开表示，保留HTTP接口字面值；循环回程保持true，版本v1.3.1。
