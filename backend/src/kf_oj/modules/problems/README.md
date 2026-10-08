# problems

负责题目逻辑身份、不可变版本、导入、标签、限制、发布状态与测试工件元数据。
跨模块通过 `public.py` 的不可变判题投影；大文件存取通过 `infra/artifacts` 的适配器。
公开题面/显式样例与隐藏输入/答案分别授权。公开 API 不返回工件摘要或存储路径。

`uv run --locked kf-problems <directory>` 导入并发布一个新版本。目录必须属于当前操作账号，
权限 0700；`problem.json` 遵循 `import_format.py`，只引用该目录下的单层普通文件名。
不解压归档、不跟随链接、不读取任意路径、不执行导入内容。默认清单可由 `--manifest` 指定。
要求配置 `KF_OJ_ARTIFACT_ROOT` 和可信 `KF_OJ_JUDGE_ADMISSION` JSON；同一准入配置应交付给节点，
不能由题目放宽。结构示例来自共享 `judge-admission.valid.json`，不是当前宿主隔离验收证明。

清单固定 C17/C++20、工具链/镜像版本、精确字节比较、编译/运行/整个作业预算，并声明按序
测试点 `{key,input_file,expected_file}` 和最多十对公开样例 `{input_file,output_file}`。
题面以 `statement_file` 读取 UTF-8。每份样例至多 64 KiB，响应采用 base64 保留二进制数据。
同一 slug 受事务锁保护，每次发布递增 revision；发布后题面、限额、样例、测试点不可修改。
旧评测继续绑定旧版本；当前列表指向新版本。工件先持久化，再在同一事务建立引用和发布。

`GET /api/v1/problems` 以 slug 游标分页（`after` / `limit`，最多 100 项），
`GET /api/v1/problems/versions/{uuid}` 读取已发布版本。禁用逻辑题目时其版本对公开读取不可用。
内部判题投影仅供授权应用编排使用；外部 Worker 读取由 submissions 的有效租约端口控制。
