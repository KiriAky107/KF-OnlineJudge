# identity

负责账号、登录凭证、会话与角色；向其他模块提供经过验证的用户身份和授权依据。
`public.py` 提供已验证主体，`service.py` 管理账号和可撤销会话，`repository.py` 封装持久化。
用户身份不能来自前端自报字段或模型参数。

本地管理命令为 `uv run --locked kf-accounts create <username>`。
创建口令由隐藏终端提示读取和确认，不接受命令行参数、环境中的默认口令或自助注册。
用户名为 3–24 个小写 ASCII 字母、数字或下划线；口令 12–128 字符，最多 512 UTF-8 字节。
密码摘要由新增的 `argon2-cffi` 依赖提供 Argon2id，固定 19 MiB/2 次/1 并行度。
参数依据 [OWASP](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
和 [argon2-cffi 文档](https://argon2-cffi.readthedocs.io/en/stable/parameters.html)。

登录接口为 `/api/v1/auth/login`，退出 `/logout`，当前主体 `/me`。会话使用随机 256-bit token，
数据库只保存 token / CSRF 摘要；默认 12 小时有效，单账号保留至多 10 个有效会话。
账号禁用立即拒绝登录和旧会话。请求只采用 ASGI 连接 peer，不读取客户端转发地址。

`0005_identity_authority` 为旧账号保留普通用户默认值，并加入管理员标记与服务端派生的授权版本。
启用状态或管理员标记实际变化时，同一事务递增授权版本并撤销旧会话；事务回滚也回滚撤销。
平台账号仅可插入普通账号列、更新启用状态，不能写管理员标记或授权版本；管理员的启用状态
需要独立本地运维身份。`kf_oj_identity_operator` 与 `kf_oj_problem_import` 为无登录的独立权限组，
口令与登录身份由运维另行配置，平台不继承运维组。导入组目前没有表权限，后续导入迁移只授予
队列与候选所需权限，不授予评测状态写入权。迁移不创建默认管理员。
保留授权或撤销历史后拒绝回退这次迁移。

`0006_authority_operations` 将运维组进一步收敛为固定函数的 EXECUTE 权限；不能直接读取
口令/会话摘要或修改账号/会话。函数由独立 NOLOGIN 角色持有，仅有授权所需列权限，固定
`search_path` 并逐项限定操作。所有账号写操作在行锁前更新同一个事务守卫；撤销/禁用/删除
最后一名启用管理员由数据库拒绝。重复授予/撤销不递增授权版本，实际变更仍原子撤销会话。
旧可重复读快照的并发变更产生序列化失败，调用方须结束整个事务后重新操作；不吞掉失败。
并发语义参见[PostgreSQL事务隔离](https://www.postgresql.org/docs/18/transaction-iso.html)，
固定函数的权限与搜索路径参见[CREATE FUNCTION](https://www.postgresql.org/docs/18/sql-createfunction.html)。

在单独的本地运维环境配置 `KF_OJ_IDENTITY_OPERATOR_DATABASE_URL`，使用具有
`kf_oj_identity_operator` 权限组的普通 LOGIN，禁止超级用户、建库/建角色/复制/绕过 RLS
以及平台/协调器/导入组成员。API、Worker 和常规服务不配置此凭据，CLI 不回退平台身份。
先显式创建普通账号，再运行 `uv run --locked kf-accounts bootstrap-admin <username>`。
没有启用管理员时只有明确 bootstrap 可首次授权；不创建默认账号或口令。已有管理员后使用
`grant-admin` / `revoke-admin` / `disable` / `enable`。恢复已禁用管理员使用 `enable`；恢复失去
全部管理员的受控数据库备份时可再次显式 bootstrap。最后一名启用管理员不能被撤销或禁用，
应先授予另一名账号。操作的锁等待最多 2 秒、SQL 最多 5 秒，CLI 只输出账号/能力/授权版本和
是否变化；错误不输出连接串、SQL 参数或驱动诊断。网页管理 API 与题包导入仍待后续实现。

当前主体与登录响应附带版本为 1 的 `capabilities`，其中维护能力和十进制授权版本来自当前
有效会话关联的账号；浏览器只展示这些信息，不能提交它们来获得授权。模块公开端口
`ProblemMaintenancePort.require_problem_maintenance` 在调用方写事务中重新检查账号、授权版本
与实时会话，并按账号→会话的顺序持有 SHARE 锁直到提交/回滚，防止验证之后撤权的竞态。
普通构造的 `Principal` 默认没有维护能力；会话摘要仅在服务端主体内传递，不进入 DTO 或 repr。

受控启动使用 `--no-proxy-headers`，避免服务器中间件预先替换 peer；反向代理后的用户共享
该连接预算，引入可信代理的独立限流策略之前不能直接打开客户端转发头信任。
每 peer 至多 10 次登录/15 分钟，预算通过真实数据库串行更新，失败尝试也消耗预算。
摘要计算最多同时 2 次，超出直接 429；限流表至多 4096 桶且每次有界回收过期桶。

有副作用的浏览器请求必须匹配 `KF_OJ_BROWSER_ORIGINS` 的显式来源；生产只接受 HTTPS 来源，
必须提供生产来源配置。会话 Cookie 为 HttpOnly / SameSite=Strict，生产 Secure；CSRF Cookie
供前端读出请求 token，在 `X-CSRF-Token` 携带，服务端核对其与当前会话绑定的摘要。
登录、主体和退出响应不缓存；请求校验错误不回显密码或输入值。API 层限制请求字节、块数和
读取期限。跨服务 Worker 凭证不使用用户 Cookie。
