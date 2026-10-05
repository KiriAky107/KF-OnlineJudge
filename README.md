# KF-OnlineJudge

以 Rust 自研判题与 Linux 沙箱、以 Python/FastAPI 提供平台服务的在线评测系统。
前端使用 Vue 3、TypeScript、Element Plus 和 Vite。

当前版本为 **0.0.1 工程骨架**：平台 API 和协调器提供存活接口，前端可检查实际 API 连接。
沙箱隔离执行、判题、账号、题库、提交与学习业务尚未实现。

## 工程划分

| 目录 | 职责 |
| --- | --- |
| `backend/` | Python 模块化单体、平台 API、学习 Worker、待投递事件发布器及唯一数据库迁移入口 |
| `judge/` | Rust workspace：协议、沙箱、判题编排、判题 Worker 和协调器 |
| `frontend/` | 页面、路由、请求生命周期与生成的 API 类型 |
| `contracts/` | 版本化 JSON Schema 与 OpenAPI 快照 |
| `deploy/` | 本地 PostgreSQL / RabbitMQ 配置 |
| `scripts/` | Python 导入边界与 Rust crate 依赖检查 |
| `tests/` | 跨语言共享协议样例 |
| `skills/`、`experiments/` | 后续内置技能与可复现实验的扩展位置 |

Python 业务域分为 `identity`、`problems`、`submissions`、`community`、`learning`、`skills`
和 `operations`。业务域之间通过公开应用接口协作，不直接访问其他域的持久化实现。
`runtime` 管理执行机制，通过注入端口调用业务能力。

Rust 分为 `kf-contracts`、`kf-sandbox`、`kf-engine`、`kf-worker`、`kf-coordinator` 五个 crate。
协调器拥有执行状态写入权，判题 Worker 不直接写评测数据库。
沙箱尚未验证隔离能力，当前执行入口明确拒绝运行程序。

## 安装与检查

工具链基线：Python 3.13.16 + uv、Rust 1.99.0、Node 26.10.0 + pnpm 11.28.4。
Python 和 Rust 版本由工程配置固定，三套工程分别提交依赖锁文件。
开发与构建使用 Linux 原生文件系统。

```bash
make install
make check
```

`make check` 包含架构边界、契约同步、静态检查、测试、前端生产构建和 Compose 配置检查。
检查无需启动数据库或队列，也不执行破坏性沙箱测试。
`make format` 格式化源码，`make contracts` 更新 Schema、OpenAPI 和前端类型。

## 启动骨架

分别在终端运行：

```bash
make api          # 平台 API：127.0.0.1:8000
make frontend     # Vue 开发服务：127.0.0.1:5173
make coordinator  # 协调器：127.0.0.1:8100
```

平台存活接口为 `/api/v1/health/live`，开发 API 文档为 `/api/docs`。
前端开发服务将 `/api` 代理到平台；生产部署需另行配置反向代理与 SPA 路由回退。
协调器存活接口为 `/health/live`。

```bash
make worker-check   # 三个后台入口仅检查配置，不消费任务
make sandbox-probe  # 只读观察 Linux 环境，不创建隔离资源
```

本地数据库与队列的启动步骤见 [部署说明](deploy/README.md)。
业务表和数据库 revision 尚未建立；迁移统一由后端 Alembic 管理。
模型服务与第三方插件尚未接入。

开发中的规划文档仅在本地维护，不包含在远端仓库中。
后续逐步实现沙箱、单机判题、分布式恢复、并行优化和学习功能。

## 许可证

[MIT](LICENSE)。引用或复用其他项目代码时记录来源并遵守对应许可证。
