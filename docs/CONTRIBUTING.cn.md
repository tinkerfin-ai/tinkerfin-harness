# 参与 TinkerFin 开发

[English](../CONTRIBUTING.md)

可复现的问题和功能建议请提交到 [Issues](https://github.com/tinkerfin-ai/tinkerfin-harness/issues)。安全漏洞请私下发送至 **1090116461@qq.com**，参见 [SECURITY.md](../SECURITY.md)。

## 提交修改

1. Fork 仓库，从 `main` 创建自己的分支。
2. 每次修改围绕一个明确目标。修复缺陷时增加回归验证，并更新受影响的文档。
3. 向 `tinkerfin-ai/tinkerfin-harness:main` 提交 PR，说明行为变化及执行过的检查。
4. 解决审查讨论，等待必需检查通过，由维护者审查并合并。

贡献者无需获得原仓库的写权限。请勿提交凭据、私密配置、含个人数据的日志或构建产物。

## 运行检查

在仓库根目录执行：

```bash
uv sync --locked --all-packages --group dev
uv run ruff check .
uv run ruff format --check packages apps/studio/server scripts tests
uv run pyright
uv run pytest
```

修改 Studio 前端时，在 `apps/studio/web` 执行：

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm exec playwright install chromium
pnpm test:proxy
pnpm lint
pnpm test:browser
```

浏览器测试会先构建应用并启动专用预览服务，请确保 `4173` 端口空闲。Docker 集成与独立 wheel 检查见[仓库开发说明](cn/development.md)。未执行的检查请说明原因。

直接使用 `pnpm exec playwright test` 运行指定测试前，先执行 `pnpm build`。

## 许可证

贡献使用所修改包或目录的许可证。请保留第三方许可证与声明文件，参见 [LICENSE](../LICENSE)。
