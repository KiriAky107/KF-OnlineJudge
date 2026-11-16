# 固定双 Worker 拓扑

本阶段使用一个控制端和两个 Worker，每个 Worker 一个执行槽位。至少一个 Worker 在独立机器/VM，保留现有 factory 隔离与停止确认。2026-11-16 已在同宿主的两个隔离 VM 上完成真实普通题与一次 Worker 进程故障接管；这不等同于跨物理主机性能基准或整套交付验收。

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

Coordinator 的 admission `max_concurrent_jobs` 设为 2；两个 Worker 各保持 1，prefetch 各为 1。多运行时 registry 内 legacy/multi_runtime 的容量仍需一致。不要把 Worker 自身并发改为 2。提交上限和执行资源预算沿用现有准入设置。

## 最小验证

1. 启动控制端和两个 Worker，提交同一组真实普通题作业，记录每次 attempt 的 Worker ID 与时间，确认两个节点都完成且时间重叠。
2. 在一个 Worker 正在判题时停止该进程；由 factory 完成旧执行清理。必要时重启原 Worker 完成停止报告，再由另一 Worker 接管。不能把租约过期直接当成旧任务已停止。
3. 同一小批提交分别用一个和两个 Worker 运行，记录完成时间、吞吐量、排队时间及资源条件。无需假定线性提速。

复用现有隔离环境。此处不要求重做旧安装、冷恢复或全故障矩阵；真实运行结果单独记录，不以配置检查或单元测试代替。
