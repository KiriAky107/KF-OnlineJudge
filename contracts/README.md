# 跨服务契约

`schemas/` 保存版本化 JSON Schema，`openapi/` 保存平台 API 快照。
共享合法/非法样例位于 `tests/fixtures/contracts/`，Python 与 Rust 必须共同验证。

目前只固定最小判题通知：`schema_version=1`、`kind=judge.evaluation`、UUID 格式的
`evaluation_id`。它不包含源码、隐藏数据、租约或执行授权；消费者仍须向协调器领取。
未知版本、未知类型及未知字段均拒绝。

`make contracts` 从 Python 模型生成 Schema/OpenAPI，并生成前端 API 类型。
`make check` 检查快照未过期及两种语言对样例的接受/拒绝一致。
执行器私有规格与 FD 协议属于 Sandbox；平台不接收 argv、宿主路径或执行镜像路径。

普通题提交协议现有 `judge-manifest`、`judge-lease`、`judge-stage`、`judge-result`
和本地可信 `judge-admission` v1 模型。清单绑定源码、不可变题目版本、按序工件摘要、
C17/C++20、固定 GNU 工具链版本及编译/运行预算；租约绑定 evaluation/attempt/Worker
与 fencing 版本。Root 权限、执行和 DB 状态转换接口仍由各自服务实现，解析清单或租约
不授予执行权。

Python/Rust `decode_wire` 在解析前限制 256 KiB UTF-8 JSON，拒绝重复字段与非法数据，
传输边界错误不回显输入。令牌及编译诊断的默认调试输出隐藏内容。`serde_json` 的运行期
直接依赖用于此有界解码，不引入 IO、网络或数据库。

Schema 描述结构；模型另外核对规范 UUID、严格整数（拒绝 `1.0`/布尔值）、工件总大小、
重复测试点、预算相容、诊断 UTF-8 字节数、编译状态/产物、清理失败和实际结果汇总。
JSON Schema 的 integer 按数值判断，允许 `1.0`；不能替代协议解码。共享样例中的
`invalid-semantic-*` 结构合法但被两种语言模型拒绝，其他非法样例同时验证结构拒绝。
样例全为合成元数据；其中令牌、摘要不是已签发凭证或实际隐藏测试数据。

Coordinator 必须在事务锁内核对当前租约及状态规则，并将结果和授权清单比较：
完整用例次序、数量和清单摘要不能只靠报告自身校验。已提交报告的幂等确认与过期租约
拒绝分别处理，旧 attempt 无权改写结果。`cancelling` 在实际停止确认前不得转为
`cancelled`；清理失败必须保留独立故障，缺失计量始终为 null。

生成器 `export.py` 是开发工具，允许读取 API 组合层；运行期的契约模型禁止依赖业务模块。
