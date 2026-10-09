# submissions

负责提交幂等请求、不可变源码引用、创建初始评测、历史查询与学生可见结果。
创建提交、评测和待投递事件必须处于同一事务。
协调器拥有领取后的执行状态与最终结果写入权；本模块不能绕过协议覆盖终态。
`queries.py` 提供已验证主体的提交历史、详情与私有源码读取，其他账号不可读取；
历史按创建时间/UUID降序游标翻页，一次至多100条。读取完整成绩时严格核对报告契约和
评测绑定，仅投影允许公开的诊断/计量/测试点序号。取消通过注入的 Coordinator 控制
端口接受意图，不更新执行状态；202与清理完成的终态不同。
`worker_access.py` 核对当前 evaluation / attempt / Worker / fencing、未撤销及未过期凭证、
固定 submission 与版本及任务清单摘要；只允许读取该清单声明的工件。
内部 API 使用 WorkerLease bearer 和 X-Worker-ID / X-Fencing-Version，不使用用户会话授权。
允许状态为 claimed / compiling / running；取消请求、终态和旧 attempt 均拒绝。
字节准备后再次核对当前租约；最多同时两个有限读取，超过直接 429，不排无界等待队列。

任务清单为 UTF-8 JSON、对象键递归排序、无额外空白，并以这些确切字节的 SHA-256 标识。
Python/Rust `manifest_bytes` 与共享字节样例保持一致；Coordinator 必须用同一序列化定义存快照。
读取凭证与客户端提交的身份分开：身份和归属从实际数据库绑定获得，不接受客户端自报 user ID。

`service.py` 按账号与幂等UUID绑定版本/语言/源码摘要，持久化源码后同一事务创建
submission、queued evaluation 和 outbox；同键同内容复用既有提交，不同内容409。
待评测固定总量64、个人8，取消清理中也占用容量；配置或容量不足明确拒绝。
Coordinator 通过正式 claim 创建短期领取权，Worker 不写业务数据库。
真实服务链与安全条件必须在独立 VM 验收；数据库 fixture 的租约或合成报告只能
验证控制规则、授权和投影，不能作为真实程序执行/停止证明。
