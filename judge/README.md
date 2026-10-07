# Rust 判题工程

Cargo workspace 分为五个 crate：

| Crate | 职责 | 当前能力 |
| --- | --- | --- |
| `kf-contracts` | 跨服务通知和执行契约 | 最小 v1 通知、共享 JSON 样例检查 |
| `kf-sandbox` | Linux 隔离、限制、计量与清理 | 规格/配置检查、内部 cgroup/namespace/运行根初始化；`run` 拒绝执行 |
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

词法检查不验证工件归属、实际文件类型、符号链接、身份与隔离能力；初始化器必须继续完成这些检查，
保留独立 VM 中的执行、安全和资源验收。

## 工件文件能力与快照

`artifacts.rs` 接收可信工件提供方选择的目录 FD，按逻辑 ID 打开发布文件；请求不能指定宿主目录。
目录及输入文件检查所有者和权限，文件必须是单链接普通文件。提供方负责工件的业务授权、原子发布，
并在准备期间避免修改发布内容；路径检查不认证业务 ID，复制前后的 inode/时间戳检查不替代内容摘要认证。
使用 `openat2` 的 BENEATH、NO_SYMLINKS、NO_MAGICLINKS、NO_XDEV 原子限定解析；不支持的内核或解析竞态直接拒绝。
先以 O_PATH 检查类型，再经持有 FD 重开该 inode，避免打开 FIFO/设备，也不重新查询用户路径。

输入、独立 stdin 和声明产物复制到 memfd，施加并读回 WRITE/GROW/SHRINK/SEAL 封印，只返回只读 CLOEXEC FD。
复制按 64 KiB 缓冲推进；单文件不超过规格 file_bytes，每批不超过 workdir_bytes 和固定 64 MiB 操作方上限。
数量沿用规格上限：64 个输入加独立 stdin，16 个输出；稀疏文件按逻辑长度计费。批次失败丢弃全部部分快照。
并行准备的总内存预留、工件摘要/发布端口、输入只读挂载、stdin 传递和最终执行后的导出调度仍待接入。

`RootedTarget::snapshot_outputs` 先读回同一任务的冻结门、成员、限额和运行根，再取得其工作目录 FD，
只提取准备时保存的声明路径，不递归枚举用户目录，也不导出符号链接、硬链接、特殊文件或子挂载。
该内部能力已在独立受限 VM 验证文件替换、目录/链接竞态、封印、文件及总量限制、部分 FD 清理。
当前只能对仍受监督器持有的冻结初始化目标取快照；执行后的终止/清空/收集生命周期仍待实现，`run` 继续拒绝。

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
只有一个线程；未来每次权限操作或 fork 前须再次检查。此控制入口以普通身份运行，不进入 namespace、降权、seccomp 或用户执行阶段，
初始化返回准备阶段的能力不可用。独立的特权 namespace 初始化入口见下文。

控制端必须先在可信 state 根下创建独占、权限 0700 的每次启动目录和 Unix listener，再通过
清空环境的 `Command` 启动内部入口；socket 不放在用户工作区，控制通道与用户 stdio 分开。
入口通过非阻塞、CLOEXEC socket 连接，队列满时直接失败，不在连接阶段无限等待。
双方用内核 `SO_PEERCRED` 核对预期 PID/UID/GID；父端 PID 来源于仍持有的 `Child`，
子端父 PID 来源于内核。内部入口不提供用户命令参数，握手消息不能转换成生命周期确认事件。

`launcher/protocol.rs` 使用带版本的 8 字节帧头和有界 JSON（普通消息最多 1 KiB，输入路径阶段最多 8 KiB）。先拒绝未知版本、标志及
非法长度，再读取固定大小缓冲；发送序列化也使用固定缓冲。父子消息类型分开，拒绝多余字段、
非法 run_id 和未知命令。所有 I/O 共用一个绝对截止时间（入口 5 秒，通道最大 10 秒），
部分帧不能续期；EOF、解析、超时或写入错误关闭并锁定通道，不能重新同步后继续初始化。

