# TinkerFin Studio Web

Studio 的 React + TypeScript 客户端。后端启动与模型配置见 [Studio 上手指南](../../../docs/cn/studio/quick_start.md)。

## 本地运行

需要 Node.js 20.19+（20.x）或 22.12+，以及 pnpm 10.8.0。在本目录执行：

```bash
pnpm install --frozen-lockfile
pnpm dev
```

打开终端显示的地址，默认 `http://127.0.0.1:5190`，也可使用 `http://localhost:5190`。登录页的“服务器地址”留空时连接 `http://127.0.0.1:8090`；填写完整 HTTP(S) 地址后自动保存在当前浏览器，刷新后保留。切换地址后需要重新登录。

浏览器直接访问服务器，后端允许任意前端来源；HTTPS 页面需连接 HTTPS 服务器。

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

首次运行 HTTP 或浏览器测试前，安装 Chromium：

```bash
pnpm exec playwright install chromium
pnpm test:http       # HTTP 与附件直传测试
pnpm test:browser     # 自动构建，运行交互与无障碍测试
```

单独运行浏览器用例：

```bash
pnpm exec playwright test tests/browser/conversation-failure.spec.ts
```

浏览器测试会构建到独立临时目录，结束时清理预览服务和构建文件。预览端口默认使用 4173，端口被占用时可指定其他端口：

```bash
PLAYWRIGHT_PORT=4273 pnpm test:browser
```
