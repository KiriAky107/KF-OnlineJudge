# problems

负责题目逻辑身份、不可变版本、导入、标签、限制、发布状态与测试工件元数据。
跨模块通过 `public.py` 的不可变判题投影；大文件存取通过 `infra/artifacts` 的适配器。
公开题面/显式样例与隐藏输入/答案分别授权。公开 API 不返回工件摘要或存储路径。

`uv run --locked kf-problems <directory>` 导入并发布一个新版本。目录必须属于当前操作账号，
权限 0700；`problem.json` 遵循 `import_format.py`，只引用该目录下的单层普通文件名。
不解压归档、不跟随链接、不读取任意路径、不执行导入内容。默认清单可由 `--manifest` 指定。
要求配置 `KF_OJ_ARTIFACT_ROOT` 和可信 `KF_OJ_JUDGE_ADMISSION` JSON；同一准入配置应交付给节点，
不能由题目放宽。结构示例来自共享 `judge-admission.valid.json`，不是当前宿主隔离验收证明。

v1 清单固定 C17/C++20、工具链/镜像版本、精确字节比较、编译/运行/整个作业预算，并声明按序
测试点 `{key,input_file,expected_file}` 和最多十对公开样例 `{input_file,output_file}`。
题面以 `statement_file` 读取 UTF-8。每份样例至多 64 KiB，响应采用 base64 保留二进制数据。
同一 slug 受事务锁保护，每次发布递增 revision；发布后题面、限额、样例、测试点不可修改。
旧评测继续绑定旧版本；当前列表指向新版本。工件先持久化，再在同一事务建立引用和发布。

`GET /api/v1/problems` 以 slug 游标分页（`after` / `limit`，最多 100 项），
`GET /api/v1/problems/versions/{uuid}` 读取已发布版本。禁用逻辑题目时其版本对公开读取不可用。
内部判题投影仅供授权应用编排使用；外部 Worker 读取由 submissions 的有效租约端口控制。


v2 使用 `schema_version: 2` 与 `language_configurations`，每个配置包含 `language`、
完整 `toolchain` 身份、`compile_limits`、`run_limits` 和 `job_wall_time_us`。
可分别声明 `c17`、`cpp20`、`python313`、`java21`；不再同时提供 v1 顶层工具链或预算。
所有语言按可信节点配置准入，缺失能力/错误补丁版本/镜像摘要/过宽预算在发布前拒绝。
`judge.admission.example.json` 的镜像摘要为占位值，使用前替换为管理员制作镜像的实际摘要。
公开题面仅显示固定语言、版本、入口与预算，不公布宿主镜像路径和隐藏工件。

导入同一 slug 的 v2 版本保持原 v1 版本与提交不变。已有提交按其保存的协议版本解析，
不能重新序列化旧 canonical 数据来补入新字段。Java 使用 Main.java 与默认包 Main；
Python 使用 main.py。每个新版本的语言集合独立，浏览器不得为旧版本补开新语言。


Web题包导入是独立的待发布流程：`/api/v1/admin/problem-imports` 使用真实维护会话，
写入还要求明确Origin与绑定CSRF。先以稳定操作UUID、ZIP摘要和精确字节数建立容量预约，
再向该操作的 `/archive` 流式PUT；同键重试仍验证实际内容，不会确认异内容。
列表、详情和丢弃只开放给该操作的管理员本人，响应仅含公开题面、样例、语言预算与有限错误。
`ready` 尚未发布，不会加入学生题库，也不改变既有题目版本。

Web格式固定单层ZIP的stored/deflate，清单名为`problem.json`；公开文件与隐藏用例文件
必须显式分别声明且不得混用。文件名只接受有界ASCII单层普通名；未知成员、链接/设备、
加密、嵌套归档、额外前缀/尾随内容、损坏CRC与虚假大小被拒。旧受控目录CLI格式保持原义。
压缩与实际解压各64 MiB、最多534文件，上传最多4096帧/每帧1 MiB/30秒；
解析期限60秒。staging总预约256 MiB，每次128 MiB，单全局上传/解析槽位，
本人最多10个/全局32个活跃候选，操作24小时到期，解析死亡最多领取两次。
真实限额由管理capabilities返回；缺少配置或新鲜Worker资源证明时明确不可用。

`uv run --locked kf-problem-import-worker` 是普通用户、独立数据库登录的组合入口。
配置`KF_OJ_IMPORT_STAGING_ROOT`、`KF_OJ_ARTIFACT_ROOT`、可信准入和
`KF_OJ_IMPORT_WORKER_CGROUP`；staging与工件目录必须归服务用户所有且权限0700。
服务不会sudo、执行上传内容/学生源码、联网安装依赖或读写评测状态。启动及每次解析
读回128 MiB/swap0/PIDs16/CPU不超过一核及文件/FD/core上限，错误身份/无界环境拒绝服务。
解析子进程继承同一个内核文件锁，父进程死亡不能让GC在存活子进程仍持有文件时释放容量。
租约与fence防止旧结果写入；完整工件持久化、候选和引用原子落库后才返回ready。
丢弃/失败/到期先清理私有暂存字节再释放预约，保留不可变操作记录与追加式有限审计。

资源约束服务模板见`deploy/problem-import-worker.service.example`。API和Worker需可访问
同一普通用户的私有目录；数据库采用各自最小角色，导入Worker只通过固定身份公开端口
验证已受理的维护授权。PrivateNetwork模板要求Unix PostgreSQL socket；实际部署前由运维
设置外部私有配置、专用登录与目录，不包含默认口令，也不代表本轮已经生产部署。
