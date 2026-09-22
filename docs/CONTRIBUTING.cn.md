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
uv run ruff check --no-cache .
uv run ruff format --no-cache --check packages apps/studio/server scripts tests
uv run pyright
uv run pytest
```

修改 Studio 前端时，在 `apps/studio/web` 执行：

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm exec playwright install chromium
pnpm test:http
pnpm lint
pnpm test:browser
```

浏览器测试会先构建应用并启动专用预览服务，请确保 `4173` 端口空闲。Docker 集成与独立 wheel 检查见[仓库开发说明](cn/development.md)。未执行的检查请说明原因。

直接使用 `pnpm exec playwright test` 运行指定测试前，先执行 `pnpm build`。

## 本地 Git 检查

克隆后启用仓库钩子：

```bash
./scripts/install-git-hooks.sh
```

先安装 Git、uv、Node.js 和 pnpm，并按上文安装工作区和前端依赖。完整检查还需要
Chromium。Windows 需安装 Git for Windows，供 Git 执行钩子；在 PowerShell 中启用：

```powershell
.\scripts\install-git-hooks.ps1
```

`git commit` 检查暂存文件的格式、Lint 和直接相关的单元测试。`git push` 执行后端与前端
完整验证，包括前端构建和浏览器测试。也可在仓库根目录手动执行：

```bash
./scripts/verify-studio.sh
```

PowerShell 对应命令：

```powershell
.\scripts\verify-studio.ps1
```

提交检查要求工作区与暂存区一致，包括测试引用的文件；请先暂存或另行保存未暂存、未跟踪文件。
推送检查要求工作区干净，且所有非删除的推送目标都指向当前检出的提交。被 Git 忽略的依赖
和本地配置仍可使用。钩子不会自动暂存、隐藏或重写文件；手动验证脚本可检查尚未提交的工作。

CI 的 `web` 通过共用 Python 检查器执行单元测试、Lint、构建和打包检查。
`web-browser` 安装 Chromium 并将界面测试分为两个任务；第一分片还执行包含真实浏览器
上传测试的 `test:http`。`verify-studio-web.sh --skip-browser`（PowerShell：
`verify-studio-web.ps1 -SkipBrowser`）跳过 HTTP 测试和界面浏览器测试。

本地钩子可通过 Git 的 `--no-verify` 绕过；提交到远程的改动仍以仓库要求的 CI 检查为准。

## 许可证

贡献使用所修改包或目录的许可证。请保留第三方许可证与声明文件，参见 [LICENSE](../LICENSE)。
