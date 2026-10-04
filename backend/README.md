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

API 当前只有 `/api/v1/health/live`，不连接数据库或消息队列。
Worker 目前仅支持 `--check`，默认拒绝进入尚未实现的任务循环。
日志、配置和迁移入口已建立；业务表、登录、判题投递和模型接入尚未实现。

Python 虚拟环境位于 `backend/.venv`，可在 `backend/` 直接使用 `uv sync --locked` 和 `uv run --locked`。
数据库由 Alembic 唯一管理；业务模型落地后在 `migrations/env.py` 中显式登记元数据。
不要用 API 启动时的 `create_all` 代替迁移，也不要让 Rust 维护另一套迁移历史。
配置示例在 `.env.example`；执行迁移前复制为 `.env` 并设置实际数据库连接。
