# Vue 前端工程

Vue 3 + TypeScript + Vite + Element Plus，使用 Vue Router 与 Pinia。
登录、已发布题库、题面、提交与个人成绩页面使用真实 API。训练、比赛、社区等
本轮以外页面仍为明确的开发预览，不提供这些尚未实现的业务能力。

```bash
pnpm install --frozen-lockfile
pnpm dev
pnpm typecheck
pnpm test
pnpm build
```

开发服务在 `http://127.0.0.1:5173`，`/api` 代理到 `http://127.0.0.1:8000`。
复制 `.env.example` 为 `.env` 可调整代理和 API 地址。
生产部署须配置 SPA 路由回退及 `/api` 反向代理；开发代理不进入生产产物。

- `views/`：路由页面；`layouts/`：页面框架；`components/`：可复用界面组件。
- `api/`：HTTP 客户端和业务 API 封装；`api/generated/`：从 OpenAPI 生成的类型。
- `composables/`：带生命周期的交互逻辑；`stores/`：跨页面共享状态。
- `router/`：路由；`styles/`：全局样式。

已有代码按实际需要落位；`components/` 和 `stores/` 是后续扩展位置，不放空状态实现。
API 类型更新执行根目录 `make contracts`；前端通过 `pnpm check:api` 拒绝过期类型。
登录凭据、模型密钥和数据库连接不能进入 `VITE_` 环境变量。

已有账号登录使用 HttpOnly/Strict cookie 与单独 CSRF cookie；401清除内存身份，
保护页面重新登录后继续。自助注册暂不开放。密码/会话不存入 localStorage。
浏览器与 API 使用同一 origin 的 `/api`，明确配置 `KF_OJ_BROWSER_ORIGINS`；
生产必须使用 HTTPS、安全 cookie 与独立部署的反向代理。

编辑器按题目版本和语言保留本地草稿，新增 Java 并保留原 C17/C++/Python/Rust 草稿。
可提交语言由服务端已发布版本的能力与语言配置决定，支持 C17/C++20、固定 CPython
3.13.16 的单文件 main.py/标准库，以及固定 OpenJDK 21.0.12.1+1-LTS 的默认包
Main.java/Main；Java 可包含受控的辅助、内部和匿名类。缺失或无效配置不能提交，
Rust 当前保留草稿且不可提交。Python 准备阶段只检查语法，不执行顶层代码；
Java 编译错误显示真实诊断，语言版本与预算取自服务端。请求不在线安装依赖。
提交凭据按账号/版本/内容 SHA-256 绑定，
POST前先保存 UUID，双击共用请求，网络超时/刷新重试复用原键；内容变化产生新键。
草稿和已保存凭据不因失败/退出而清除，本地存储不可用时拒绝发起无法稳定重试的提交。

详情和列表查询每2秒一次，每轮最多240次/10分钟；终态、错误或离页停止，隐藏页面
取消当前查询，恢复可见时继续原预算。路由变化/会话变化后的旧响应不能覆盖当前视图。
耗尽后可手动刷新；不会用预览成绩填补错误。取消受理与最终停止分别展示。
运行指标仅来自完整持久化报告，null显示“—”；诊断和源码按纯文本显示。
`pnpm test` 覆盖草稿、幂等重试、轮询期限/隐藏/终态/失效响应和401处理，
已纳入 `make check-frontend`；真实浏览器编译/判题另在受限独立 VM 验收。
