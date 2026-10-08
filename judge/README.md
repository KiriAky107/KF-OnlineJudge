# KF-OnlineJudge 判题工程

| crate | 职责 |
| --- | --- |
| `kf-contracts` | 平台与判题服务的通知契约和共享样例 |
| `kf-engine` | 执行编排端口、输出比较和判题汇总；不进行权限操作 |
| `kf-sandbox` | Linux namespace、运行根、身份、seccomp、cgroup、监督和清理 |
| `kf-worker` | 普通用户工件准备与执行适配器注入；不操作评测数据库 |

依赖保持 `worker → engine / sandbox / contracts`、`engine → contracts`、`sandbox → contracts`。原始系统调用和权限操作集中在 sandbox 内。

## 普通执行入口

普通构建包含真实执行内核。支持 Linux x86_64、内核至少 6.6、cgroup v2 以及配置要求的控制器和隔离接口。必须由独立、单线程、未预装 seccomp 的 root helper 调用，拒绝 setuid 调用。业务 Worker 不以 root 运行。

```sh
kf-sandbox validate --spec request.json
kf-sandbox check-runtime --config /etc/kf-judge/runtime.json
kf-sandbox run --config /etc/kf-judge/runtime.json --spec request.json   --artifacts /var/lib/kf-judge/publications/input   --output /var/lib/kf-judge/publications/output
```

`check-runtime` 是只读诊断，始终输出 `execution_allowed=false` 和 `active_validation_required`；目录和接口可见不能证明隔离。`run` 不接受绕过验证的开关：配置、运行根和祖先必须由 root 持有且不可被其他身份写入；配置为有界、单链接普通文件；运行镜像必须包含 root 保护的 `manifest.json`：

```json
{"image_kind":"linux_gnu_cxx_x86_64_v1","architecture":"x86_64","revision":1}
```

部署方负责可信 GNU 镜像的内容、发布与存储。每次任务仍需完成真实 namespace、只读根、输入、身份、seccomp 和 exec 门控，任何阶段失败都不放行用户程序。helper 所在 cgroup 必须在任务委派子树外，实际内存、swap、进程和 CPU 上限不得超过可信配置的监督器预留；仅设置配置字段不生效。

`run` 缺少配置参数、环境不支持或准备失败时退出 2，stderr 输出结构化错误；`failure` 与 `cleanup_failure` 分别保留原始阶段错误和清理错误，诊断只含固定代码与 errno，不回显私有路径或输入。正常完成时 stdout 输出执行报告、归因和发布 ID；程序退出 0 不等于 Accepted。

输入仅允许逻辑工件引用，通过受控目录 FD 与 `openat2` 的 BENEATH/NO_SYMLINKS/NO_MAGICLINKS/NO_XDEV 解析；只读封印快照绑定到每任务 work。stdin 不挂载为用户文件。执行请求不能选择宿主挂载、运行镜像、身份池或委派根，也不继承宿主环境或拼接 shell。每任务独立 tmpfs、UID/GID、网络 namespace，无外部网络。

完成任务树终止、cgroup 空读回、owned 子进程回收、最终计量和导出后，才移除资源并释放 UID。stdout/stderr 和输出文件为不可变快照。普通文件入口按 run ID 排他发布 `stdout`、`stderr`、`artifact-N`，先写自身临时目录再原子安装，不覆盖已有目录；清理未完成时不发布。

## 监督和恢复

CPU 总时间与墙钟使用可信监督器和单调时钟；CPU 带宽仅用于节流。内存 OOM 和 pids 超限依据所属 cgroup 的真实事件及计量，不能凭 SIGKILL 推断 MLE。并发排空二进制 stdout/stderr，独立限额、截断和提前 stdin 关闭均保留实际证据。监督器最终报告保留计量不可用状态、全部限制观察、取消、退出状态和清理错误；比较与成绩属于 Engine。

root 保护的 v2 journal 与 owner flock 记录启动标识、PID 起始时间、资源 inode 和生命周期。服务启动恢复只回收证实归属且原 owner 已退出的租约；未知、跨启动或替换资源拒绝接管。外来文件不递归删除；清理失败继续保留身份，修复后可重复恢复。重启恢复 API 为 `kf_sandbox::cleanup::recover`，调用方须采用相同可信配置和独立单线程 helper 模型。

## 验证

```sh
make architecture check-judge
make check
```

宿主普通检查不执行权限、恶意或超限测试。`vm-acceptance` 仅增加独立 VM 的固定验收入口与故障注入，不是生产执行授权。ignored 的 Linux 测试必须在无宿主共享挂载、无业务凭据、无 NIC 且整体资源上限已实际读回的独立 VM 内执行；`KF_SANDBOX_INDEPENDENT_VM=1` 只是测试防误用标记，不能代替环境证据。未执行或跳过不能计为 Linux 安全验收。

覆盖真实 GNU C/C++ 编译后运行、危险调用、宿主和跨任务文件、权限提升、网络、CPU/内存/pids/输出/文件限制，以及取消、初始化回滚、真实 owner SIGKILL、服务恢复和重复清理。验收记录保存在本地忽略的计划目录；不包含远端开发文档或业务凭据。
