# KF-OnlineJudge 判题工程

| crate | 职责 |
| --- | --- |
| `kf-contracts` | 平台与判题服务的通知契约和共享样例 |
| `kf-engine` | 执行编排端口、输出比较和判题汇总；不进行权限操作 |
| `kf-sandbox` | Linux namespace、运行根、身份、seccomp、cgroup、监督和清理 |
| `kf-coordinator` | PostgreSQL 事务、领取/租约/阶段/结果、取消和恢复；不操作账号或题库 |
| `kf-worker` | 普通用户工件准备与执行适配器注入；不操作评测数据库 |

依赖保持 `worker → engine / sandbox / contracts`、`coordinator → contracts`、`engine → contracts`、`sandbox → contracts`。原始系统调用和权限操作集中在 sandbox 内。

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

本地已提供 native 单用例、编译一次多测试点与平台消费/持久化协调入口；
个人成绩通过平台归属查询展示；交互题、SPJ 和生产部署仍未实现。`kf-worker --check` 只检查入口，不消费或执行任务。权限 helper 和镜像必须由部署方配置，Worker 不调用 sudo、不修改账号或权限。

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

## 单机持久化协调与常驻 Worker

先通过 Backend Alembic 迁移，分别创建仅继承 `kf_oj_platform`、`kf_oj_coordinator`
的普通数据库登录角色。Coordinator 不迁移、不读账号/写题库；Worker 不接收数据库
连接、不写评测状态、不调用 sudo。令牌是内部服务身份，与浏览器会话及作业短期租约分离。

```sh
kf-coordinator --config /etc/kf-judge/coordinator.json
kf-worker serve --config /etc/kf-judge/worker.json
kf-worker check-stopped --socket /var/lib/kf-judge/factory/control.sock
```

两个配置都须为 0600、有界、单链接普通文件。Coordinator 配置包含 `listen`、
`database_url`、`workers`（`id`/64位十六进制 `token`）、不同的 `platform_token`、
`admission`、`lease_seconds`（5–300）及 `recovery_seconds`（1–60）。
Worker 配置包含 `worker_id`、`service_token`、`coordinator_url`、`platform_url`、
`amqp_url`、固定 `socket`、自身 0700 `staging` 与同一 `admission`。
本轮只允许单机数值 loopback HTTP/AMQP 与 loopback/绝对 Unix socket PostgreSQL；
未提供跨机器 TLS 模式。Worker 固定并发/prefetch 为1，缺少有效领取权不能执行。

`/health/live` 表示进程存活；配置后 `/health/ready` 连接数据库并核对最小权限。
内部 HTTP 先认证再解析有界请求，整体10秒，数据库连接槽位4，handler槽位16。
claim 使用数据库时间、行锁和有限全局容量锁，提交后才返回新 attempt/递增 fence/
短期令牌与清单摘要。阶段严格递增，续租和结果核对当前绑定与令牌；用例、attempt
及终态同事务提交后才确认。完整同一报告可重复确认，修改或旧 attempt 被拒绝。

取消与过期撤销执行权，先进入 `cancelling`；不能把租约到期直接当作任务已停止。
Worker 续租失败在有界期限内取消 helper，通过 Root 工厂的停止查询核对 owned journal、
任务目录、身份和 cgroup 全部排空，才发送服务身份认证的停止报告。
未知资源保留并使工厂停止分配；Coordinator 保留 `cleanup_failed`，占用容量，
由管理员处理明确的外来资源后重启工厂/Worker。停止查询不能选取宿主路径、命令或任务。
只有确认实际清理的基础设施失败可按 admission 有限重试，普通成绩不重试。

恢复扫描每批最多16项；已投递而30秒后仍待领取的事件重新通知，broker/Worker 断线
采用最多30秒退避。AMQP 操作各5秒，HTTP 连接1秒/请求2秒；结果最多重试3次相同报告。
Worker SIGTERM 取消当前执行后退出，SIGKILL 由 Root EOF 清理、租约过期和重启协调收敛。
消息手工 ack 只表示通知已处理，持久化状态与补偿负责恢复，不把 ack 当成最终成绩。

输出预算必须同时满足每流不超过单文件上限、stdout/stderr 合计不超过捕获存储
上限；Python/Rust 清单在准入时拒绝不一致限额，实际沙箱仍独立核对。连续测试点
遇到完成 helper 尚待回收的暂忙槽位时，仅对不消耗 run ID 的 Busy 分配响应做
最多2秒/64次有界等待，仍受作业截止时间与取消约束；拒绝、重放和执行响应不明
不会重新执行同一请求。

新增外部依赖职责：Coordinator 的 `tokio-postgres 0.7.15` 只负责专用角色数据库事务；
Worker 的 `lapin 2.5.5` / `futures-lite 2.6.1` 负责 AMQP 通知，`reqwest 0.12.24`
负责有界 loopback 请求并禁用代理与重定向；`uuid 1.27.0` 生成身份，`sha2 0.10.9`
验证清单/工件/报告摘要。Engine 不引入数据库、HTTP、队列或权限依赖。


## Python / Java 与明确协议版本

新语言使用 v2 清单、typed 程序工件、阶段和报告。旧 v1 清单/报告/canonical 摘要保持原样，
不默加新字段。需要同一节点处理两代任务时，管理员显式配置 `AdmissionRegistry` 的
`legacy` 与 `multi_runtime`；仅配置旧能力的消费者继续拒绝 v2。两代配置必须共享
并发/prefetch/重试预算，所有镜像共用一个 Root 工厂、UID 池和容量预留。

CPython 固定 3.13.16，仅 main.py/标准库。准备 profile 只分析/编译源码，不执行顶层代码，
不导入用户 pyc、marshal、pickle 或 ZIP；运行使用已检查的封印源码。
OpenJDK 固定 Temurin 21.0.12.1+1-LTS，固定 Main.java/default-package Main；javac
使用固定 UTF-8 与禁用 annotation processing 的参数，允许最多256个有界平铺 class，
包括辅助/内部/匿名类，class 文件名仅允许受控 ASCII 字符且最长128字节。链接、目录替换、空类集、重复/超量/过长或不支持名称拒绝；
完整类集封印后逐点使用。同一题的每点都重新建立 helper、UID 与工作根。

每次检查、编译、运行均通过 Root 隔离门控，不在普通 Worker 执行解释器或 javac。
请求不能指定运行镜像宿主路径、任意 flags、classpath、JAR、agent 或环境变量；
PYTHONPATH、JAVA_TOOL_OPTIONS、JDK_JAVA_OPTIONS 等不从宿主继承。
语法/编译/入口错误为 CE，普通运行异常为 RE；真实可信 CPU/墙钟、cgroup OOM 和
输出超限分别为 TLE/MLE/OLE。用户自抛 MemoryError/OOM、打印文字或 exit code
不能证明 MLE。JVM Xmx 仅限制堆，元空间、代码缓存、native、每个线程栈均包含在
完整 cgroup 预算；低于启动/非堆预留的配置提前拒绝。镜像/门控/通信/清理故障为
SystemError，未知计量不补零。

镜像制作使用 `scripts/build_judging_images.py`，固定来源/摘要与闭合动态库列表，
不复制平台 venv、site-packages、服务配置或凭据。产物经 root 所有权、不可写目录、
逐文件摘要及 catalog 摘要核对后发布；路径只由可信 Root 配置决定。
生产标准预算不使用 VM 模拟器验收时明确设置的较慢启动预算。
新版节点配置与排空步骤见 [部署说明](../deploy/README.md#四语言节点的受控升级)。
