# 数据库所有权

Alembic 是唯一的 schema 入口。先对专用 PostgreSQL 18 数据库运行 `uv run --locked alembic
upgrade head`，API 不在启动时创建表。迁移账号拥有该数据库对象并需要 `CREATEROLE`；它不作为
任何运行服务账号。生产配置及登录角色由部署方提供，不在源码里设置默认数据库口令。

初始迁移建立两个受限的 `NOLOGIN` 权限组：`kf_oj_platform` 和 `kf_oj_coordinator`。
平台组创建 submission / queued evaluation / outbox，不能指定或更新执行状态、attempt 或成绩。
协调器组领取和更新评测、插入 attempt / 用例成绩及恢复通知，不能修改账号、题库或提交身份。
Worker 没有数据库角色，也不需要数据库连接配置。

服务使用各自独立的普通 `LOGIN` 角色，分别加入对应权限组；登录角色不能拥有数据库/表、
不能加入迁移账号或其他高权限组，且必须为 `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`。
初始迁移拒绝已有的高权限同名组和继承其他角色的同名组。应用启动时不运行 migration。
降级删除本项目业务表与触发器，保留权限组及操作方配置的登录账号，避免删除集群级身份。

发布是一次单向转换：先在事务内创建版本和连续测试点，再设置 `published_at`。
数据库触发器拒绝再次发布、修改已发布版本和追加/改写已发布测试点；评测通过复合外键
永久绑定 submission 的版本，当前 attempt 和用例成绩也由复合外键绑定同一评测及版本。
工件引用具有数据库生成的 `ready` 状态，引用存在时不能转为 `deleting` 或删除元数据。

真实数据库验收使用显式 `KF_OJ_TEST_POSTGRES` 管理连接。每个测试只创建和删除随机命名的
`kf_oj_test_*` 数据库，不清空已有业务库。例行无数据库测试会明确跳过这些验收；跳过不算通过。
