# Rust 判题工程

Cargo workspace 分为五个 crate：

| Crate | 职责 | 当前能力 |
| --- | --- | --- |
| `kf-contracts` | 跨服务通知和执行契约 | 最小 v1 通知、共享 JSON 样例检查 |
| `kf-sandbox` | Linux 隔离、限制、计量与清理 | 规格/配置检查与 `probe` 只读观察；`run` 拒绝执行 |
| `kf-engine` | 语言适配、测试点编排、比较与确定性汇总 | 预留库边界，无模拟判定 |
| `kf-worker` | 拉取、租约、工件准备与本地编排 | `--check` 入口，无消费循环 |
| `kf-coordinator` | 节点登记、租约、状态与结果写入权 | HTTP 存活检查，无任务端点 |

```bash
# 从仓库根目录调用。
make install
make check-judge
make sandbox-probe
make worker-check
make coordinator
```

工具链由 `rust-toolchain.toml` 固定，依赖由 `Cargo.lock` 固定。
协调器默认只监听 `127.0.0.1:8100`，唯一接口是 `/health/live`。
`KF_COORDINATOR_LISTEN` 可覆盖地址；在业务协议落地前不对外暴露内部服务。

沙箱的 `probe` 输出只表明当前进程看到的 namespace/cgroup/seccomp 信息，
不代表已经验证创建权限、委派或安全隔离。`run` 返回结构化拒绝和退出码 2。
真正的权限操作和 `unsafe` 代码只能落在沙箱模块，实施时逐项记录安全理由与 Linux 验证。
当前统一禁止 `unsafe`，需要时在对应模块明确缩小例外范围。

## 本地执行规格校验

```bash
cd judge
cargo run --locked -p kf-sandbox -- validate --spec crates/sandbox/tests/fixtures/spec.valid.json
```

`validate` 最多读取 64 KiB JSON，返回校验结果和 `execution_allowed: false`。
`run` 继续拒绝执行。当前规格仅用于本地 Worker/启动器边界，未改变平台 API 或跨服务任务通知。

规格使用 `native`、`compile_c`、`compile_cpp` profile；原生可执行文件必须引用输入工件在
`/work` 下的路径，编译器入口固定为运行镜像内的 `/usr/bin/gcc` 或 `/usr/bin/g++`。
输入工件只声明逻辑 ID 和只读文件相对路径，cwd 与导出路径也位于 `/work`；
请求不能提供宿主挂载、运行根、UID/GID 或策略上限。路径采用规范 ASCII 文件名，
不接受 `..`、绝对路径、空分段、反斜杠或隐式规范化。

限额须逐项显式提供：时间单位为微秒，内存、栈、输出、文件与工作目录容量单位为字节，
进程和 FD 为个数。`ResourceLimits::for_profile` 提供调用方模板，可信 `SpecPolicy` 可进一步收紧上限；
缺失或为零的限额不会变成无限制。CPU 总时间和墙钟时间分别校验，不假设多线程 CPU 时间小于墙钟。
环境变量仅接受 `LANG`/`LC_ALL` 的 `C`、`C.UTF-8` 和 `TZ=UTC`。

这些检查尚未验证工件归属、实际文件类型、符号链接、身份与隔离能力；初始化器必须继续完成这些检查，
保留独立 VM 中的执行、安全和资源验收。

## 本地执行结果与归因

`outcome.rs` 定义可信监督器的本地结果记录，含退出码/信号、计量、输出工件、启动阶段错误、
取消、限额观察及独立清理状态。`ExecutionReport::classify` 校验记录与已校验规格的一致性，
返回执行分类；退出码 0 仅代表程序正常退出，Accepted 仍由引擎比较输出后确定。
这些类型尚未接入实际监督器，也没有改变平台跨服务协议。

计量必须显式区分 `measured` 与 `unavailable`，读取失败、计数器重置及非法数据不能当作零。
CPU 和内存统计覆盖从纳入任务 cgroup 开始的初始化、线程和后代；CPU 不扣除估算的初始化费用。
墙钟使用 exec 放行至确认任务树为空并完成最终快照之间的单调时间。

