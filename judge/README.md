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
保留独立 VM 中的执行、安全和资源验收。执行结果、限额归因与清理状态的契约将在后续阶段补齐。
