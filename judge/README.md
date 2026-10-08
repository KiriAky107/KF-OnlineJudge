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

## Worker 和 Engine 的最小链路

服务管理器先在有限监督器 cgroup 内启动一个新的 `kf-sandbox serve-one --config /etc/kf-judge/runtime.json --socket /run/kf-judge/task.sock`。socket 的祖先须由 root 持有、不可被 Worker 写入并允许其搜索；socket 排他创建为 root:worker 的 0660，既有路径不覆盖。一次新进程只接收一次请求，等待连接和准备帧有绝对截止时间，不提供常驻消费循环。

普通用户运行：

```sh
kf-worker judge-once --socket /run/kf-judge/task.sock \
  --job native-job.json --artifacts /srv/kf-worker/publication
```

本地单用例作业示例（不是平台任务通知协议）：

```json
{"request":{"run_id":"case-001","program_artifact":"program-001","stdin_artifact":"input-001"},"expected_artifact":"expected-001"}
```

Worker 选取业务授权的自身发布目录，准备有界封印的期望输出，并向 Engine 注入 `ExecutionPort`。适配器构造 native 规格，仅绑定程序与 stdin，不将期望输出放入用户运行根。helper 根据 `SO_PEERCRED` 核验配置中的 Worker UID/GID；客户端核验 helper 的 root 凭据。有界私有帧与 `SCM_RIGHTS` 只传持有的目录能力和只读封印输出，收到的 FD 重新检查类型、长度、访问模式、封印和 CLOEXEC，不传任意宿主挂载路径。

helper 不接管 root 或其他身份的输入工件；每任务保留相同的隔离和预算门控。执行中客户端断开会取消全树，额外控制数据归为协议错误并拒绝执行，清理后退出。socket 清理核对自身 inode，不删除外来替换。业务发布目录的选择与业务授权仍属于可信 Worker；文件所有权不是业务授权证明。

Engine 仅在真实、无基础设施错误的 exit 0 后逐字节比较输出。非零退出/信号为 RuntimeError，CPU/墙钟、内存、输出限制分别映射 TLE/MLE/OLE，取消与 SystemError 单独表达；不可用计量保持 null。汇总在 Engine 内按输入顺序确定，基础设施错误或取消停止后续执行；空用例和重复 run ID 在执行前拒绝。`judge-once` 返回结构化 verdict、用例与实际计量。新增 `serde` 直接依赖用于本地作业/领域结果类型，Engine 不引入 sandbox 或 OS 库。

本地已提供 native 单用例及编译一次、多测试点编排；平台队列消费、持久化协调、完整提交投递、交互题、SPJ 和部署仍未实现。`kf-worker --check` 只检查入口，不消费或执行任务。权限 helper 和镜像必须由部署方配置，Worker 不调用 sudo、不修改账号或权限。

## 编译一次、多测试点

Root 服务 `kf-sandbox factory --config /etc/kf-judge/runtime.json` 先核对有限 cgroup
预留、配置/镜像权限和已确认归属的恢复记录，逐次产生独立单线程 helper。
普通用户通过 `FactoryClient` 申请固定 run ID 的连接能力；配置、镜像、二进制、
UID 池和宿主目录由服务决定。槽位满直接拒绝；有限持久 ID 记录跨重启阻止重放。
工厂退出通过生命线取消 helper，清理失败保留租约并停止分配；服务配置见
[部署说明](../deploy/README.md)。

```sh
kf-worker judge-submission --socket /var/lib/kf-judge/factory/control.sock \
  --job submission.json --artifacts /srv/kf-worker/authorized \
  --staging /srv/kf-worker/staging
```

本地作业包含版本化 `manifest`、`binding` 和 `manifest_digest`，清单摘要采用共享
规范序列化。工件发布按已授权的 SHA-256 名称读取，暂存父目录由普通 Worker
持有且为 0700。暂存日志记录进程启动时间、内核启动标识和目录/文件 inode；
进程重启只回收已确认归属的旧文件，未知内容保留并拒绝继续。

Engine 的编译、运行、期望值和时钟/取消端口分离。C17/C++20 映射固定 GNU 参数，
无 shell 或用户 flags；每 attempt 编译一次，封印产物、计算摘要并完成清理后，
按清单顺序逐点申请新 helper。每点只传程序和 stdin，期望值留在 Worker 比较端。
编译失败/超限为 CE，准备、导出或清理失败为 SystemError；取消/基础设施失败停止
后续点并列出跳过原因。计量分开保存编译消耗与运行 CPU 总量、内存峰值，缺失值
保留 null；完整清单缺点不能 Accepted。`judge-submission` 返回真实报告与阶段 PID。

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
