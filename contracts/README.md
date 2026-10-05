# 跨服务契约

`schemas/` 保存版本化 JSON Schema，`openapi/` 保存平台 API 快照。
共享合法/非法样例位于 `tests/fixtures/contracts/`，Python 与 Rust 必须共同验证。

目前只固定最小判题通知：`schema_version=1`、`kind=judge.evaluation`、UUID 格式的
`evaluation_id`。它不包含源码、隐藏数据、租约或执行授权；消费者仍须向协调器领取。
未知版本、未知类型及未知字段均拒绝。

`make contracts` 从 Python 模型生成 Schema/OpenAPI，并生成前端 API 类型。
`make check` 检查快照未过期及两种语言对样例的接受/拒绝一致。
执行请求/报告、评测状态机、租约与结果提交契约在 JS-01/DR-01 中逐项补充。

生成器 `export.py` 是开发工具，允许读取 API 组合层；运行期的契约模型禁止依赖业务模块。
