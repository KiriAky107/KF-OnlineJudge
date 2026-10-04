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
