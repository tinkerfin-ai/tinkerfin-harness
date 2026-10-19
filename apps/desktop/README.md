# TinkerFin 客户端

在 Apple Silicon Mac 上为 Studio 会话执行文件、命令、浏览器、桌面和已授权的 MCP 工具。模型由 Studio 服务端调用。应用包含 Python、Node.js、原生沙箱和浏览器工具，需要 macOS 15 或更新版本。网页操作优先使用系统 Chrome；没有可用版本时自动下载独立浏览器，并始终使用客户端自己的浏览器资料目录。

在本机客户端点击“连接”，再通过浏览器授权连接 Studio 账号。在 Studio 网页选择本地设备后，客户端自动准备项目工作区；设备名称、语言与系统权限可在客户端设置中管理。使用流程见[本机协作指南](../../docs/cn/studio/local_devices.md)。

## 构建应用

构建需要 Apple Silicon Mac、macOS 15 SDK、Swift 6、uv、Node.js ≥22.12 与 npm，以及以下固定运行时：

- [Python 3.11.14 完整发行包（20251031，Apple Silicon，pgo+lto）](https://github.com/astral-sh/python-build-standalone/releases/download/20251031/cpython-3.11.14%2B20251031-aarch64-apple-darwin-pgo%2Blto-full.tar.zst)，包含静态依赖许可证；构建脚本校验固定 SHA-256
- [Node.js 22.23.3 darwin-arm64 官方发行包](https://nodejs.org/dist/v22.23.3/node-v22.23.3-darwin-arm64.tar.gz)，构建脚本校验固定 SHA-256
- `runtime/package-lock.json` 锁定的原生沙箱依赖
- `agent-browser` 0.33.2 的托管构建，包含与程序匹配的技能内容及许可证

从仓库根目录准备依赖和浏览器工具：

```bash
uv sync --locked --all-packages
npm ci --prefix apps/desktop/runtime --ignore-scripts
uv run python apps/desktop/scripts/build_browser_cli.py \
  --output /path/to/browser-runtime/agent-browser
```

在 `apps/desktop/resources/connection.json` 中设置部署地址：

```json
{
  "studio_url": "https://studio.example.com",
  "api_url": "https://api.example.com"
}
```

`studio_url` 是浏览器打开的 Studio 地址；`api_url` 是服务端根地址，不含 `/api`。构建 Studio 网页时，将 `VITE_STUDIO_API_ADDRESS` 设置为相同的 `api_url`。地址随应用构建固定，只有本机开发的回环地址允许使用 HTTP。

将运行时和浏览器工具路径替换为实际位置。浏览器工具旁的构建清单和 `agent-browser-assets` 目录必须保留。输出路径及其同名 ZIP 必须不存在：

```bash
uv run python apps/desktop/scripts/build_macos.py \
  --python-archive /path/to/cpython-3.11.14-aarch64-full.tar.zst \
  --node-archive /path/to/node-v22.23.3-darwin-arm64.tar.gz \
  --browser-cli /path/to/browser-runtime/agent-browser \
  --configuration apps/desktop/resources/connection.json \
  --output 'dist/TinkerFin.app'
```

构建从当前源码制作 wheel，并从 `uv.lock` 安装第三方依赖。浏览器工具构建固定上游提交与 Rust 工具链，应用构建核验程序、托管补丁、技能文件和许可证的摘要。输出包含 `.app`、ZIP 和 ZIP 摘要；应用内保留依赖许可证与清单。运行用户无需安装 Python、Node 或 Docker。

默认使用 ad-hoc 签名，供开发验收。正式分发使用 `--identity 'Developer ID Application: …'`，然后在发布环境完成 Apple 公证与 stapling。ad-hoc 构建不提供跨构建的稳定 Keychain 身份；替换开发包后，macOS 可能要求认证才能读取已有设备凭证。先在系统提示中完成认证，凭证仍不可用时再重新连接。构建不会安装到 `/Applications` 或上传产物。

## 验证

```bash
uv run pytest apps/desktop/tests packages/tinkerfin-sandbox/tests/native packages/tinkerfin-remote/tests
uv run ruff check --no-cache apps/desktop
uv run ruff format --no-cache --check apps/desktop
uv run pyright apps/desktop/src apps/desktop/tests apps/desktop/scripts
TINKERFIN_TEST_APP='/absolute/path/TinkerFin.app' \
  uv run pytest apps/desktop/tests/test_app_bundle.py
```

原生测试需要 macOS 与对应运行时。打包测试仅使用指定应用和临时状态目录，不申请桌面权限，不读取用户已有 Keychain 凭证，也不调用真实模型。

自动化浏览器继承本机工作区的文件、账号和进程隔离。macOS 不允许进程再次应用沙箱，专用 Chrome 因此使用 `--no-sandbox`，不具备 Chrome 自身额外的渲染器沙箱；这不影响日常浏览器的启动配置。

辅助功能、屏幕录制、真实桌面动作和正式签名下的升级，需要在获授权的 macOS 会话中分别验收。原生沙箱不能保证终止主动脱离进程组的后台工作；停止后的不确定结果必须在本机核验。

## 数据与卸载

客户端默认读取 `~/.tinkerfin/settings.json`；工具执行状态保存在 `~/.tinkerfin/state/`，技能暂存文件保存在 `~/.tinkerfin/state-inputs/`，设备凭证存放在 Keychain。项目文件位于 `~/.tinkerfin/projects/<服务地址摘要>/<账号命名空间>/<项目 ID>/`，与私有配置和工具执行状态隔离。移除应用不会删除项目文件或工具执行状态。

卸载前在 Studio 的“设置 → 本地设备”中撤销设备授权，在 Mac 菜单栏菜单中选择“退出 TinkerFin”，再移除应用。需要彻底清理状态时，先确认没有仍需核验的操作，再由用户删除应用状态目录、技能暂存目录及 Keychain 中对应的 TinkerFin 设备连接条目。切换账号保留原项目目录，切回原账号继续使用。

## 许可证

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
