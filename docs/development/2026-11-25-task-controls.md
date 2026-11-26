# Web v1.4.0：任务控制、站点限速与多包上传

本次从 main 的 `faa71e3` 开始，在独立工作区实现四项请求；没有修改真机配置、部署服务或启动真实运动。

## 最终行为

- 续跑卡片增加「终止任务」：只清除所选任务的未完成进度，保留路线。活动任务不能借此清进度；原子写盘失败会恢复内存断点并返回失败，其他任务不受影响。
- 站点可选 `vmax` 范围 0.1–1.2 m/s；缺失时动态继承任务最大 vx。各段采用任务与两端站点限速的最小值，首次接近首站只取任务与目标点。循环、断点续跑及跳站仍按对应的路线端点计算。
- 连续通过模式把相同限速的连续站点合并下发，在限速变化处分段；逐点模式也在限速变化处等待到站，避免提前经过站点时解除上一段的低速约束。成功结束恢复任务上限。失败或取消时不主动提高残留低速上限，下次任务启动会重新设置任务上限。
- 任务最大 wz 和临时 wz 调节上限均为 2 rad/s。活动路线的临时 vx 控件由任务限速接管，避免覆盖当前站间限制。
- 录包列表支持全选、多选上传；单个包上传时仍可提交其他包。两个执行名额包含等待语音阶段，更多包按顺序排队；同包不能重复排队，排队和执行时均不能删除。队列在内存中，服务重启后重新提交未完成包。
- 数据、语音、记录编号及匹配清单继续作为一个目录上传，保留原有压缩和回执校验。

## 实际验证

- 后端：`python -m unittest dev.test_task_controls dev.test_recording_window dev.test_bag_upload dev.test_recording_api dev.test_recording_tls dev.test_runtime_tools`，47项通过；解释器为 `/home/tink/.cache/go2w_web_venv/bin/python`。
- 浏览器：`dev/test_task_controls_browser.py`、`dev/test_bag_batch_upload_browser.py` 和既有录包卡片测试。使用真实 Chrome 的本地 mock 模式；检查了桌面和375/390像素手机布局。
- 假ROS：`dev/test_routes.mjs`、`dev/test_route_resume.mjs`、`dev/test_route_through.mjs`、新增 `dev/test_route_limits.mjs`。测试连接本机 WebSocket，在临时数据目录使用400×400、0.05米分辨率的空地测试图，覆盖逐段限速、wz=2、终止广播、恢复出发及既有路线生命周期。连续运行长套件时仅在假环境设置 `GO2W_IDLE_LIE=0`，并为各套件重置运行进程，避免空闲趴下或上一套件的取消收尾影响下一套件。
- JS语法检查、`dev/test_taught_source.mjs`、`dev/test_taught_ui.mjs`、`dev/test_recording_capture.mjs` 和 `git diff --check` 通过。
- 代码审查发现并复现了并发启动覆盖低速限制的竞态；新增红→绿测试，并在任何异步等待前取得启动租约。第二次启动以及启动期间的外部 vx 写入、遥控等入口被拒绝，最后释放租约。独立复测确认活动上限保持0.2 m/s。

这些结果证明Web协议、持久化、界面和本地假导航链的行为；不代表真机动力学、制动距离或局域网并行上传性能已经验证。原有20秒语音浏览器测试文件完整保留；本次主要通过真实本地rsync的后端整包测试和新批量界面测试验证上传。
