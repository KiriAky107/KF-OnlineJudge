# identity

负责账号、登录凭证、会话与角色；向其他模块提供经过验证的用户身份和授权依据。
`public.py` 提供已验证主体，`service.py` 管理账号和可撤销会话，`repository.py` 封装持久化。
用户身份不能来自前端自报字段或模型参数。

本地管理命令为 `uv run --locked kf-accounts create <username>` / `disable <username>`。
创建口令由隐藏终端提示读取和确认，不接受命令行参数、环境中的默认口令或自助注册。
用户名为 3–24 个小写 ASCII 字母、数字或下划线；口令 12–128 字符，最多 512 UTF-8 字节。
密码摘要由新增的 `argon2-cffi` 依赖提供 Argon2id，固定 19 MiB/2 次/1 并行度。
参数依据 [OWASP](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
和 [argon2-cffi 文档](https://argon2-cffi.readthedocs.io/en/stable/parameters.html)。

登录接口为 `/api/v1/auth/login`，退出 `/logout`，当前主体 `/me`。会话使用随机 256-bit token，
数据库只保存 token / CSRF 摘要；默认 12 小时有效，单账号保留至多 10 个有效会话。
账号禁用立即拒绝登录和旧会话。请求只采用实际连接 peer，不信任客户端自报身份或转发地址。
每 peer 至多 10 次登录/15 分钟，预算通过真实数据库串行更新，失败尝试也消耗预算。
摘要计算最多同时 2 次，超出直接 429；限流表至多 4096 桶且每次有界回收过期桶。

有副作用的浏览器请求必须匹配 `KF_OJ_BROWSER_ORIGINS` 的显式来源；生产只接受 HTTPS 来源，
必须提供生产来源配置。会话 Cookie 为 HttpOnly / SameSite=Strict，生产 Secure；CSRF Cookie
供前端读出请求 token，在 `X-CSRF-Token` 携带，服务端核对其与当前会话绑定的摘要。
登录、主体和退出响应不缓存；请求校验错误不回显密码或输入值。API 层限制请求字节、块数和
读取期限。跨服务 Worker 凭证不使用用户 Cookie。
