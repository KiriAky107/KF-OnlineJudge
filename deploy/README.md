# 本地开发依赖

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

`kf-worker judge-submission` 是受控本地适配入口，仍不消费队列。
它验证不可变清单摘要，C17/C++20 仅映射到固定 GNU 命令，编译一次；
成功产物经完整清理后封印并计算 SHA-256，每点分别申请 helper、stdin 与工作区。
期望值逐点读取，留在比较端，不传给工厂/用户文件系统。
新增锁定 `sha2 0.10.9` 仅供 Worker 验证输入及编译产物摘要；
Sandbox 同版本仅为独立 VM 测试数据生成的开发依赖，Engine 不依赖加密或 OS 库。
