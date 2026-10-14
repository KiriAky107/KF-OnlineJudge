# 本地开发依赖

开发环境用 Compose 提供数据库和消息队列。单机交付使用独立的受限服务，安装入口见
[单机交付说明](release/README.md)。管理员账号、题包导入、发布、下架和审计见
[题目维护运维说明](admin-problems.md)。

## 启动开发依赖

将 `.env.example` 复制为 `.env`，在仓库根目录运行：

```bash
make infra-config
make infra-up
make infra-down
```

`compose.dev.yml` 只启动 PostgreSQL 和 RabbitMQ，端口绑定 localhost。
`infra-down` 保留数据卷。API 存活检查和前端预览可以先独立启动。
不要在这个 Compose 或宿主机上运行学生代码，也不要将示例口令用于正式部署。

开发镜像固定 PostgreSQL 18.6 和 RabbitMQ 4.3.6；正式实验还需锁定镜像摘要。
配置检查只检查 Compose 结构。服务健康和备份恢复要在实际环境验证。
镜像用法见官方 [PostgreSQL](https://hub.docker.com/_/postgres) 和
[RabbitMQ](https://hub.docker.com/_/rabbitmq) 说明。

## 单机 helper 工厂

`kf-sandbox factory --config /etc/kf-judge/runtime.json` 启动 Root 工厂。
普通 Worker 连接配置中的 `state_root/factory/control.sock`。
安装 `deploy/kf-sandbox-factory.service` 示例前，管理员需核对固定二进制、不可写的
GNU 镜像与配置、Worker UID/GID，以及 Root 所有且与监督器 cgroup 分离的任务委派根。
工厂读回服务和任务预算，每次编译、测试点运行都经过完整隔离检查。

工厂无等待队列，只接收有界 run ID。槽位满、重复 ID 和未知字段立即拒绝。
每次请求创建全新的单线程 helper，继承已认证连接和工厂生命线。
额外控制帧、断连或生命线消失会终止任务树。ID 先进入持久记录，最多 4096 个，
跨重启继续拒绝重放。清理失败时保留身份租约并停止分配；恢复只处理已记录的
归属和 inode，未知资源保留并拒绝启动。

### 轮换记录周期

可信 Root 参数 `--max-requests` 设置本周期受理上限，范围 1 到 4096，默认 4096。
容量用尽后，先停止任务并核对回收，再由管理员维护记录周期。

`kf-sandbox rotate-factory-cycle --config ROOT_CONFIG --cycle-id ID` 在受限监督器作用域
中核对配置和镜像、取得工厂排他锁，确认任务、身份、捕获目录、委派 cgroup 均为空，
且控制 socket 已消失，才归档请求记录到 Root 私有 `factory/cycles/`。
归档文件和目录经 fsync 后清空当前记录，最多保留 64 个周期；历史 ID 继续用于拒绝重放。

正常启动、重启、寿命结束和容量用尽不会自动轮换或删除历史记录。
若归档完成后中断，先排空，再显式用同一 cycle ID 重试。未知或不完整记录需要检查，
不能删日志续用工厂。轮换入口不启动服务。新安装路径的轮换、崩溃恢复和跨周期拒绝
仍需独立 VM 验收。

## 单机提交服务的启动与停止

`kf-worker judge-submission` 是受控本地适配入口；常驻
`kf-worker serve --config` 使用有界 AMQP 通知和认证后的 Coordinator claim。
配置见[判题工程说明](../judge/README.md#单机持久化协调与常驻-worker)。

Worker 校验不可变清单摘要，将 C17/C++20 映射到固定 GNU 命令，编译一次。
产物经完整清理后封印并计算 SHA-256；每个测试点单独申请 helper、stdin 和工作区。
期望输出留在比较端。Worker 用 `sha2 0.10.9` 校验输入与产物；Sandbox 的同版本
开发依赖仅生成独立 VM 测试数据，Engine 不依赖加密或 OS 库。

### 启动顺序

1. 用独立迁移账号执行 Backend Alembic。平台与协调器登录账号分别继承各自的
   NOLOGIN 角色；平台只插入初始评测，执行状态由 Coordinator 写入。
2. 为平台准备私有工件目录，通过应用服务导入不可变题目并创建测试账号。
3. 验证 GNU 镜像、Root 配置、工厂服务和任务 cgroup 的实际限额，启动工厂。
4. 独立启动 API、Coordinator、outbox publisher 和普通 Worker。HTTP/AMQP 使用
   数值 loopback，PostgreSQL 使用 loopback/Unix socket；启动入口不创建表。
5. 检查 Coordinator 数据库权限与 readiness，再在独立 VM 验证执行隔离。
   Root 工厂不持有平台/MQ/数据库口令，用户运行镜像不含服务文件。

平台配置包括私有 `KF_OJ_ARTIFACT_ROOT`、工件容量和可信
`KF_OJ_JUDGE_ADMISSION`。工具链、编译/运行预算、作业期限、并发/prefetch/retry
必须与 Coordinator/Worker 一致。单机并发 1、prefetch 1，共享任务身份池。
源码最多 256 KiB，测试点最多 256 个；stdout/stderr 各不超过 file_bytes，
合计不超过 workdir_bytes。增加 Worker 数量不会扩大节点槽位。

以下开发命令均在 `backend/` 运行；迁移账号与平台账号分别配置：

```sh
uv run --locked alembic upgrade head           # 私有迁移账号，仅执行受控 schema 操作
uv run --locked kf-accounts create local_user  # 换成平台角色；终端读取密码并二次确认
uv run --locked kf-problems /srv/private-import
uv run --locked uvicorn kf_oj.api.app:create_app --factory --host 127.0.0.1 --port 8000 \
  --no-proxy-headers --limit-concurrency 32 --backlog 64
uv run --locked python -m kf_oj.infra.messaging.publisher
```

账号和导入目录由管理员准备，浏览器不能传入私有路径。导入格式见
[题目模块](../backend/src/kf_oj/modules/problems/README.md)。题库、题面和提交使用
不可变版本 UUID；更新需发布新版本。临时验收使用合成数据和测试口令，业务数据与
配置放在仓库外。

取消功能还需 API 配置 `KF_OJ_COORDINATOR_URL=http://127.0.0.1:8100` 和
`KF_OJ_COORDINATOR_TOKEN`，后者对应 Coordinator 的 platform_token。
独立生成服务令牌，保存在私有配置中。Coordinator/Worker JSON 为 0600、单链接、
有界文件；Root 持有镜像、配置和二进制。运行前读回节点、服务和任务的
memory/swap/pids/CPU/文件/期限上限。

开发前端在 `frontend/` 运行 `pnpm dev`，默认 127.0.0.1:5173。
平台明确允许相同 Origin，`/api` 使用同源代理。生产入口还需 HTTPS、安全 Cookie、
SPA 回退和 API 反向代理。登录后在题面提交源码，按同一 submission UUID 查看状态；
等待、取消中和系统失败各有独立状态。

### 停止与故障恢复

先向 Worker 发送 SIGTERM，使当前任务取消并清理，再停止 publisher/API/Coordinator，
最后停止工厂和本地依赖。强制死亡后的旧 attempt 必须撤销并取得停止证明。
`cleanup_failed` 保留未确认资源，管理员检查故障来源后再恢复工厂/Worker。
不要递归清空任务目录或强改结果。

broker 暂停期间，API 保留 queued/outbox；恢复后 publisher confirm 与 Coordinator
补偿重新投递。重启 Worker/Coordinator 时保留配置、工件、日志、DB 和 attempt。
旧租约不能覆盖新结果，基础设施重试有次数上限。

容量满时返回 429 或 503。先检查待执行和清理状态，再恢复服务或核对扩容。
`kf-artifacts --limit 100` 只回收达到年龄要求的无引用工件和已标记删除项，
保留仍被题目或提交引用的源码。Root 恢复还需核对 journal、inode 和实际排空。

## 四语言节点的受控升级

`judge.admission.example.json` 是显式 v1/v2 注册表，
`sandbox.managed.runtime.example.json` 使用一槽位共享 Root 配置。
原 v1 示例语义保留。示例中的 `aaaaaaaa…` 是无权限占位摘要，必须换成固定镜像
manifest.json 的实际 SHA-256，并逐文件验证版本、字节和权限。
准入注册表与 Root runtime_images 使用同一完整身份；Root 镜像路径仅放入私有配置。

1. 停止创建提交和消费投递，向普通 Worker 发 SIGTERM，保留 DB、清单、工件和 journal。
2. 确认工厂 stopped/cleanup_complete，读回空任务 cgroup 和 owned 资源。
   发现未知资源或未排空时停止升级并保留证据。
3. 用独立账号执行 Backend Alembic upgrade。含新语言数据时旧迁移拒绝降级；
   保留这些数据。平台与 Coordinator 使用各自最小权限角色，Worker 无 DB 账号。
4. 安装固定版本 Backend/Coordinator/Worker/Sandbox，同时更新普通服务的同一
   `judge_admission`/`admission`。Root 配置单独保管，旧消费者不混用新协议。
5. 设置并读回节点、控制面、工厂和任务的 CPU/memory/swap/pids/文件/期限上限。
   至少预留一份 512 MiB 编译任务和独立控制面；Java 运行示例总预算 384 MiB，
   Xmx 只是堆上限。并发 1、prefetch 1，使用共享 UID 池。
6. 在断网、无宿主共享挂载、无业务凭据的受限独立 VM 验证四语言、故障和排空，
   再恢复服务。生产启用单独安排。

Python `main.py` 仅支持标准库。Java `Main.java` 使用默认包 Main，最多 256 个
有界编译 class。请求不在线安装依赖，不运行 pip/uvx，不接收解释器参数、JAR 或 agents。
题目版本决定语言和预算，旧题目、草稿和报告保留；Rust 暂不支持。
CE、RE、真实超限与 SystemError 的区别见
[判题说明](../judge/README.md#python--java-与明确协议版本)。

Worker 的 `control_timeout_ms` 可设为 1000 到 5000，缺省 2000。
按实际领取、续租和停止耗时选择，并核对租约与恢复流程。
该值不改变学生阶段/作业期限、重试次数、工件下载的固定 2 秒期限或 1 秒连接期限。

回退旧软件前先排空 v2 任务，保留 v2 不可变版本和报告供兼容消费者读取。
恢复时沿用停止与故障流程，保持 outbox 和有限重试。
公开示例不含业务令牌；数据库、AMQP 和服务令牌分别在仓库外生成，Root 不持有它们。

## 验收与 CI

所有学生源码、隔离、超限和故障测试都在无 NIC、无宿主共享挂载、无业务凭据且
整体上限已读回的独立 VM 运行。配置解析、readiness 和存活探测不能替代执行验收。
仓库不自动部署生产。

需要 workflow 时，在 GitHub 私有仓库 `KiriAky107/KF-OnlineJudge` 设置有限并发执行。
Gitea 保存工程提交，不运行 workflow。
