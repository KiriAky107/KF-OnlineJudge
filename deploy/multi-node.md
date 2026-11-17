# 固定双 Worker 拓扑

默认使用一个控制端和两个 Worker，每个 Worker 一个执行槽位；也可按下述方式显式启用有限多槽。至少一个 Worker 在独立机器/VM，保留现有 factory 隔离与停止确认。2026-11-16 已在同宿主的两个隔离 VM 上完成真实普通题与一次 Worker 进程故障接管；这不等同于跨物理主机性能基准或整套交付验收。

本说明适用于独立 Worker 配置与手动搭建的固定拓扑。现有 immutable release 安装器仍生成单机 loopback 配置，尚未提供多节点配置生成/升级入口；不要直接修改受 release 绑定的已安装配置来套用本例。真实 VM 验证使用独立测试配置，不代表该安装器已支持多节点交付。

## 连接

- 控制端的平台、Coordinator、PostgreSQL 继续监听 loopback。
- 通过标准 TLS 反向代理分别提供平台内部 API 和 Coordinator 的独立 HTTPS origin。不要将内部服务路由混入面向浏览器的公共站点。
- Broker 提供 AMQPS listener。现有平台 outbox 可继续使用控制端本地 AMQP。
- Worker 使用 `https://control.example:8443/`、`https://control.example:8444/` 与 `amqps://USER:PASSWORD@control.example:5671/`。这些仅为占位，凭据只写入各节点 0600 配置。
- 私有 CA 可通过 Worker 的 `tls_ca_file` 指定绝对 PEM 文件路径；CA 同时用于 HTTP 与 AMQP。服务证书必须覆盖连接地址，不能关闭验证。未设置则使用各 TLS 库默认信任根。
- Worker 不接收数据库凭据，两个 Worker 的 ID/token、staging、factory、运行根和 UID 池各自独立。

最小配置变更（合入已有可运行配置，而不是丢弃 admission/runtime 设置）：

```json
{
  "coordinator_url": "https://control.example:8443/",
  "platform_url": "https://control.example:8444/",
  "amqp_url": "amqps://WORKER_USER:REPLACE_LOCALLY@control.example:5671/",
  "tls_ca_file": "/etc/kf-judge/control-ca.pem"
}
```

HTTP origin 不支持路径前缀。AMQP 使用既有 vhost 编码，根路径仍表示默认 `/`。

## 容量

默认：Coordinator 的 admission `max_concurrent_jobs` 为全局上限；每个 `workers[]` 的 `slots` 默认 1。Worker 的 admission `max_concurrent_jobs` 与 `prefetch` 均默认 1。多运行时 registry 内 legacy/multi_runtime 的容量仍需一致。

### M4 有限槽位与资源准入

显式启用两槽时，把同一节点的 Coordinator `workers[].slots`、Worker admission 的 `max_concurrent_jobs`/`prefetch` 均设为 2；全局上限单独设置。每槽独立执行完整作业，测试点仍顺序执行。共享 staging 的回收和 factory 全局停止证明只在所有本地槽空闲时执行。

