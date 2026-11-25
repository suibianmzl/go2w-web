# 多包上传的接收目录锁修复

2026-11-25 21:17（Asia/Shanghai）实机核对：`.21` 运行 `p-web-83c15b60fb48-86321964`，上传目标为 `.166`。本次修改接收端配置，没有修改狗上的 Web、运行环境变量或录制进程，也没有重启服务。

## 原因与修改

Web 已有两个并发上传名额，但 `.166` 的两条上传密钥强制命令均为：

```text
command="/usr/bin/rrsync -wo -no-del /home/evoengine/go2w_bags",restrict …
```

接收端 `/usr/bin/rrsync` 默认对整个受限目录使用非阻塞排他锁。不同包虽然写入不同子目录，也会互相拒绝。`.21` 日志中 `go2w-01_trigger_1125_204713_r`、`go2w-01_trigger_1125_205544_r` 等上传失败均包含：

```text
/usr/bin/rrsync error: Another instance of rrsync is already accessing this directory.
rsync error: error in rsync protocol data stream (code 12)
```

将 `.166` 的 `/home/evoengine/.ssh/authorized_keys` 中两条对应上传密钥的强制命令改为：

```text
command="/usr/bin/rrsync -wo -no-del -no-lock /home/evoengine/go2w_bags",restrict …
```

只增加接收程序自带的 `-no-lock` 选项，不修改密钥、目标目录或其他 SSH 限制。Web 继续限制每台狗最多两个并发作业，同一包排队或上传期间禁止重复提交及删除。不同狗须保留不同的狗编号，防止生成相同包名后写入同一目录。

修改采用原子替换，权限保留 `0600`。原文件备份于接收服务器的 `/home/evoengine/.ssh/authorized_keys.before-concurrent-bags-20261125T131637Z`；不要将包含密钥的文件提交到仓库。后续新增上传密钥时也须保留 `-no-lock`。旧的单写配置仍会导致并发作业失败，单独更新 Web 不能修复接收配置。

## 实机验证

1. 修改前，从 `.21` 运行容器同时发起两个完整生产参数的 rsync dry-run（包含 Zstd、checksum、partial 和 progress2）。其中一个退出码为 0，另一个退出码为 12，并复现上述目录锁报错。
2. 修改后，对同样两个包重复并发 dry-run，两个退出码均为 0。包大小分别为 629,722,572 和 631,232,867 字节；没有需要传输或新建的数据文件。
3. 通过 `.21` 正在运行的 Web 的 `/ws`，同时提交三个 `bag_upload` 指令，并使用 `verify: true` 明确绕过本地“已上传”回执，实际执行 rsync：

   | 包名 | 观察结果 |
   | --- | --- |
   | `go2w-01_trigger_1125_162248_r` | run → done |
   | `go2w-01_trigger_1125_162318_r` | run → done |
   | `go2w-01_trigger_1125_205544_r` | queued → run → done |

   三条指令均返回成功。状态推送实际显示前两包同时 `run`、第三包 `queued`，最后三包均为 `done`。服务器对应原目录均含 MCAP、`metadata.yaml`、`capture.json` 和 `recording-link.json`，没有另建副本。这些包此前已存在，因此本次验证的是实际并发连接、校验同步、排队和回执流程，不代表重新传输了全部数据，也不据此计算上传带宽。这些历史包没有录音，本次不验证麦克风录制。

4. 使用 `.21` 的实际上传密钥分别尝试 dry-run 删除、`../` 越界目标和反向读取。接收端均以退出码 12 拒绝，并分别报告删除选项被禁用、不允许 `..`、只写服务器禁止读取，确认原有目录与操作限制仍然生效。

原始错误、并发 dry-run、Web 状态和限制检查证据保存在本机 `/tmp/go2w-multi-upload-fail-20261125/`。该目录为本次临时诊断产物。