`rustix` 1.1.5 用于沙箱的 syscall/FD 封装，由锁文件固定。
选择它的 OwnedFd/AsFd 接口完成连接与凭据核对；目前无需 `nix` 或直接 `libc` 调用，
namespace 初始化另有一个局部、附安全理由的 `unshare_unsafe` 调用，workspace 的 unsafe deny
仍生效；不使用 FILES 标志，调用前检查独立入口只有一个线程。参见 [rustix](https://github.com/bytecodealliance/rustix)、
[socket_peercred](https://docs.rs/rustix/1.1.5/rustix/net/sockopt/fn.socket_peercred.html) 和
[fork 后的安全约束](https://doc.rust-lang.org/std/os/unix/process/trait.CommandExt.html#tymethod.pre_exec)。

普通 Linux 回归实际启动可信入口，验证单线程、认证握手、准备拒绝、取消和断开后的退出回收；
协议回归验证边界与失败锁定。这些测试没有运行用户代码或安全攻击，不是 Linux 隔离验收。
目标进程的阻塞/放行、完整 FD 白名单、进程树监督、阶段回滚及独立 VM 安全验收仍待完成。

移入独立 Linux 测试 VM 时，可为已构建的 `launcher_control` 测试二进制设置
`KF_SANDBOX_TEST_BIN=/kf/bin/kf-sandbox`，指定 VM 内同次构建的可信启动器路径；
默认仍使用 Cargo 构建的路径。此变量只由测试读取，启动器子进程仍清空环境。
在 VM 内直接验证 GNU 工具链或这些控制测试，只能证明测试载荷可用，不能作为沙箱隔离验收。

## 真实目标阻塞与任务 cgroup

`launcher/blocked.rs` 通过清空环境的 `Command` 新建可信 bootstrap 进程，bootstrap
在单线程入口设置父线程死亡时的 SIGKILL、核对实际父 PID，再自行 SIGSTOP。父端持有
唯一 Child 和 pidfd，用 waitid/NOWAIT 及实际进程状态确认停止；调用方须保留默认 SIGCHLD
处理（包括不使用 SA_NOCLDWAIT），不能由其他线程/信号处理器回收该 Child。代码额外拒绝
可观察到的 SIGCHLD 忽略/自定义处理。没有自定义 pre_exec 或 Rust fork 子进程初始化。

`cgroup.rs` 只接受可信运行配置中的委派子树，逐级 NOFOLLOW 打开并验证 cgroup v2、
helper 所有权、不可被组/其他用户写入、空 domain 及必需控制器。它设置并读回总体与每任务的
memory.max、交换空间 0、pids.max、cpu.max 和 memory.oom.group，限制任务目录数和层级。
每任务使用排他 mkdir，不采用已存在目录。CPU 带宽只是节流；CPU 总时间监督仍未实现。

`PreparedTarget` 将真实停止的 bootstrap 纳入已配置限额、已冻结的任务 cgroup，重复核对
限额、成员和冻结状态。当前没有 thaw/continue/exec 公共方法，不使用请求中的 argv 启动用户程序；
启动器二进制路径来自可信调用方。缺少隔离时明确返回 namespaces/capability_unavailable，
通过 cgroup.kill 和 pidfd 终止并回收后返回，清理错误单独保留。部署方仍须认证配置文件和
实际委派；尚未安装权限 helper、接入完整监督器或启用 CLI run。现有控制入口的 Initialize
仍返回准备拒绝。未来隔离初始化与最终 exec 放行需要独立的验证和控制阶段。

普通 `make check-judge` 对 `blocked_vm.rs` 的权限用例明确报告 ignored。仅在无宿主共享挂载、
无业务凭据且外部已验证整体资源上限的独立 QEMU VM 内，由可信运行者设置
`KF_SANDBOX_INDEPENDENT_VM=1` 和启动器路径，执行测试二进制的
`--ignored --test-threads=1 --nocapture`。测试还要求 VM 内的专用标记、QEMU 厂商信息、
网络仅 lo 及无宿主路径；不具备条件时拒绝，不能将 ignored 计为验收通过。
固定 C 探针仅在该 VM 中验证真实内存 OOM、fork 拒绝与 CPU 节流，测试过程没有执行提交程序。

参见 [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)、
[pidfd_open](https://man7.org/linux/man-pages/man2/pidfd_open.2.html)、
[PR_SET_PDEATHSIG](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)。

## 实际 namespace 初始化

`isolation/namespaces.rs` 集中 namespace 策略及 OS 操作，`launcher/namespaces.rs`
负责进程与控制编排。它的 `NamespacedTarget` 只推进固定的可信初始化，不接收用户命令。
它沿用停止目标及 cgroup 纳入流程；确认限额、成员和冻结状态后，通过私有 socketpair
发送初始化命令，再仅在此阶段解除冻结和继续 bootstrap。通道仍使用 1 KiB 帧及绝对截止时间，
协议只含 initialize/cancel、固定运行根预算和初始化结果，没有用户 argv、宿主路径或 exec 命令。

socketpair 的 SO_PEERCRED 标识创建者，不能当作子进程 PID 认证；安全依据是可信 Command
独占继承的端点。bootstrap 在 unshare 前核对创建者。PID 1 的对端位于祖先 PID namespace，
内核会把该对端 PID 映射为 0，rustix UCred 的非零 Pid 类型不能表示它，因此 PID 1 只使用
已经认证并独占继承的端点，不在该角色再次调用 socket_peercred。控制 socket 暂占内部入口的
FD 0，通常 stdout/stderr 为 null；运行根初始化暂借 FD 1/2 传递可信目录能力，切根后恢复为 null。
这里没有用户 stdio，最终 FD 白名单和 stdio 收集仍待实现。

初始化要求原 user namespace 中的 root helper，建立 mount/PID/IPC/UTS/network/cgroup
namespace，并立即把挂载传播递归设为 private。P0 选定的身份模型保留原 user namespace；
单 UID 的非特权 user namespace 映射不受支持。独立任务 UID/GID 租约、降权和 capabilities
清除属于后续身份阶段，当前 helper 仍有初始化权限，不能执行用户代码。

NEWPID 仅影响后续子进程，所以 bootstrap 再次 fresh exec 可信入口作为 namespace PID 1。
PID 1 设置父线程死亡 SIGKILL，并利用尚未替换的祖先 procfs 核对实际外部父 PID。
监督器读回其父子关系、NSpid、六类 namespace inode 与保留的 user namespace，随后从祖先
namespace 通过 pidfd 使 PID 1 停止，再冻结整个任务 cgroup。bootstrap 仍位于祖先 PID
namespace，负责等待这个 init；用户进程树的 PID-1 回收循环尚未实现。

cgroup namespace 在实际任务成员关系建立后创建，PID 1 核对自己的 cgroup 视图为 `0::/`。
它只虚拟化 cgroup 路径；限额和终止继续由外部监督器持有的 cgroup FD 执行。完成此阶段后，
namespace 单独初始化时运行根尚未就绪；`refuse_unimplemented_root` 返回
root_filesystem 阶段拒绝并清理，CLI run 继续拒绝。

`namespace_vm.rs` 与 `blocked_vm.rs` 使用同一个 VM 条件守卫。独立 VM 中的固定 C 探针验证
UTS 主机名、SysV IPC 隔离、新 network namespace 仅含未启用的 lo、跨任务挂载隔离和
PID namespace 的后续子进程。探针由验收运行者在 VM 中进入目标 namespace，没有开放生产
用户执行入口。权限测试还覆盖 pids.max 导致初始化失败及冻结目标丢弃；VM 自身的 PID 1
在整体终止路径回收孤儿 helper，完整服务进程树回收仍待实现，不能据此宣称已完成。

参见 [PID namespace](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)、
[cgroup namespace](https://man7.org/linux/man-pages/man7/cgroup_namespaces.7.html)。

## 私有运行根与有界临时空间

`isolation/root.rs` 负责可信目录能力、固定挂载操作和内核读回；`launcher/root.rs` 的
`RootedTarget` 负责租用、阶段控制、失败回滚以及 namespace 终止后的目录清理。
它仅推进可信初始化；受限 proc/dev、任务身份、capabilities、seccomp 和用户进程树监督
仍待实现，成功建立运行根不能放行用户代码，CLI `run` 继续拒绝。

镜像和任务父目录只来自可信运行配置。逐级 NOFOLLOW 打开目录，要求 root 所有且不可被
组或其他用户写入；镜像的 work/tmp/proc/dev 挂载锚点必须是实际目录。每任务目录排他创建，
不接管同名旧目录，不递归删除未经租用的内容。部署方须保证镜像发布、内容及其底层存储
不可被其他进程修改；当前检查不替代可信镜像清单、输入工件的符号链接/硬链接/TOCTOU 校验。

监督器只传递已打开的镜像/任务目录 FD 和工作空间预算，协议不能选择宿主挂载路径。
unshare 后继承 FD 仍指向旧 mount，因此初始化器通过内核报告的可信路径在新 namespace
中重新逐级打开，并核对 dev/inode。镜像采用非递归 bind，排除源目录内已有子挂载，
再设为 read-only/nosuid/nodev；每任务另外挂载独立 work/tmp tmpfs。
最小根使用 `pivot_root(".", ".")` 的堆叠方式，detach 旧根并切到 `/work`，
释放所有初始化目录 FD 后才报告完成。初始 ramfs/rootfs 等不能 pivot 的环境返回阶段错误。

`workdir_bytes` 同时限制 work 和 tmp：按 4 KiB 向下取整后约 3/4 分给 work、1/4 分给 tmp，
总量不超过申请值，每个挂载也限制 inode 数。过小或不支持的预算在创建任务目录前拒绝。
work 为 nosuid/nodev，保留后续编译和执行能力；tmp 另加 noexec。
监督器从祖先 procfs 核对实际根的镜像 dev/inode、只读标志、scratch 文件系统/容量/标志，
拒绝 init 仍持有目录 FD 的情况，再保持 STOP/freeze。proc/dev 目前为空，必要设备尚未挂载。

`root_vm.rs` 只在规定的独立 VM 中验收真实 pivot、只读写入 EROFS、tmp 写满 ENOSPC、
跨任务 scratch 隔离、宿主挂载不变、排他租用和失败清理。固定探针由 VM 验收运行者进入
目标挂载 namespace；这不证明用户 exec、身份降权、完整根逃逸防护或进程树回收已完成。
普通工程检查将这些特权用例明确标记 ignored。

参见 [pivot_root](https://man7.org/linux/man-pages/man2/pivot_root.2.html) 和
[mount](https://man7.org/linux/man-pages/man2/mount.2.html)。

## 受限 proc/dev 与初始化 FD 防护

`isolation/proc_dev.rs` 固定 OS 策略，`launcher/proc_dev.rs` 的 `RestrictedRootTarget`
在已完成的 `RootedTarget` 上推进独立握手；两个类型均没有用户 exec 方法。
fresh exec 的 bootstrap/init 在创建内部 FD 前扫描自己的 FD，只允许 0/1/2，
精确排除扫描器自己持有的 FD。任何额外继承文件或目录都在 namespace 操作前拒绝，
避免原始 close 与 Rust FD 所有权冲突。初始化后监督器再次确认 init 和 bootstrap
的根、cwd 及无目录 FD，拒绝 cwd 留在私有 root/work 以外的情况。

仅 namespace PID 1 在切根后重新挂载 proc，使用 ro/nosuid/nodev/noexec、
`hidepid=2,subset=pid`。监督器核对实际文件系统、挂载选项、PID 1/PPid 0，
以及全局 sys/sysrq-trigger/kcore/keys 等入口缺失；选项或挂载能力不支持则拒绝。
dev 使用 64 KiB、16 inode 的私有 tmpfs，只创建 root 所有、0666 的四个字符设备：
null (1:3)、zero (1:5)、random (1:8)、urandom (1:9)，随后把目录挂载设为只读。
dev 为 nosuid/noexec，允许这四个设备 I/O 所以不设置 nodev；不提供磁盘、mem、tty 或 pts。
固定 dev 的容量是初始化开销，work/tmp 的申请预算和内存计费政策另行管理。

`proc_dev_vm.rs` 在规定的独立 VM 中验证新 PID 视图、固定设备 I/O、非 root 固定探针对
helper/外部 PID、旧根和跨任务文件的访问拒绝，并验证意外继承 FD 的启动拒绝。
VM 专用故障注入器让内核对 mknodat 返回 EPERM，验证部分 proc/dev 初始化的真实清理。
探针的 UID 切换及故障过滤器仅属于 VM 验收，不能当作生产身份或 seccomp 实现。
测试输入尚未安全绑定，工件校验/提取、生产身份/caps/seccomp 和完整用户 stdio/监督
仍待实现，因此完整 proc/dev 与输入保护 TODO 暂不标记完成，run 继续拒绝。

参见 [proc 的 hidepid/subset 选项](https://man7.org/linux/man-pages/man5/procfs.5.html)
及 [null/zero 设备](https://man7.org/linux/man-pages/man4/null.4.html)。

## 可信运行配置与启动前检查

```bash
cd judge
cargo run --locked -p kf-sandbox -- check-runtime --config ../deploy/sandbox.runtime.example.json
```

首轮支持策略为 Linux x86_64、内核至少 6.6；这是项目保守支持范围，内核版本不能代替逐项能力验证。
所需接口包括 user/mount/PID/IPC/UTS/network/cgroup namespace、pidfd 等待、身份降权、seccomp filter，
以及实际委派子树中的 CPU/memory/pids 控制器、`cgroup.kill`、`memory.peak`、内存/进程事件和交换空间限制。
初始镜像类型固定为 `linux_gnu_cxx_x86_64_v1`：受控只读目录镜像、GNU C/C++ 编译器、动态加载器和
可信 `manifest.json`；内部运行根阶段已核对目录权限与实际只读挂载，镜像内容清单校验仍未实现。

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

## 只读输入初始化

`launcher/inputs.rs` 的 `InputBoundTarget` 在受限 proc/dev 就绪后继续固定初始化，
经已认证的控制 socket 逐个发送封印、只读、CLOEXEC 的文件 FD。接收端只接受预定的
消息次序、一个 FD、有限数量与总字节；多余/截断 ancillary 数据使通道永久失败。
stdin 只保留在监督器的独立快照中，不挂载到用户工作区，也尚未接入执行 stdio。

匿名 memfd 不能直接 bind 到任务 namespace。初始化器用固定 64 KiB 缓冲将快照
复制到有界 work tmpfs，再将该文件 self-bind 为 readonly/nosuid/nodev；数据另加 noexec，
仅规格指定的 native 主程序保留执行位。复制占用原工作区预算，ENOSPC 立即拒绝并回滚。
监督器逐字节核对封印来源与绑定内容、文件类型/权限/属主、挂载标志及 FD 关闭情况，
随后保持目标冻结。root 拥有的 sticky 工作区及输入上层目录允许旁边生成编译输出，
保护输入和其祖先不被未来任务身份替换。该固定阶段尚无用户执行入口。

`inputs_vm.rs` 在独立受限 VM 验收真实只读写入 EROFS、执行/noexec 区分、输入祖先
保护、stdin 不可见、不同任务隔离，以及链接/已有文件/空间不足导致的部分挂载回滚。
身份降权、capabilities/seccomp、用户进程树与 stdio 仍须完成后才能启用 `run`。

## 生产身份与权限收缩

`launcher/identity.rs` 的 `IdentityReducedTarget` 先在可信 state 根下的 root-owned
0700 `identities/uid-N` 目录中原子租用任务身份，再准备输入和推进降权。活跃、遗留或
未知标记一律不接管；池耗尽返回 EAGAIN。目标树终止、子进程回收和 root 清理均成功后
才移除自有租约；清理失败保留标记、独立报告错误，Drop 也遵循同样顺序。服务重启
后的遗留资源核验/回收仍是后续阶段，当前不会自动复用残留身份。

`isolation/identity.rs` 仅在固定单线程 PID 1 中执行：清空 supplementary groups，
关闭 keepcaps/ambient；锁定 root 特权、ambient 提升与 keepcaps，保留并锁定正常 UID
切换清权行为；遍历内核实际支持的全部 capability 位清空 bounding set，设置
`no_new_privs`，统一 real/effective/saved/filesystem UID/GID，并显式清空
effective/permitted/inheritable。凭据变更后重新设置 PDEATHSIG=SIGKILL。
初始化器读取 securebits/keepcaps/NNP/全部 capability 与组，监督器读取同一已持有 PID
的内核 status，再校验 root/cwd/目录 FD 和 STOP/freeze。未知 ABI、失败或不一致拒绝。

`identity_vm.rs` 与固定 VM fixture 验收真正的集成 init 凭据、并发独占/池耗尽/清理
后复用/失败留租约，以及精确生产 reducer 代码在 fork/线程/exec 中的权限继承；固定
setuid-root 文件无法恢复权限，跨 UID 信号/ptrace/文件访问拒绝。测试故障过滤器只
注入真实 setgroups EPERM，验证失败回滚；它不构成生产 seccomp 策略。fixture 仅可在
规定独立 VM 使用，没有向 CLI 增加用户执行命令。后续 seccomp、全任务树/stdio/计量
和运行后清理仍未验收，`run` 继续拒绝。

### 生产 seccomp 初始化检查点

`launcher/seccomp.rs` 的 `FilteredTarget` 在已降权的固定 PID 1 安装策略，父监督器
冻结后读回 `Seccomp=2`、`NoNewPrivs=1` 和增加一个过滤器；内核错误按 seccomp 阶段
拒绝，并先清空/回收任务再释放身份租约。这个类型仍在 exec 阶段拒绝，CLI `run` 未启用。

`isolation/seccomp.rs` 固定支持 Linux x86_64，按 native、compile_c、compile_cpp
建立默认 EPERM 的 syscall 白名单；兼容 i386 ABI 终止整个调用进程，x32/未知编号
拒绝。clone 只允许明确的普通 fork 或 pthread flags；编译 profile 另允许 vfork 和
编译输出权限设置。clone3 的指针参数不可由经典 BPF 检查，因此返回 ENOSYS，交由
glibc 回退到受检查的 clone。过滤器由固定 Rust 数据生成，经 seccompiler 0.5.0 的
安全 API 安装；请求不能传入 BPF。libc 仅提供 ABI 常量，依赖均限制在沙箱 crate。

允许的语言操作仍受独立根目录、只读输入、任务身份和 cgroup 限额约束；seccomp
不能检查路径字符串。控制通道 FD 3 的 recvmsg/sendto 和设置 I/O 超时仅用于可信
初始化，FD 为 CLOEXEC；最终用户 stdio/FD 替换仍须另行验收。通用信号发送、网络
创建、ptrace、namespace 重建和权限修改没有语言例外。pthread 取消/主动发送信号、
用户自建 namespace、clone3 专用功能和非 x86_64 程序当前不受支持。

`seccomp_vm.rs` 及固定 VM fixture 验证集成 init 安装、冻结读回、失败清理，以及
fork/exec/pthread 的真实过滤器继承、i386 SIGSYS、x32 EPERM 和 clone3 ENOSYS。
这些 fixture 仅在无共享挂载/无凭据、整体资源受限的独立 VM 中运行；普通 BPF
回归不是内核隔离验收。进程树监督、最终 stdio 和完整运行安全验收仍待实现。

危险能力的固定默认拒绝还覆盖 process_vm_*、mount/chroot/pivot、bpf/perf、
io_uring/userfaultfd、keyctl、模块/kexec/reboot、身份/capability 变更和信号发送。
fcntl 仅保留复制、读取 FD/flags、CLOEXEC 与普通同步/非阻塞/追加 flags，不允许
异步信号所有权、O_ASYNC 或高位 flags；ioctl 仅允许 TCGETS/FIONREAD 查询。
prctl 仅保留只读身份查询及本线程名称设置/查询，不能修改 dumpability 或过滤状态。
seccomp 安装后的控制收包仅允许 FD 3 + MSG_CMSG_CLOEXEC，不再允许 sendmsg。

`seccomp_capabilities_vm.rs` 的固定探针在相同降权身份、无过滤器时先确认 socket、
socketpair、ptrace 和 dumpability 可用，再安装真实生产策略验证三 profile 的拒绝。
语言探针使用 VM 内签名验证的只读 GNU 工具链副本视图和有界私有测试工作区，在
生产编译 profile 下实际执行 gcc/g++，在 native 下运行动态 C/C++/pthread，检查
空输入 EOF、二进制 I/O 和正常退出码 7。该探针独立验证 syscall 兼容性，不通过
尚未完成的用户执行 API，也不将其当作完整 OJ 执行/stdio 验收。

编译器子工具需要固定搜索路径。`ValidatedSpec::execution_environment()` 只从已校验
的语言/时区变量和可信默认值生成环境；编译 profile 注入镜像内 `PATH=/usr/bin`，
TMPDIR 指向私有 `/tmp`，native 不带 PATH。请求仍不能指定 PATH、TMPDIR、HOME
或 loader 覆盖；最终 exec 必须先清空环境、进入私有根，再应用这份环境。VM 语言
探针使用相同固定编译 PATH，仅将 TMPDIR 显式指向其有界测试工作区。

### Namespace 进程树回收原语

私有 `isolation/process_tree.rs` 为单线程、已降权/过滤的 namespace PID 1 定义回收
角色。PID 1 保留运行，主程序在独立子进程 exec；主进程 pidfd + waitid 与 wait-any
配合，处理主进程退出和孤儿后代收养的竞态，只有 ECHILD 才确认树为空。取消、控制
通道失联或监控异常都使 PID 1 退出，由内核终止该 namespace 的所有剩余进程；外层
监督器仍必须检查 cgroup 为空并回收 bootstrap，不能把主进程退出当作整个任务结束。

exec 参数与固定环境在 fork 前分配，fork 前重复验证单线程/PID 1/凭据/过滤器与
SIGCHLD wait 所有权。受审计的 unsafe 仅在沙箱内部：子进程用 async-signal-safe
系统操作绑定 FD 0/1/2、关闭其他 FD、设置 cwd/SIGPIPE，随后 execve 或 _exit。
FD 3 仅为 CLOEXEC 的 exec errno 管道；错误管道 EOF 只表示没有收到 exec 错误，
单凭 EOF 不能证明程序已经 exec（例如放行前被终止）。普通信号/pidfd_send_signal/
pidfd_getfd 仍拒绝，仅增加可见进程的只读 pidfd_open 以支持等待。

`tree_vm.rs`/固定 `tree_probe` 在受限独立 VM 实际验证正常主进程、孤儿收养/回收、
主进程退出仍有后代时取消、控制端断开及真实 exec ENOENT。探针使用与生产相同的
私有回收/凭据/seccomp 原语，在新 PID namespace 的 PID 1 运行；它不是完整任务
执行 API。CLI run 继续拒绝，stdio/限额/计量与完整监督全部验收前不暴露执行放行。

`io::PipePump` now feeds immutable snapshot stdin and drains independent stdout
and stderr pipes in bounded nonblocking ticks. Only blocking child ends reach
FDs 0/1/2; anonymous captures stay in a trusted 0700 directory capability, have
per-stream/file/aggregate byte ceilings and no published writable descriptor.
An over-limit observation retains at most the allowed prefix and requires
whole-tree cancellation; its byte count is a lower bound. Early stdin closure
uses thread-local SIGPIPE masking, preserving global disposition and prior
pending signals. Init is explicitly non-dumpable before filtering so same-UID
users cannot reopen its control/capture handles through proc. Fixed guarded VM
probes exercise the exact transport/reaper; full CLI exec, accounting and
post-run artifact publication still await their remaining gates.
