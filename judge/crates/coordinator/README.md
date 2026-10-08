# 持久化协调器

`kf-coordinator --config /etc/kf-judge/coordinator.json` 读取 0600、有界、单链接
私有配置。`listen` 为 loopback 地址，`database_url` 仅数值 loopback 或绝对 Unix socket，
使用继承 `kf_oj_coordinator` 的普通数据库角色；Backend Alembic 是唯一迁移入口。

`workers` 配置各 Worker 的 UUID 与独立64位十六进制 `token`，`platform_token`
与它们不同；同时配置可信 `admission`、`lease_seconds`（5–300）、
`recovery_seconds`（1–60）。内部身份与浏览器会话、短期执行租约分离。
新增 `tokio-postgres 0.7.15` 负责有限数据库连接和事务，`sha2 0.10.9` 绑定摘要。
依赖方向仍为 `coordinator → contracts`，不调用沙箱或维护另一套 migration。

`/health/live` 表示存活，`/health/ready` 核对数据库和角色权限。内部接口先认证，
请求有体积/期限/并发上限；claim 原子生成 attempt、可信数据库时间租约与递增 fence，
提交后才返回领取权。阶段更新、续租和报告都核对当前绑定与令牌；报告的用例、attempt
和终态同一事务提交，原样重复可确认，修改/旧 attempt 拒绝覆盖。

取消与过期先撤销执行权并进入 `cancelling`；实际清理证明到达前不释放容量。
清理失败保留 `cleanup_failed`，只有已确认停止的基础设施失败可按有限预算重试。
恢复扫描每批最多16项，待领取事件投递30秒后仍无执行权时补偿重新通知。
Worker 的实际停止证明与执行隔离需独立 VM 验收；普通数据库测试不代表 Linux 安全验收。
