# Rust 判题工程

Cargo workspace 分为五个 crate：

| Crate | 职责 | 当前能力 |
| --- | --- | --- |
| `kf-contracts` | 跨服务通知和执行契约 | 最小 v1 通知、共享 JSON 样例检查 |
| `kf-sandbox` | Linux 隔离、限制、计量与清理 | `probe` 只读观察；`run` 拒绝执行 |
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
