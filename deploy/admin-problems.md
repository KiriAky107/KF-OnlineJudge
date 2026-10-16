# 题目维护的运行与恢复

本说明描述当前工程接口与受限服务配置。独立 VM 验收使用合成数据与仅 VM 有效的
临时口令，生产迁移、安装服务和部署仍需单独安排。运行服务用项目锁文件、Python
3.13.16 和 uv；请求不调用 uvx/pip 或联网安装依赖。

## 迁移、账号和授权

Backend Alembic 是唯一迁移入口。先停止新维护写入并备份专用数据库/私有工件，
以数据库所有者的独立配置执行下列命令；随后切换到平台配置创建普通账号：

```sh
cd backend
uv run --locked alembic upgrade head
uv run --locked alembic check
uv run --locked kf-accounts create maintenance_admin
```

用户名是示例，可按实际账号替换；密码仅在终端交互读取，不写入命令、仓库或浏览器
存储。迁移、平台、Coordinator、授权运维和导入服务分别使用专用登录身份，均不共享
高权限账号。普通登录角色为 `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`，
只继承对应 `NOLOGIN` 权限组。判题 Worker 无数据库连接。

通过仓库外的私有配置设置 `KF_OJ_IDENTITY_OPERATOR_DATABASE_URL`，只为授权 CLI
提供继承 `kf_oj_identity_operator` 的专用身份；它必须与平台数据库身份不同。然后执行：

```sh
uv run --locked kf-accounts bootstrap-admin maintenance_admin
uv run --locked kf-accounts grant-admin recovery_admin
uv run --locked kf-accounts revoke-admin maintenance_admin
uv run --locked kf-accounts enable maintenance_admin
```

恢复账号必须先存在。bootstrap 只用于没有启用管理员时；后续授权使用 grant。
数据库在固定操作函数中序列化变更，禁止撤销或禁用最后一名启用管理员。授权版本
变化会使旧会话失效，管理员须重新登录。不要直接更新管理员字段来绕过保护。

## 私有存储与导入服务

API 与普通 Python 导入 Worker 使用同一服务用户可访问的私有工件/暂存目录，目录
归该用户所有、权限 0700；数据库登录身份分别继承 `kf_oj_platform` 和
`kf_oj_problem_import`。导入 Worker 不执行上传内容、参考解或学生代码，不调用 sudo，
不写评测数据库中的执行状态/成绩，也不持有 Coordinator 或 SSH/GitHub 凭据。

外部私有配置需提供 `KF_OJ_DATABASE_URL`、`KF_OJ_ARTIFACT_ROOT`、
`KF_OJ_IMPORT_STAGING_ROOT`、可信 `KF_OJ_JUDGE_ADMISSION` 与
`KF_OJ_IMPORT_WORKER_CGROUP`。工件根、精确固定镜像及准入须与平台一致，网页题包
不能改写工具链/镜像/权限。示例摘要不能作为真实镜像摘要使用。

[导入服务模板](problem-import-worker.service.example) 使用普通 `User=kf-oj`，
128 MiB 内存、swap 0、PIDs 16、最多一核、文件 64 MiB、FD 64、core 0、
`NoNewPrivileges`、整组停止和 30 秒故障重启间隔。部署方先核对实际用户、路径和
环境文件再安装为 `kf-problem-import.service`；其 PrivateNetwork/AF_UNIX 要求使用
可访问的 PostgreSQL Unix socket。API 的连接路径可不同，但必须连到同一专用数据库。
配置好实际受限 cgroup 后，组合入口为 `uv run --locked kf-problem-import-worker`。
仅解析配置或存在模板不代表实际资源隔离已经验证；启动及每次解析均读回必要上限，
身份、目录、上限或新鲜资源证明缺失时拒绝处理并报告不可用。

在已安装且路径核对的服务上，可信运维可执行：

```sh
systemctl start kf-problem-import.service
systemctl show kf-problem-import.service -p MainPID -p ControlGroup -p MemoryMax -p MemorySwapMax -p TasksMax -p CPUQuotaPerSecUSec
systemctl stop kf-problem-import.service
```

