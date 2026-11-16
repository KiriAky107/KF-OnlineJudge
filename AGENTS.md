# KF-OnlineJudge 工程约定

按现有模块边界扩展。本地存在 `docs/ENGINEERING.md` 和 `docs/DEVELOPMENT.md` 时先阅读；开发文档不在远端仓库中。

- 本机 `docs` 使用共享目录链接，Windows 和 CachyOS 使用同一份共享文档；实际目标由 `readlink docs` 确认。不创建另一份同步副本，不把源码、依赖或构建产物写回共享目录。
- `docs/` 中的开发文档仅保留本地，不加入 Git 提交，不推送到 Gitea；不得用 `git add -f` 绕过忽略规则。
- 分支采用 `feat/`、`fix/`、`chore/`、`perf/`、`test/` 等工程前缀，提交采用 Conventional Commits；不添加工具名称前缀或工具署名。
- 本仓库提交身份使用 `kronecker <2026692687@qq.com>`。
- 后端固定 Python 3.13.16，由 uv 管理；不要改用系统 Python 3.14 或用 uvx 运行项目服务。
- 在本机原生文件系统中开发，使用仓库根目录 Makefile；Python 环境为 `backend/.venv`，Rust 构建产物为 `judge/target`。
- 平台为模块化单体；业务域之间通过 `public.py` 调用，不直接操作其他模块的 ORM/repository。
- Runtime 通过注入端口调用业务工具；不导入业务模块或具体基础设施实现。
- Rust crate 依赖方向与 Python 分层由 `scripts/check_architecture.py` 检查。新增边界须同步更新架构说明与检查规则。
- 跨服务协议变更先更新模型和共享样例，运行 `make contracts`，保留 Python/Rust 的兼容验证。
- 数据库迁移只由后端 Alembic 管理；协调器控制执行状态，判题 Worker 不直接写评测数据库。
- 沙箱未验证隔离前拒绝执行；不得用直接运行宿主命令作为临时判题实现。
- 只建立当期需要的接口和实现；未实现能力明确报告，不提供模拟成绩或虚假任务成功状态。
- 使用项目锁文件；新增依赖说明职责。凭据、私有代码和隐藏测试数据不进入仓库。
- 测试备份不得积累：测试结束并保存必要验收证据后，及时删除不再使用的备份、虚拟机磁盘/快照及临时工件；当前任务仍依赖的资源须列明用途和清理条件，被替代后立即清理，不能以保留日志为由长期保留大体积副本。

按变更运行 `make check-backend`、`make check-judge`、`make check-frontend` 和 `make architecture`。
跨工程骨架或契约改动运行完整 `make check`，记录实际通过项和未验证的能力。
沙箱的权限、隔离、超限和破坏性测试单独在规定执行环境验证。