当前每槽使用一个有界 AMQP 连接/consumer，HTTP 客户端在同节点槽之间复用。Broker 的同一用户 `max-connections` 配额至少应覆盖所有使用该用户的 Worker 槽数总和（两个节点各2槽至少4连接）；保留独立节点凭据时按各用户分别计算。还需核对 vhost 的总连接上限，计入 Worker 之外的 outbox 等连接；例如4个Worker槽加至多2个outbox连接需要vhost容量6。同步核对 channel/consumer 限制，启动后确认预期消费端都已建立，再接受性能数据。旧单槽部署的账号连接上限不能直接沿用，否则可能只剩一个节点消费。配置命令见 [RabbitMQ CLI 的用户和 vhost 限额](https://www.rabbitmq.com/docs/4.3/man/rabbitmqctl.8)。

Coordinator 节点配置片段（合入原有 id/token，非完整配置）：

```json
{
  "slots": 2,
  "task_cpu_millicores": 750,
  "resource_budget": {"cpu_millicores": 1500, "memory_bytes": 402653184}
}
```

- **S0**：省略 `resource_budget`，仅固定槽位准入。沙箱真实 cgroup 限制始终有效。
- 工件服务既有的并发读取限流保持不变；Worker 遇到429仅在原总2秒工件读取预算内短暂等待，404/鉴权拒绝不会重试，预算耗尽仍按基础设施错误处理。
- **S1**：设置预算。领取事务中每个未证明清理完成的 attempt 预留固定 CPU 配额和 `max(compile_limits.memory_bytes, run_limits.memory_bytes)`。编译、运行和阶段切换使用同一整作业预留；不按实际峰值预测，不提前释放。临时不足返回 capacity，任务保持 queued，不消耗 attempt，后续既有通知重试。
- `task_cpu_millicores` 默认 1000，必须与该节点 native runtime 的 `1000 * task_cpu_quota_us / cpu_period_us` 一致（非整数向上取整）。这是 CPU 带宽容量，不是 CPU 时间限额或利用率；任务间相同，因此 CPU 维度近似静态槽上限，内存才随声明限额变化。
- 上述384MiB例子只适用于所有已广告单作业内存 ceiling 不超过该值的节点。保留 Java 等更高启动内存需求时应提高预算，不能为套用示例而降低运行时最低限制。
- 预算是扣除控制服务和 supervisor 预留后可供作业使用的容量。Coordinator 启动拒绝不能装入 admission 中任何单作业内存 ceiling 的节点预算；CPU 也必须至少装入一份任务配额，避免合法任务因配置永久排队。
- 同时配置 native factory 的 `max_concurrent_runs`、UID 池以及 aggregate CPU/内存/进程/工作目录预算。它原有的“槽数 × 单任务 ceiling + supervisor”硬校验不放宽；不要仅改 Worker 槽数。managed prepare 保留固定宿主布局与父 cgroup 限制，旧 immutable 安装器仍不负责生成这些多槽配置。

更改槽数或 native quota/预算前先排空相关 Worker 并确认旧 attempt 清理完成，再一致更新 Coordinator、Worker 和 factory 配置。

预算配置是可信运维声明，不自动探测宿主资源，不跨机器迁移已领取任务，也不更改租约、fencing 或清理证明语义。

## 最小验证

1. 启动控制端和两个 Worker，提交同一组真实普通题作业，记录每次 attempt 的 Worker ID 与时间，确认两个节点都完成且时间重叠。
2. 在一个 Worker 正在判题时停止该进程；由 factory 完成旧执行清理。必要时重启原 Worker 完成停止报告，再由另一 Worker 接管。不能把租约过期直接当成旧任务已停止。
3. 同一小批提交分别用一个和两个 Worker 运行，记录完成时间、吞吐量、排队时间及资源条件。无需假定线性提速。

复用现有隔离环境。此处不要求重做旧安装、冷恢复或全故障矩阵；真实运行结果单独记录，不以配置检查或单元测试代替。

## 多槽阶段验证状态（2026-11-17）

默认仍为单槽。多槽和资源预算为显式启用能力，本阶段尚未完成稳定性能验收，不建议把下列数据解释为生产就绪或调度提速。

- 定向检查：Coordinator 12项通过；相关check-judge为182通过、49项既有VM专用检查忽略，fmt/clippy及架构检查通过。此后工件429反压改动的Worker 19项和clippy通过。
- 真实双VM各2槽。固定槽位S0观察到两节点各512MiB整作业预留；预算S1观察到384/256MiB，两个节点都记录Memory拒绝，等待任务最终完成。CPU采用每作业750m固定quota，S1节点预算1500m/384MiB。
- 最后一次预热后的S0首批8/8首次AC，11.333秒；S1首批最终8/8 AC，但一项编译阶段lease_lost，清理后由另一节点第二attempt完成。该批38.321秒含重试，性能样本无效；预定S0→S1→S1→S0未完成，不能计算有效性能比。
- 运行在同宿主嵌套VM，A含控制服务且2vCPU，B为1vCPU；不是独立物理机扩展测试。55次宿主OOM事件采样未见OOM，但尚未定位租约丢失原因，不能据此排除控制面延迟等问题。
- 先前调试轮存在配置/启动/连接就绪问题及一次artifact_unavailable；其HTTP状态未记录，429适配修复不构成该次失败根因已证实。

当前保留实现与真实功能证据，后续应先定向定位多槽负载下的lease_lost，再决定稳定性验收和合并。不要通过放宽超时或反复完整重跑掩盖失败。原始结果、完整JSON校验记录和资源清理记录保留在本地阶段报告中，不上传凭据、题目工件或VM磁盘。

补充原始预热证据：warm_a有两项初次attempt报告cleanup_failed且报告内cleanup_complete=false，随后停止证明完成清理，数据库最终cleanup_complete=true并重试AC。预热未计入性能，但必须保留该异常；最终清理完成不等于首次执行无异常。