主归因先报告基础设施/计量/清理故障，同时保留全部限额证据；正常监督下按首次观察时间选择
取消或限额，相同时取消优先，限额依次为内存、CPU、墙钟、进程、stdout、stderr、文件和工作目录。
这是监督器观察顺序，不能宣称是内核事件的真实先后。重复限额、越界时间和不支持的证据拒绝分类。

SIGKILL、内存峰值或单独的 `oom_kill` 不会推断任务内存超限。初版内存归因要求可信采集器核对
任务 cgroup 的配置及基线，再提供 `max`、`oom`、`oom_kill`、`oom_group_kill` 的正增量；
计数器在相同任务 cgroup 上采集，覆盖其后代。
参见 [cgroup v2 内存事件](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files)。
FD、栈和其他信号仍按原退出状态记录，缺少专门证据时不猜测原因。

输出保留字节数受限，`observed_bytes` 在截断时只是已读下界，`reached_eof` 单独记录。
超限输出须有对应证据；缺失计量、无说明的截断或不完整读取报告基础设施失败。
工件只能导出规格已申请的路径，单文件与已导出总量均受限；这些字段检查不能替代实际文件安全检查。
准备失败和清理失败分别保留，真实进程树终止、资源回收与 VM 验收仍待后续阶段实现。

## 启动生命周期顺序

`launcher/lifecycle.rs` 提供独立的状态约束：已校验 → 准备 → cgroup/限额确认 → namespace
确认 → 运行根确认 → 身份确认 → seccomp 确认 → exec 放行 → exec 确认 → 等待。
放行与 exec 成功确认分别记录；主进程退出只进入终止阶段，直到整个任务树为空且启动器/init
子进程已回收，才能进行最终计量、输出/工件收集和清理。清理成功后状态才结束。

取消、限额或操作失败停止推进初始化；乱序/重复阶段消息记录协议错误并进入终止流程。
即使调用方忽略错误，也无法继续这次执行的 exec 放行。计量阶段的错误不恢复已回收的任务，
也不跳过剩余收集；启动/监督故障与清理故障独立保留，重试清理成功不抹掉原始错误。
最先停止的原因只用于调度诊断，最终执行分类仍由结果模型依据全部观察确定。

事件仅由可信监督器在核验同一任务的实际操作后产生，不可从执行请求直接反序列化。
该状态模块不执行系统调用、不证明隔离、不产生执行许可；真实任务树终止与清理仍待实现和
独立 VM 验证。当前 CLI `run` 保持拒绝执行。

## 独立启动入口与控制通信

`launcher/entry.rs` 在可信二进制的独立进程中运行，不启动 Tokio、不安装自定义 `pre_exec`
闭包，也不在 Worker 的 fork 子进程里运行 Rust 初始化逻辑。入口实际检查 `/proc/self/task`
只有一个线程；未来每次权限操作或 fork 前须再次检查。当前入口以普通身份运行，没有实现
特权 helper、namespace、降权、seccomp 或用户程序执行，初始化返回准备阶段的能力不可用。

控制端必须先在可信 state 根下创建独占、权限 0700 的每次启动目录和 Unix listener，再通过
清空环境的 `Command` 启动内部入口；socket 不放在用户工作区，控制通道与用户 stdio 分开。
入口通过非阻塞、CLOEXEC socket 连接，队列满时直接失败，不在连接阶段无限等待。
双方用内核 `SO_PEERCRED` 核对预期 PID/UID/GID；父端 PID 来源于仍持有的 `Child`，
子端父 PID 来源于内核。内部入口不提供用户命令参数，握手消息不能转换成生命周期确认事件。

`launcher/protocol.rs` 使用带版本的 8 字节帧头和最多 1 KiB JSON。先拒绝未知版本、标志及
非法长度，再读取固定大小缓冲；发送序列化也使用固定缓冲。父子消息类型分开，拒绝多余字段、
非法 run_id 和未知命令。所有 I/O 共用一个绝对截止时间（入口 5 秒，通道最大 10 秒），
部分帧不能续期；EOF、解析、超时或写入错误关闭并锁定通道，不能重新同步后继续初始化。

