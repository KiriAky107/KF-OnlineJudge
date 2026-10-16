# Python 平台工程

固定 Python 3.13.16，使用 uv 管理项目与锁文件。API、待投递事件发布器和学习 Worker
共用一个 Python 包、一个依赖锁文件，分别运行独立进程。

业务模块位于 `src/kf_oj/modules/`；HTTP 在 `api/`；通用适配器在 `infra/`；
Agent 执行机制在 `runtime/`；进程组合入口在 `workers/`。
业务域之间通过公开应用接口调用，不直接访问其他域的 repository/models。
Runtime 通过注入端口调用业务能力；导入边界由根目录的架构检查脚本验证。

```bash
# 从仓库根目录调用。
make install
make api
make worker-check
```

API 提供存活、真实登录/退出/当前用户、公开题目与有效 Worker 租约的清单/工件读取。
配置数据库后在 lifespan 内组合模块，配置缺失时业务端口明确不可用，存活接口仍可用。
学习 Worker 目前仅支持 `--check`；Rust 判题 Worker 使用单独的常驻消费入口。
提交创建 API 在持久化源码后，同一事务写入初始评测和 outbox；按用户与幂等键
防止重复创建，待评测总量和个人待评测数有固定上限。

待投递事件使用独立进程 `uv run --locked python -m kf_oj.infra.messaging.publisher`；
配置 `KF_OJ_DATABASE_URL` 为专用平台角色，`KF_OJ_AMQP_URL` 为内部 broker。
发布器一次只锁定一个到期事件；使用持久队列、消息及 mandatory publisher confirm，
确认后才提交投递标记。锁过期允许重投，消息不是执行权，Coordinator claim 才是。
队列最多 64 条、32 KiB，拒绝溢出，消息有效期 10 分钟；broker 不可用采用有界退避，
不阻塞提交事务。日志仅固定错误代码，不打印源码、答案或连接口令。
后台每次投递最多 12 秒，关闭连接最多 2 秒，SIGTERM 在当前有界操作结束后退出。
账号/题目/工件命令及限制见对应模块 README；模型能力尚未接入。

个人提交查询 `/api/v1/submissions` 使用 `(created_at,id)` 降序游标，最多100条；
`/{id}`、`/{id}/source` 仅允许提交本人读取，统一拒绝外来/不存在记录。
成绩投影不包含租约、工件摘要、隐藏用例 ID/输入/答案或程序输出；编译诊断至多64KiB，
缺失计量保留 null，未完成报告不展示成绩。身份/提交/内部响应均为 no-store。
`POST /{id}/cancel` 验证本人、Origin、会话和 CSRF，再调用 Coordinator 接受取消；
202仅表示已持久化意图，实际停止与资源回收由执行服务确认。
配置 `KF_OJ_COORDINATOR_URL` 为数值 loopback HTTP origin，
`KF_OJ_COORDINATOR_TOKEN` 与私有 Coordinator 的 `platform_token` 一致；
缺少配置或通信失败返回503，不直接修改执行状态。新增运行依赖 `httpx` 仅负责这个
3秒有界控制调用，禁用代理/重定向，不缓存或打印远端响应及内部凭据。

Python 虚拟环境位于 `backend/.venv`，可在 `backend/` 直接使用 `uv sync --locked` 和 `uv run --locked`。
数据库由 Alembic 唯一管理；业务模型在 `migrations/env.py` 中显式登记元数据。
不要用 API 启动时的 `create_all` 代替迁移，也不要让 Rust 维护另一套迁移历史。
配置示例在 `.env.example`；执行迁移前复制为 `.env` 并设置实际数据库连接。
