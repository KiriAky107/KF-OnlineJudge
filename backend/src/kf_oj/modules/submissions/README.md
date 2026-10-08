# submissions

负责提交幂等请求、不可变源码引用、创建初始评测、历史查询与学生可见结果。
创建提交、评测和待投递事件必须处于同一事务。
协调器拥有领取后的执行状态与最终结果写入权；本模块不能绕过协议覆盖终态。
`queries.py` 提供已验证主体的私有源码读取，其他账号不可读取。
`worker_access.py` 核对当前 evaluation / attempt / Worker / fencing、未撤销及未过期凭证、
固定 submission 与版本及任务清单摘要；只允许读取该清单声明的工件。
内部 API 使用 WorkerLease bearer 和 X-Worker-ID / X-Fencing-Version，不使用用户会话授权。
允许状态为 claimed / compiling / running；取消请求、终态和旧 attempt 均拒绝。
字节准备后再次核对当前租约；最多同时两个有限读取，超过直接 429，不排无界等待队列。

任务清单为 UTF-8 JSON、对象键递归排序、无额外空白，并以这些确切字节的 SHA-256 标识。
Python/Rust `manifest_bytes` 与共享字节样例保持一致；Coordinator 必须用同一序列化定义存快照。
读取凭证与客户端提交的身份分开：身份和归属从实际数据库绑定获得，不接受客户端自报 user ID。

当前只实现读取与授权基础。提交创建/幂等/outbox、Coordinator 正式 claim、报告及用户成绩
查询仍在后续模块阶段，不能把测试数据库 fixture 的租约当作正式领取或实际执行证据。