新增 `rustix` 1.1.5 仅用于沙箱的安全 syscall/FD 封装，启用 `net`、`process`，由锁文件固定。
选择它的 OwnedFd/AsFd 接口完成连接与凭据核对；目前无需 `nix` 或直接 `libc` 调用，
也未放宽 `unsafe` 限制。参见 [rustix](https://github.com/bytecodealliance/rustix)、
[socket_peercred](https://docs.rs/rustix/1.1.5/rustix/net/sockopt/fn.socket_peercred.html) 和
[fork 后的安全约束](https://doc.rust-lang.org/std/os/unix/process/trait.CommandExt.html#tymethod.pre_exec)。

普通 Linux 回归实际启动可信入口，验证单线程、认证握手、准备拒绝、取消和断开后的退出回收；
协议回归验证边界与失败锁定。这些测试没有运行用户代码或安全攻击，不是 Linux 隔离验收。
目标进程的阻塞/放行、完整 FD 白名单、进程树监督、阶段回滚及独立 VM 安全验收仍待完成。

## 可信运行配置与启动前检查

```bash
cd judge
cargo run --locked -p kf-sandbox -- check-runtime --config ../deploy/sandbox.runtime.example.json
```

首轮支持策略为 Linux x86_64、内核至少 6.6；这是项目保守支持范围，内核版本不能代替逐项能力验证。
所需接口包括 user/mount/PID/IPC/UTS/network/cgroup namespace、pidfd 等待、身份降权、seccomp filter，
以及实际委派子树中的 CPU/memory/pids 控制器、`cgroup.kill`、`memory.peak`、内存/进程事件和交换空间限制。
初始镜像类型固定为 `linux_gnu_cxx_x86_64_v1`：受控只读目录镜像、GNU C/C++ 编译器、动态加载器和
可信 `manifest.json`；镜像内容、权限和只读挂载验证仍未实现。

配置由可信部署方提供，最多 64 KiB；不能由执行请求覆盖。`runtime.rs` 校验受控 state/tasks/image
目录布局、独立 cgroup 子树、普通 Worker 身份和独立任务 UID/GID 槽位；任务身份从 100000 起，
不与配置中的 Worker 重叠。槽位仅定义映射，真正分配前须核查系统账号占用，直到旧任务树为空且
清理完成后才可复用，服务重启也须恢复租用状态。

样例最多并发 2 个任务，每任务上限 512 MiB 内存、32 个进程和 256 MiB 工作空间；
整体内存预算 1.5 GiB、进程预算 128、工作空间预算 1 GiB，分别给监督器保留 256 MiB、32 个进程和 64 MiB。
CPU 带宽在同一 100 ms 周期中，每任务配额 100 ms、监督器预留 50 ms、父级配额 250 ms。
校验采用检查过的整数运算，整体预算至少覆盖「任务上限 × 并发数 + 监督器预留」，拒绝溢出或不足。
CPU 带宽和 CPU 总时间分别配置；预算目前只完成政策校验，真正应用和核验由后续 cgroup/监督器实现。
工作空间的 tmpfs 占用仍计入任务内存；镜像存储与工件长期保留由部署/工件提供方单独管理。

`check-runtime` 只读取当前环境和配置路径，输出结构化拒绝，退出码 2；配置有效时明确
`configuration_valid: true`、`execution_allowed: false`。目录/接口可见与不可见分别记录，
配置归属、路径真实包含关系、镜像完整性、身份池、helper、委派、预算实际生效、pidfd、namespace、
降权、seccomp 和清理能力均保留未验证状态。它不创建目录、改账号、启动服务或运行用户程序。
即使观察值里的 `isolation_verified` 被误设为 true，也不能解除未实现的执行拒绝。

接口语义参见 [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)、
[pidfd_open](https://man7.org/linux/man-pages/man2/pidfd_open.2.html) 和
[seccomp filter](https://docs.kernel.org/userspace-api/seccomp_filter.html)。
