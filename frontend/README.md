# Vue 前端工程

Vue 3 + TypeScript + Vite + Element Plus，使用 Vue Router 与 Pinia。
当前页面是开发预览和实际 API 连接检查；没有模拟登录或模拟评测结果。

```bash
pnpm install --frozen-lockfile
pnpm dev
pnpm typecheck
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
