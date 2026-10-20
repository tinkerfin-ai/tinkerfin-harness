# 仓库开发

[English](../en/development.md)

在仓库根目录执行以下命令。需要 Python 3.11 或更高版本以及 uv。

提交 PR 和运行常规 Python、Studio Web 检查的步骤见[参与开发](../CONTRIBUTING.cn.md)。

## 安装工作区

```bash
uv sync --locked --all-packages --group dev
```

开发依赖按任务分组：`test` 提供测试运行器与契约 Fixture，`lint` 提供 Ruff 和 Pyright，
`packaging` 提供打包验证工具。`dev` 聚合这三组，开发依赖不会作为包依赖发布。

使用 `--no-default-groups --group NAME` 可选择安装的组。常规 Python 与 Studio 检查需要
`test` 和 `lint`。安装 `packaging` 组及全部工作区包后，打包套件可通过 `--noconftest`
独立运行。选择性安装后使用 `uv run --no-sync`，保持已选择的环境。

### 本地按需使用清华源

默认安装使用官方 PyPI。本地需要镜像时，在仓库根目录执行以下命令；首次安装先运行 `uv venv` 创建 `.venv`：

```bash
uv export --locked --all-packages --group dev --output-file /tmp/tinkerfin-dev-requirements.txt > /dev/null
uv pip sync --default-index https://pypi.tuna.tsinghua.edu.cn/simple /tmp/tinkerfin-dev-requirements.txt
```

该命令按锁定版本安装并校验已记录的包哈希，不修改 `uv.lock` 或全局源配置。

## 应用与开发环境

应用按职责放在 `apps/desktop`、`apps/studio-server` 和 `apps/studio-web`；框架包留在 `packages/`。

准备好[服务端配置与依赖](../../apps/studio-server/README.md)后，在 macOS 或 Linux 上管理当前工作树的服务：

```bash
pnpm --dir apps/studio-web install --frozen-lockfile
uv run python -m scripts.dev start
uv run python -m scripts.dev status
uv run python -m scripts.dev stop
```

首次启动可用 `--env-file`、`--api-port`、`--web-port` 选择配置与端口，之后复用该环境记录。每个工作树独立记录进程身份，端口冲突会明确报错，不会停止占用端口的其他程序。日志位于 `.cache/dev-environment/`。

`start --dependencies` 按指定环境文件创建独立的 Docker 依赖；先为各工作树配置不同的数据库、Redis、MinIO 和沙箱发布端口。已有外部依赖使用普通 `start`，不会被管理工具接管。`stop` 保留数据，`uv run python -m scripts.dev destroy` 会永久删除该环境创建的容器、网络和数据卷，保留共享镜像及其他环境资源。

开发桌面端使用 `uv run python -m scripts.desktop_dev`。服务端和无关网页改动不会使桌面开发包失效；桌面或其框架依赖改动后自动重建。下载、安装与发布见[桌面说明](../../apps/desktop/README.md)。

## CI 与发布

PR 和主分支推送先按源码路径及 `uv.lock` 依赖关系选择检查：网页运行 Web 检查，服务端运行服务端检查，共享框架变化触发实际消费方。稳定的 `Quality gate` 汇总选中的任务；选中任务失败或取消都不能通过。

检查与发布独立。普通推送不发布客户端；`desktop-vX.Y.Z` 标签或 `Desktop release` 的手动操作才构建并发布桌面安装包。`Studio server image` 单独验证服务端镜像，不上传桌面应用。

## 验证 Studio Web

在 `apps/studio-web` 执行 `pnpm test:browser`，构建应用并运行浏览器测试。
直接运行指定测试文件时，先构建应用：

```bash
pnpm build
pnpm exec playwright test tests/browser/todo-trace.spec.ts --workers=1
```

## 构建 wheel

统一构建命令生成框架包和 Python 应用的 wheel：

```bash
uv run --no-project --python 3.11 python scripts/build_wheels.py --out-dir dist
```

输出目录不能包含已有 wheel。只构建部分项目时，传入相对于仓库根目录的路径：

```bash
uv run --no-project --python 3.11 python scripts/build_wheels.py \
  packages/tinkerfin-contracts packages/tinkerfin-native-stream \
  --out-dir dist/selected
```

命令将当前项目文件复制到临时目录，包含未提交的改动和未跟踪的源码文件，排除构建目录、
缓存及自动生成的包元数据。构建不会修改或删除工作树中的这些文件。
准备发布产物时应暂停编辑源码，保证各项目使用一致的输入。

每个 wheel 必须与临时副本中的源码逐字节一致，核对范围包括 Python 模块、类型声明、
类型标记和包资源。所有选定项目都通过核对后，命令才向输出目录写入 wheel。
缺少文件、多出文件或内容不同都会导致命令失败。uv 缓存已包含构建依赖时，可添加 `--offline`。

本地构建、打包测试和 Studio Dockerfile 都调用此入口。
生产 Dockerfile 只构建 Studio wheel，从 PyPI 安装框架包，并按锁文件安装第三方依赖。
CI 使用独立的 `candidate` 构建目标验证当前框架源码。

## 验证打包

```bash
mkdir -p .cache
uv export --locked --all-packages --no-dev --group packaging --no-emit-workspace \
  --no-header --output-file .cache/test-requirements.txt
uv run --no-sync python -m pip download --require-hashes --no-deps --only-binary=:all: \
  -r .cache/test-requirements.txt --dest .cache/test-wheels
uv run --locked --no-sync python -m pytest --noconftest tests/packaging -m packaging_e2e
```

测试覆盖构建残留、当前源码内容、wheel 元数据、许可证、依赖声明和隔离环境安装。
CI 统一在 Python 3.14 上验证核心安装场景、全部可选依赖组合和 Studio 部署所需的
候选源码 wheel 集合。


准备命令下载适合当前 Python 与平台的 wheel，并校验锁文件中的哈希。隔离安装测试
随后只使用 `.cache/test-wheels`，不访问包索引。锁文件、Python 版本或平台变化后需
重新执行准备命令；隔离构建所需的工具也包含在本地 wheel 仓库中。
