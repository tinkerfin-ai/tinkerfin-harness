# 仓库开发

[English](../en/development.md)

在仓库根目录执行以下命令。需要 Python 3.11 或更高版本以及 uv。

提交 PR 和运行常规 Python、Studio Web 检查的步骤见[参与开发](../CONTRIBUTING.cn.md)。

## 安装工作区

```bash
uv sync --locked --all-packages --group dev
```

开发依赖按任务分组：`test` 提供测试运行器与契约 Fixture，`lint` 提供 Ruff 和 Pyright，
`integration` 提供 Docker 与数据库客户端，`packaging` 提供 wheel 验证工具。
`dev` 聚合四组；这些开发依赖不会作为包依赖发布。

使用 `--no-default-groups --group NAME` 可选择安装的组。当前包与 Studio 测试需要
`test`、`lint` 和 `integration`：类型契约测试会调用 Pyright，共享 Fixture 的收集会导入
集成客户端。安装这些客户端不会启动 Docker 服务。安装 `packaging` 组及全部工作区包后，
打包套件可通过 `--noconftest` 独立运行。选择性安装后使用 `uv run --no-sync`，保持已选择的环境。

### 本地按需使用清华源

默认安装使用官方 PyPI。本地需要镜像时，在仓库根目录执行以下命令；首次安装先运行 `uv venv` 创建 `.venv`：

```bash
uv export --locked --all-packages --group dev --output-file /tmp/tinkerfin-dev-requirements.txt > /dev/null
uv pip sync --default-index https://pypi.tuna.tsinghua.edu.cn/simple /tmp/tinkerfin-dev-requirements.txt
```

该命令按锁定版本安装并校验已记录的包哈希，不修改 `uv.lock` 或全局源配置。

## 验证 Studio Web

在 `apps/studio/web` 执行 `pnpm test:browser`，构建应用并运行浏览器测试。
直接运行指定测试文件时，先构建应用：

```bash
pnpm build
pnpm exec playwright test tests/browser/todo-trace.spec.ts --workers=1
```

## 构建 wheel

统一构建命令生成十个框架包和 Studio 服务端的 wheel：

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
Dockerfile 在容器内导出锁定的生产依赖并构建 wheel。

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
CI 在 Python 3.11–3.14 上运行核心安装场景，另在 Python 3.11 上运行全部可选依赖组合和
Studio 部署所需的完整 wheel 集合。


准备命令下载适合当前 Python 与平台的 wheel，并校验锁文件中的哈希。隔离安装测试
随后只使用 `.cache/test-wheels`，不访问包索引。锁文件、Python 版本或平台变化后需
重新执行准备命令；隔离构建所需的工具也包含在本地 wheel 仓库中。

## 验证 Docker 集成

启动 Docker 后执行以下测试；测试会创建并清理专用的临时服务：

```bash
uv run pytest -m docker_integration
```

主质量工作流在 push 和 PR 时执行。完整 Docker 套件由独立工作流每日运行，也可在
`Docker integrations` 工作流中通过 **Run workflow** 手动启动；其结果不阻塞主质量门禁。
