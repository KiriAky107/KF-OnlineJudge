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
