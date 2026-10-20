# TinkerFin 客户端

在 Apple Silicon Mac 上为 Studio 会话执行文件、命令、浏览器、桌面和已授权的 MCP 工具。模型由 Studio 服务端调用。应用包含 Python、Node.js、原生沙箱和浏览器工具，需要 macOS 15 或更新版本。网页操作优先使用系统 Chrome；没有可用版本时自动下载独立浏览器，并始终使用客户端自己的浏览器资料目录。

## 安装与连接

1. 从 [GitHub Releases](https://github.com/tinkerfin-ai/tinkerfin-harness/releases) 下载 TinkerFin Desktop 的 `TinkerFin.zip`，解压并将 `TinkerFin.app` 移到“应用程序”。无需安装开发工具或自行编译。
2. 打开客户端，在菜单栏点击“连接”，填写 Studio 网页地址，再在浏览器登录并授权。API 使用独立地址时，可展开高级设置填写；地址不含 `/api`。
3. 客户端显示“已连接”后，在 Studio 网页选择本地设备。完整流程见[本机协作指南](../../docs/cn/studio/local_devices.md)。

应用使用 ad-hoc 签名，**未通过 Apple 公证**。macOS 阻止打开时，核对下载来源后，在“系统设置 → 隐私与安全性”中找到 TinkerFin 并选择“仍要打开”；不要关闭系统安全保护；操作见 [Apple 说明](https://support.apple.com/en-us/102445)。ZIP 的 SHA-256 文件随发行包提供。

在设置中的服务地址旁点击“更换”可连接其他 Studio；新授权成功后停止原服务的本机执行并切换。地址须使用 HTTPS，HTTP 仅允许本机回环开发地址。网页部署应设置 `VITE_STUDIO_API_ADDRESS`，供客户端发现 API；无法发现时需要手动填写。

## 开发与构建

构建需要 Apple Silicon Mac、macOS 15 SDK、Swift 6 和 uv。在仓库根目录执行：

```bash
uv sync --locked --all-packages
uv run python -m scripts.desktop_dev
```

开发入口按当前桌面源码及其依赖选择应用；输入变化时重建，只有 Studio 服务端或无关网页代码变化时复用已有应用。`--build-only` 只构建。切换工作树前先退出正在运行的 TinkerFin，避免多个客户端占用同一设备状态。

生成独立分发包：

```bash
uv run python apps/desktop/scripts/build_macos.py --output dist/TinkerFin.app
```

输出路径及同名 ZIP 必须不存在。构建从当前源码制作 wheel，按 `uv.lock` 安装 Python 依赖，按 npm 锁文件安装官方 Playwright MCP、Playwright 和原生沙箱；无需单独编译浏览器工具。

固定的 Python 3.11.14 与 Node.js 22.23.3 官方分发包缓存在 `~/Library/Caches/TinkerFin/build-inputs`，每次使用均校验 SHA-256，多个工作树可共用。`--cache` 可指定缓存位置；离线时可通过 `--python-archive`、`--node-archive` 指定同一固定分发包。缓存损坏时命令会报出文件路径，移除该文件后重新下载。

输出包含 `.app`、ZIP 和 ZIP 摘要，应用内保存第三方许可证与依赖清单。默认 ad-hoc 签名；构建不会安装或上传产物。替换应用后 macOS 可能要求重新认证 Keychain，完成系统认证后仍无法读取凭证时，重新连接设备。

## 发布

`Desktop release` 工作流只由 `desktop-vX.Y.Z` 标签或选择现有标签的手动操作触发。标签版本必须与 `pyproject.toml` 中的桌面版本一致。工作流在 Apple Silicon 上执行检查、构建和完整应用验收，校验上传附件后才公开 Release。普通推送和 PR 只运行受影响的检查。

```bash
git tag desktop-v0.1.1
git push origin desktop-v0.1.1
```

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

辅助功能、屏幕录制、真实桌面动作和系统权限授权及应用替换，需要在获授权的 macOS 会话中分别验收。原生沙箱不能保证终止主动脱离进程组的后台工作；停止后的不确定结果必须在本机核验。

## 数据与卸载

客户端默认读取 `~/.tinkerfin/settings.json`；工具执行状态保存在 `~/.tinkerfin/state/`，技能暂存文件保存在 `~/.tinkerfin/state-inputs/`，设备凭证存放在 Keychain。项目文件位于 `~/.tinkerfin/projects/<服务地址摘要>/<账号命名空间>/<项目 ID>/`，与私有配置和工具执行状态隔离。移除应用不会删除项目文件或工具执行状态。

卸载前在 Studio 的“设置 → 本地设备”中撤销设备授权，在 Mac 菜单栏菜单中选择“退出 TinkerFin”，再移除应用。需要彻底清理状态时，先确认没有仍需核验的操作，再由用户删除应用状态目录、技能暂存目录及 Keychain 中对应的 TinkerFin 设备连接条目。切换账号保留原项目目录，切回原账号继续使用。

## 许可证

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