停止后读回对应 cgroup 的 `pids.current=0`，再处理自己确认的临时文件；不得只观察
父 PID。父进程异常退出时，活解析子进程仍持有内核文件锁，GC 不能提前释放或复用
该槽位。恢复使用原服务配置，让有界租约/重试收敛；不通过删除任务记录或放宽上限
恢复。错误配置的 Worker 不写就绪证明，网页维护能力明确不可用。

## 题包、候选与发布

管理入口为同源 `/admin/index.html#/problems`，共享安全 Cookie/CSRF 与实时账号权限。
普通用户没有维护能力。题包只接受 stored/deflate 单层 ZIP，`problem.json` 清单、
UTF-8 题面、显式公开样例与隐藏输入/答案必须分别声明。只接受有界 ASCII 普通文件名；
拒绝链接、设备、路径穿越、重复名称、加密/嵌套/未知成员、损坏 CRC 和虚假大小。
不接收外部下载 URL、生成器、脚本或 SPJ，不执行包内内容。

压缩及实际解包分别最多 64 MiB，最多 534 个文件；上传最多 4096 帧、每帧 1 MiB、
整体 30 秒；解析整体 60 秒。每次暂存预约 128 MiB，总预约 256 MiB，上传/解析
全局并发 1、每管理员 1，未结束候选本人最多 10 个/全实例 32 个，操作 24 小时到期，
解析死亡最多领取两次。能力接口返回实际固定边界，管理员不能在清单中放宽。
题包规范与固定语言入口见[题目模块](../backend/src/kf_oj/modules/problems/README.md)。

先以稳定 UUID、实际内容 SHA-256 和字节数建立导入，再向同一操作流式 PUT。
双击、超时、响应丢失和刷新沿用原 UUID；变更内容创建新操作。浏览器只保存账号、
UUID、摘要、字节数和前置条件，不保存包正文、口令、会话或管理员能力。
自动状态查询有界，失败后手动核对原操作。`ready` 是未发布候选，学生不能读取。
候选预览只含公开题面/样例/预算，隐藏答案/工件引用和宿主路径不会下发。

发布使用独立稳定 UUID、本人候选/导入 UUID 与预期活动版本；事务内复核当前授权、
准入和工件实际内容，原子创建新 revision、切换活动版本并追加审计。修改题目通过
新包发布新不可变版本；不得修改已发布内容或硬删被提交引用的旧版本。
发布响应不确定时读回原维护回执与导入状态，不能先在页面声称成功。409 后读回
最新活动版本/状态，再由操作者决定是否按新前置条件操作，不静默覆盖。

下架关闭公开读取和新提交，合法已受理任务继续通过有效租约读取原已发布版本；
本人历史/成绩保留。恢复只切换可用性，不重评旧提交。题目内提交与源码仍只对本人
开放；性能只比较同不可变版本、语言、精确镜像/工具链/预算和可信测量环境的最终 AC，
缺测/无法证明/其他样本不足五条明确不可比较。

## 审计、故障和回收

管理端有限分页展示真实题目、版本历史、本人导入/维护回执和维护审计。
审计含实际主体、对象、结果、时间和操作 UUID，不记录口令、隐藏正文；应用角色没有
UPDATE/DELETE 审计权限。失败/回滚不会创建成功回执、半发布版本或成功审计。
比赛、节点、站点设置等管理区仍明确未实现。

408 表示实际上传期限超出；409 表示内容/版本/状态冲突；413/415 为字节或格式拒绝；
503 可能是资源槽位占用、配置/服务不可用。先检查原 UUID、真实状态、Worker 心跳、
数据库预约及实际 cgroup，按原因有限重试，不能密集重试或把错误当成成功。
解析服务/API 重启后保留持久化操作和原 UUID，让有界恢复完成。丢弃、失败和到期
先清理暂存字节再释放预约；未知/仍被活解析进程锁定的资源保持拒绝并供运维核对。

工件收集器每次只处理有界老且无引用的数据：

```sh
uv run --locked kf-artifacts --limit 100
```

按返回的 metadata cursor 分次继续核对；候选、已发布用例、旧提交/成绩的引用必须
保留。不要手工清空私有工件目录、删除审计或通过改写预约数字腾容量。
业务数据、口令、题包、镜像/构建产物及本地验收记录不进入工程提交。
本地阶段保存使用明确工程分支与串行分次推送，CI/CD 如需运行只使用约定的 GitHub
私有仓库和明确并发限制；Gitea 不执行 workflow。
