# TinkerFin Studio Web

Studio 的 React + TypeScript 客户端。后端启动与模型配置见 [Studio 上手指南](../../../docs/cn/studio/quick_start.md)。

## 本地运行

需要 Node.js 20.19+（20.x）或 22.12+，以及 pnpm 10.8.0。在本目录执行：

```bash
pnpm install --frozen-lockfile
pnpm dev
```

打开终端显示的地址，默认 `http://127.0.0.1:5173`，也可使用 `http://localhost:5173`。开发服务器将 `/api` 代理到 `http://127.0.0.1:8090`。连接其他后端时：

```bash
VITE_API_PROXY_TARGET=http://127.0.0.1:8092 pnpm dev
```

构建并本地预览：

```bash
pnpm build
pnpm preview
```

## 测试与检查

```bash
pnpm lint             # 静态检查
pnpm test             # 单元测试
```

首次运行代理或浏览器测试前，安装 Chromium：

```bash
pnpm exec playwright install chromium
pnpm test:proxy       # HTTP 代理与附件上传测试
pnpm test:browser     # 自动构建，运行交互与无障碍测试
```

单独运行浏览器用例前先构建：

```bash
pnpm build
pnpm exec playwright test tests/browser/conversation-failure.spec.ts
```

浏览器测试独占本地预览端口，默认使用 4173。端口被占用时可指定其他端口：

```bash
PLAYWRIGHT_PORT=4273 pnpm test:browser
```
