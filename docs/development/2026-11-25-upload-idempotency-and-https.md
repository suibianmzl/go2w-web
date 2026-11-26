# 上传重复包与手机录音的部署缺口

## 已实现的同包处理（v1.4.4）

从 main `64a4f8f` 开发。同一个录包以已有录制目录名识别；rsync始终同步到同一服务器目录，因此重复提交不会在平台保存第二个同名录包。不同名称的包不自动合并，手动和异常自动包仍独立保留。

- 有当前服务器的有效回执时，默认请求直接返回已上传，不再启动rsync；列表全选、批量上传均跳过已上传项。
- 已上传行的按钮为“核对已上传录包”，确认后才核对并补齐服务器缺失或变化的文件。
- 回执记录 `upload_target`、`bundle_fingerprint` 和 `source_fingerprint`。源特征包含所有可见普通文件的路径、大小、纳秒mtime，不包含每次重建的recording-link.json、隐藏临时文件和符号链接。它不是整包内容校验；保持大小与mtime不变的外部篡改不在自动检测范围。
- 更新数据、语音或服务器后，旧回执不能跳过同步；旧版未记录目标/源特征的回执需要核对一次。同步期间源特征变化会标记失败，避免保存错误成功回执。
- rsync使用 `--checksum` 按文件内容核对，使同秒、同大小的替换也能更新。初版尝试 `--modify-window=-1`，本地测试通过但.166的受限rrsync拒绝该参数；实机只读dry-run确认baseline与checksum模式均可用，选择不改服务器限制的checksum方式。日志写“包总大小/同步耗时”，不把目录总大小当作实际传输量。

验证覆盖：32项上传/录音后端测试，包括重复请求、重启恢复、旧回执、换目标、新增语音、同名数据替换、同秒同大小替换、同步中修改和显式补文件后仍只有一个目标目录；批量上传和原录制卡片的真实Chrome回归通过。.21→.166的checksum dry-run确认零数据重传，约600MB原包只交换了303字节的rsync数据，未写入远端文件；实际SSH协议开销另计。

## .21 的HTTPS只读定位

当前运行链为 go2w-runtime.service → go2w-boot → compose → go2w_runtime → supervisor.py → /opt/go2w/runtime/entrypoint.sh。容器内 entrypoint.sh:163 固定启动 `python3 …/go2w_web.py --port 8080 --static …`，没有传入 `--tls-cert` / `--tls-key`。活跃Web进程参数相同，仅监听HTTP 8080；/opt/go2w/config（容器 /etc/go2w）和Web目录没有站点证书或私钥，runtime.env没有TLS设置。Web程序本身已支持证书参数，因此升级Web包不会自动开启HTTPS。

局域网HTTP页面不属于浏览器允许麦克风的安全上下文，录音在权限申请前就被拒绝；不是打开手机麦克风权限就能解决。

推荐增加HTTPS反向代理，转发已有HTTP服务和/ws WebSocket，保留当前安装健康检查。直接把8080改成TLS还须一并适配启动脚本、supervisor配置、compose和web_install.py的HTTP/普通WebSocket健康检查。证书需包含手机实际访问的IP/域名，手机需要信任其签发机构，之后使用HTTPS访问并允许麦克风。

本次仅诊断远端部署，未创建证书、改配置、重启或部署。手机信任状态未核对，HTTP入口仍不能录麦克风。证据在本机临时目录 /tmp/go2w-tls-config-audit-20261125。
