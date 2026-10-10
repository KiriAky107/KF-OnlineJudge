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

性能投影由 Coordinator 与验证后的最终 AC 同事务写入，平台仅有 SELECT 权限。
每个 Worker 可配置 `measurement_epoch`（64 位小写十六进制摘要）。运维须先核对实际
硬件、内核、CPU 调度/资源上限及测量配置，再固定该纪元；上述条件变化时更换纪元。
未配置纪元时正常判题与历史查询保留，统计不宣称可比较。组键还绑定不可变题目版本、
语言、精确工具链/镜像及编译/运行预算；它不含学生源码或提交身份。

`kf-coordinator --config /etc/kf-judge/coordinator.json --backfill-performance UUID`
是单条、有数据库期限的受控修复入口。它重新验证已保存报告的绑定、清单与摘要，
仅从原尝试已绑定的纪元生成缺失投影；重复运行不增加样本。新配置不能给无纪元的旧尝试
补造可信环境。原判题报告不改写。升级只有 Backend Alembic；存在纪元/投影时降级拒绝
丢弃数据，须另行进行显式数据保留迁移，不能用删除成绩绕过。
