# 本地开发依赖

管理员账号 bootstrap/恢复、有界题包解析服务、不可变发布/下架、审计与回收步骤见
[题目维护运维说明](admin-problems.md)。该说明与服务模板用于受控部署准备，不表示已部署生产。

`compose.dev.yml` 只提供 PostgreSQL 和 RabbitMQ，端口绑定 localhost。
先复制 `.env.example` 为 `.env`，然后在仓库根目录运行：

```bash
make infra-config
make infra-up
make infra-down
```

`infra-down` 保留数据卷；不要把示例口令用于正式部署。
API 存活检查和前端预览不依赖这些容器，可以先独立启动。
该 Compose 不运行判题 Worker 或不可信代码。

镜像固定具体维护版本（PostgreSQL 18.6、RabbitMQ 4.3.6），正式实验前另锁镜像摘要。
配置检查只验证 Compose 结构；成功启动、实际健康与备份恢复需要单独验证。
后续受控主机服务和最小权限沙箱启动器配置放在本目录。

官方镜像说明：[PostgreSQL](https://hub.docker.com/_/postgres)、
[RabbitMQ](https://hub.docker.com/_/rabbitmq)。

### 单机 helper 工厂

`kf-sandbox factory --config /etc/kf-judge/runtime.json` 是 Root 服务入口；
普通 Worker 使用配置中 `state_root/factory/control.sock` 的普通用户客户端。
`deploy/kf-sandbox-factory.service` 给出有限服务预算示例，安装前由可信管理员
核对固定二进制、不可写 GNU 镜像/配置、Worker UID/GID，以及**与服务自身 cgroup
分离**的 Root 所有任务委派根。工厂实际读回服务 cgroup 限额，应用/读回任务总预算；
每次编译和测试点仍须通过已有完整隔离门控。服务文件本身不证明这些条件成立。

工厂只接收有界 run ID，无排队：槽位满、重复 ID 或未知字段立即拒绝。
每次申请使用全新单线程 helper，固定继承已认证的连接与工厂生命线；
额外控制帧、断开或生命线消失会终止整棵任务树。ID 先写入有限持久记录
（最多 4096 个，跨重启拒绝重放）；容量用尽需要管理员在所有任务停止、已核对
回收后安排新记录周期，不自动删除记录。清理失败保留身份租约并停止新分配；
重启只根据已记录归属和 inode 回收，未知/外来内容保留并拒绝启动。

`kf-worker judge-submission` 是受控本地适配入口。常驻 `kf-worker serve --config`
使用有界 AMQP 通知和经过服务身份认证的 Coordinator claim；配置与依赖见
[判题工程说明](../judge/README.md#单机持久化协调与常驻-worker)。
它验证不可变清单摘要，C17/C++20 仅映射到固定 GNU 命令，编译一次；
成功产物经完整清理后封印并计算 SHA-256，每点分别申请 helper、stdin 与工作区。
期望值逐点读取，留在比较端，不传给工厂/用户文件系统。
新增锁定 `sha2 0.10.9` 仅供 Worker 验证输入及编译产物摘要；
Sandbox 同版本仅为独立 VM 测试数据生成的开发依赖，Engine 不依赖加密或 OS 库。

### 单机提交服务的启动与停止

1. Backend Alembic 迁移；创建分别仅继承平台/协调器 NOLOGIN 角色的专用登录账号，
   平台角色只可插入初始评测，执行状态仅由 Coordinator 写入。
2. 普通平台服务持有私有工件目录；使用应用服务导入不可变题目和创建测试账号。
3. 验证固定 GNU 镜像、Root 配置及工厂服务/任务 cgroup 的真实上限，再启动工厂。
4. 独立启动 API、Coordinator、outbox publisher 和普通 Worker；单机 HTTP/AMQP
   仅数值 loopback，PostgreSQL loopback/Unix socket。任何入口都不自动创建表。
5. Coordinator readiness 验证数据库和权限；仍需独立 VM 验证真实执行门控。
   Root 工厂不可持有平台/MQ/数据库口令，用户运行镜像不含服务文件。
6. 先向 Worker 发送 SIGTERM，使当前任务取消/清理，再停止 publisher/API/Coordinator，
   最后停止工厂和本地依赖。强制死亡后的旧 attempt 必须撤销并取得实际停止证明；
   `cleanup_failed` 不等于普通终态，先核对保留资源再由管理员恢复工厂/Worker。

平台配置私有 `KF_OJ_ARTIFACT_ROOT`、有界字节/文件容量以及
`KF_OJ_JUDGE_ADMISSION`（完整可信 JSON）；其工具链、编译/运行上限、总体作业期限、
并发/prefetch/retry 必须与 Coordinator/Worker 一致。单机使用并发1、prefetch1，
不能仅提高 Worker 数量绕过节点容量。源码最多256KiB，测试点最多256个；
stdout/stderr 各不超过 file_bytes，合计不超过 workdir_bytes。

迁移账号与平台账号分开配置，命令均在 `backend/` 运行：

```sh
uv run --locked alembic upgrade head           # 私有迁移账号，仅执行受控 schema 操作
uv run --locked kf-accounts create local_user  # 换成平台角色；终端读取密码并二次确认
uv run --locked kf-problems /srv/private-import
uv run --locked uvicorn kf_oj.api.app:create_app --factory --host 127.0.0.1 --port 8000 \
  --no-proxy-headers --limit-concurrency 32 --backlog 64
uv run --locked python -m kf_oj.infra.messaging.publisher
```

每个账号操作/导入目录都由管理员准备，不从浏览器接受私有路径。导入格式见
[题目模块](../backend/src/kf_oj/modules/problems/README.md)，不可变版本以 UUID
在题库/题面链接和提交请求中传递。更新题目需导入新版本，不覆盖已有提交的题目版本。
临时验收使用合成数据和测试口令，实际业务数据与服务配置都保存在仓库外的私有目录。

取消另需 API 私有配置 `KF_OJ_COORDINATOR_URL=http://127.0.0.1:8100` 与
`KF_OJ_COORDINATOR_TOKEN`（Coordinator 的 platform_token）。所有服务令牌私下生成，
不可互用或放进前端环境。Coordinator/Worker 私有 JSON 为0600、单链接、有界文件；
Root 镜像/配置/二进制由 root 持有，任务委派子树独立于监督器自身 cgroup。
运行前实际读回整体节点、服务和任务 memory/swap/pids/CPU/文件/运行期限上限，
并通过真实隔离门控；配置解析、readiness、存活探测都不能替代执行验收。

前端从 `frontend/` 启动 `pnpm dev`，默认127.0.0.1:5173；平台明确允许相同浏览器
Origin，`/api` 同源代理。生产需另行配置 HTTPS、安全 cookie、SPA 回退与 `/api`
反向代理，本仓库不自动部署。用户以受控账号登录，在题面选择支持语言、提交源码，
随后查看同一 submission UUID 的持久化状态。等待/取消中/系统失败都不等于成功成绩。

broker 暂停时 API 仍能持久化 queued/outbox；恢复后 publisher confirm 与 Coordinator
补偿重新投递。Worker/Coordinator 重启使用原私有配置、工件、日志与 DB，不删除
attempt 或强改状态。旧租约不能覆盖新结果，有限基础设施重试最终收敛。
容量满时返回429或503；先确认待执行/清理状态，恢复服务或由管理员扩充经过核对的
存储容量，不删除仍被提交/题目引用的工件。`kf-artifacts --limit 100` 仅回收满足年龄
要求的无引用工件和已标记删除项；它不会通过删除用户源码来腾出容量。
恢复 Root 必须核对 journal/inode/实际排空；未知资源保留，管理员处理自己确认的
故障来源后再重启。不要递归清空任务目录，也不要把 cleanup_failed 改成已成功。

这些入口和顺序不表示生产部署已完成。隔离/超限与用户程序仅在无 NIC、无宿主共享
挂载、无业务凭据且整体上限已读回的独立测试 VM 内验收。开发 Compose 仍只提供可信
数据库和 broker，不在宿主或业务服务器运行用户代码。workflow 如需启用由指定
GitHub 私有仓库承载，并设置有限并发；Gitea 只保存逐阶段工程提交。


## 四语言节点的受控升级

新增公开示例 `judge.admission.example.json` 为显式 v1/v2 注册表，
`sandbox.managed.runtime.example.json` 为一槽位的共享 Root 配置；原 v1 示例保持原语义。
所有 `aaaaaaaa…` 镜像摘要均是无权限占位值，不能当作已验镜像。管理员制作固定镜像后，
对 manifest.json 的实际字节计算 SHA-256，逐文件验证版本/字节/权限，并将相同完整身份
写入准入注册表与 Root runtime_images。Root 镜像路径仅在私有可信配置中保存。
配置解析通过或 check-runtime 可见仍不允许跳过实际隔离门控。

1. 停止创建新提交/消费投递，向所有普通 Worker 发 SIGTERM；保留 DB、清单、工件和 journal。
2. 取得工厂实际 stopped/cleanup_complete 证明，读回空任务 cgroup 和 owned 资源；
   未排空或存在未知资源时停止升级，保留证据，不递归清空任务目录。
3. 使用独立迁移账号执行 Backend Alembic upgrade；新语言数据存在时旧迁移降级会拒绝，
   不删除数据以实现回退。平台与 Coordinator 继续使用各自最小权限角色，Worker 无 DB 账号。
4. 安装固定版本的 Backend/Coordinator/Worker/Sandbox；同时更新三个普通服务的同一
   `judge_admission`/`admission` 注册表，Root 配置单独由管理员持有。旧消费者不混用新协议。
5. 为整体节点、控制面各服务、工厂和任务设置有限 CPU/memory/swap/pids/文件/期限，
   读回内核值。节点至少预留一份512MiB编译任务以及独立控制面；Java运行示例总预算384MiB，
   Xmx不能代替整体预算。并发1/prefetch1，共享UID池，不因第二Worker存在而扩大节点槽位。
6. 在独立无NIC、无宿主共享挂载、无业务凭据 VM 用合成题目验证四语言、故障与实际排空，
   再恢复工厂/Coordinator/API/publisher/Worker。生产启用需单独安排，仓库不自动部署。

请求不在线安装 Python/Java 依赖，不运行 pip/uvx，不接收自定义解释器参数/JAR/agents。
Worker 私有配置可设置 `control_timeout_ms`（1000–5000，缺省2000）作为 Coordinator
单次控制请求的期限。资源受限节点应依据实际领取/续租/停止耗时选择，并核对租约和
停止恢复；这不改变学生阶段/作业期限、重试次数或工件下载的固定2秒期限，连接仍限1秒。
Python main.py 仅标准库；Java Main.java 使用默认包 Main，最多256个有界编译class。
题目语言与预算以公开版本配置为准，旧题目和草稿保留，Rust继续明确不支持。
新版检查阶段CE/运行RE/真实限额与SystemError的区别见 [判题说明](../judge/README.md#python--java-与明确协议版本)。

停止/故障恢复沿原单机流程：停止Worker使任务全树排空，旧租约/report拒绝后恢复；
publisher/broker中断保留outbox，有限重试而非清空记录。恢复旧软件前应先排空v2任务，
保留不可变v2版本和报告供兼容消费者读取；不能把旧配置静默转换为宽松新能力。
这些公开配置不含业务令牌。数据库/AMQP/服务令牌分别在仓库外私下生成，Root不持有它们。
需要CI时只在GitHub私有仓库 `KiriAky107/KF-OnlineJudge` 运行并设置有限并发，Gitea保存源码。
