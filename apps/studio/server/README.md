# TinkerFin Studio 后端

提供用户认证、模型配置、Agent 会话、自动化任务、附件、运行历史和 Sandbox 工作区。
首次使用见 [Studio 上手指南](../../../docs/cn/studio/quick_start.md)；HTTP 接口见 [API 参考](docs/api.md)。

## 快速部署

需要 Docker、Docker Compose 2.24 或更高版本，以及 Bash。Windows 请在 WSL 中执行。

```bash
git clone https://github.com/tinkerfin-ai/tinkerfin-harness.git
cd tinkerfin-harness/apps/studio/server/deploy
./setup.sh
# 在 .env 中填写 S3_STORAGE_BUCKET
./deploy.sh
```

`setup.sh` 创建 `.env` 和随机凭据；桶名必填且无默认值，填写后由 `deploy.sh` 拉取镜像并等待服务就绪。默认 API 地址为
`http://127.0.0.1:8090/api`，健康检查地址为 `http://127.0.0.1:8090/health/ready`。
就绪检查包含对象存储和自动化工作器健康状态。自动化的日程、权限和结果查看见
[使用指南](../../../docs/cn/studio/automation.md)。
默认不预热沙箱；首次使用工作区时创建沙箱，缺少运行镜像时还需下载，耗时取决于网络。

Docker 项目名为 `tinkerfin-studio`，包含 `server`、`mysql`、`redis-runtime` 和
`opensandbox`、`minio`。只部署后端，不包含 Web 页面。
全新数据库初始化时预置账号 `tinkerfin`，密码 `123456`。已有数据卷不重新初始化或覆盖账号。
当前没有公开注册接口；对外开放前按[上手指南](../../../docs/cn/studio/quick_start.md#修改初始密码)修改初始密码。

脚本可以从任意目录通过完整路径执行；配置默认读取脚本所在目录的 `.env`：

```bash
/path/to/tinkerfin/apps/studio/server/deploy/deploy.sh
```
使用其他配置文件时执行 `./deploy.sh --env-file /path/to/.env`，凭据目录为该文件旁的 `secrets/`。

## 修改配置

需要先改配置时，执行：

```bash
./setup.sh
# 编辑生成的 .env
./deploy.sh
```

已有 `.env` 和密码会保留。不要删除 `secrets/` 后重新生成密码；目录缺失或文件不完整时，
应从备份恢复。目录权限为 `0700`，请勿放宽。

在 `.env` 中修改应用连接参数：

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `S3_STORAGE_BUCKET` | 必填，无默认值 | 应用存储桶，不存在时自动创建 |
| `S3_STORAGE_PUBLIC_ENDPOINT` | `http://127.0.0.1:9000` | 浏览器上传和下载的存储地址 |
| `STUDIO_IMAGE` | `ghcr.io/tinkerfin-ai/studio-server:0.1.0` | 后端镜像 |
| `STUDIO_BIND_ADDRESS` | `127.0.0.1` | 后端监听地址；允许远程访问时设为 `0.0.0.0` |
| `STUDIO_PORT` | `8090` | 后端对外端口 |
| `DEPLOY_WAIT_TIMEOUT` | `600` | 等待服务就绪的秒数 |
| `MYSQL_HOST` / `MYSQL_PORT` | `mysql` / `3306` | 后端连接的数据库地址和端口 |
| `MYSQL_DATABASE` / `MYSQL_USER` | `tinkerfin` / `studio` | 数据库名和账号 |
| `MYSQL_PUBLISHED_PORT` | `13306` | 从本机连接内置 MySQL 的端口 |
| `REDIS_RUNTIME_PUBLISHED_PORT` | `6379` | 本机访问 Redis 的端口 |
| `OPEN_SANDBOX_PUBLISHED_PORT` | `8091` | 本机访问 OpenSandbox 的端口 |
| `OPEN_SANDBOX_CPU` | `1` | 每个新建执行沙箱的 CPU 核数上限，可填 `0.5` 等正数 |
| `OPEN_SANDBOX_MEMORY_MIB` | `1024` | 每个新建执行沙箱的内存上限，单位为 MiB，必须为正整数 |
| `OPEN_SANDBOX_WARM_POOL_SIZE` | `0` | 全局预热沙箱数量；设为 `1` 可提前准备一个工作区 |
| `LOG_LEVEL` | `INFO` | 后端日志等级 |

中间件端口默认仅绑定宿主机的 `127.0.0.1`。远程使用附件时，将
`S3_STORAGE_PUBLIC_ENDPOINT` 设置为浏览器可达的地址，并通过反向代理公开 MinIO API，
或设置 `S3_STORAGE_BIND_ADDRESS=0.0.0.0` 开放配置的端口。HTTPS 页面应使用 HTTPS 存储地址。
浏览器上传与下载直接访问该地址，容器内部连接使用 `S3_STORAGE_ENDPOINT`。

修改 `MYSQL_PUBLISHED_PORT` 不改变容器内部的
数据库连接。MySQL 密码保存在 `secrets/mysql_password`，Redis 与 OpenSandbox 密钥分别
保存在同名 Secret 文件中；`secrets/database_url` 由脚本根据 MySQL 配置自动生成，不要手动编辑。

执行沙箱与控制服务的资源限制相互独立；控制服务的限制不包含它创建的沙箱。
这些值是上限，不是启动时预留的占用，也不代表整套部署的最低主机规格。
默认额度适合低并发试用；较大的数据分析、文档处理任务可调高 `OPEN_SANDBOX_MEMORY_MIB`，
内置控制服务在 `docker-compose-base.yaml` 中限制为 0.5 CPU、512 MiB，需要时修改该文件。
执行沙箱额度使用应用默认值，只有需要覆盖时才在 `.env` 中添加对应配置。
超过内存上限可能导致对应容器被终止。

沙箱 CPU 和内存设置只影响新建沙箱，已有工作区继续使用创建时的额度；重新部署后端不会
调整已有沙箱。预热数量为 `0` 时仍保留已有工作区，只在需要新的工作区时创建。
多个后端实例共用同一沙箱命名空间时，必须配置相同的预热数量。

内置 MySQL 只在新数据卷首次启动时创建账号、数据库并导入业务表。已有数据库的账号、密码
或库名必须先由管理员调整，再同步 `.env` 和密码文件；更改配置不会修改现有数据库账号。

## 使用自己的镜像

修改 `.env` 中的 `STUDIO_IMAGE` 后执行 `./deploy.sh`。私有仓库需要先执行 `docker login`。

修改源码后，可直接构建并部署：

```bash
./deploy.sh --build
```

默认生成 `tinkerfin-studio-server:local`；已设置自定义镜像名时使用该名称。
构建在 Docker 内完成，不需要宿主机安装 Python 或 uv。脚本使用当前检出的源码，
构建后直接启动本地镜像。

## 只启动基础依赖

```bash
./setup.sh
docker compose -f docker-compose-base.yaml up -d --wait
```

随后可在 PyCharm 或命令行启动 Studio。宿主机连接 MySQL 使用 `127.0.0.1:13306`，
Redis 使用 `127.0.0.1:6379`，OpenSandbox 使用 `127.0.0.1:8091`，MinIO 使用 `127.0.0.1:9000`。
本地后端配置中的密码应与 `deploy/secrets/` 中对应文件一致。

## 使用外部依赖

执行 `./setup.sh`，编辑 `.env` 中的 MySQL、Redis、OpenSandbox 和 MinIO 地址，并把已有服务的
密码填入 `secrets/` 对应文件，然后执行：

```bash
./deploy.sh --external
```

此模式只启动 `server`。S3 凭据分别填写 `secrets/s3_storage_access_key` 和
`secrets/s3_storage_secret_key`；配置 `S3_STORAGE_ENDPOINT`、`S3_STORAGE_PUBLIC_ENDPOINT`
和 `S3_STORAGE_BUCKET`，账号须能创建桶、管理生命周期及读写对象。桶内 `attachments/`
用于附件，应保持私有，不要配置匿名访问；其他前缀可供应用的其他文件用途使用。
外部 MySQL 需要事先创建数据库，并在新库中导入 `database/mysql/schema.sql`。

只替换 MySQL 时，在 `.env` 中设置外部 MySQL 参数和：

```dotenv
COMPOSE_PROFILES=redis-runtime,opensandbox,minio
```

然后执行普通的 `./deploy.sh`。容器访问宿主机服务时可使用 `host.docker.internal`。

## 日志与数据

以下命令在 `deploy/` 目录执行：

```bash
docker compose ps
docker compose logs -f server
docker compose down
```

重新部署会重新创建服务容器并短暂中断服务，数据卷会保留。`docker compose down` 也会保留
数据；`docker compose down -v` 会永久删除本项目的数据库、Redis、OpenSandbox 和 MinIO 数据卷。
删除前应完成备份。

`server` 服务的 Docker 日志按 50 MiB 滚动，最多保留 3 个文件。需要独立文件日志时，在 `.env` 中启用
`LOG_FILE_ENABLED=true`，设置绝对路径 `LOG_FILE_PATH` 并给对应目录挂载可写卷。

自动化任务和运行记录保存在 MySQL，运行检查点保存在 Redis。备份恢复时须保持数据库、
检查点和附件数据一致。任务参考文件及运行附件保留持久引用，删除任务仍保留历史文件。

附件保存在所配置桶的 `attachments/` 下，内置 MinIO 使用 `minio-data` 卷。Sandbox 工作区跨会话
保留，不会因闲置自动删除；OpenSandbox 需要访问宿主机 Docker，请只在受信任的主机部署。

## 本地开发

在仓库根目录执行：

```bash
uv sync --all-packages --group dev --locked
cp apps/studio/server/.env.example apps/studio/server/.env
# 编辑 server/.env，填写可从宿主机访问的依赖地址和密码
uv run python -m tinkerfin_studio --host 127.0.0.1 --port 8090 --reload
```

使用 IDE 启动服务时，将运行工作目录设为仓库根目录，使 `--reload` 同时覆盖 Studio 与
`packages/` 的 Python 源码。

本地配置来自 `server/.env`。填写 `S3_STORAGE_BUCKET`；示例配置通过文件读取 `deploy/secrets/` 的 S3 凭据，相对路径以 `.env` 所在目录为准。可选文件日志默认
位于 `server/logs/studio.log`。相对路径以配置文件所在目录为基准。

```bash
uv run pytest -q apps/studio/server/tests
uv run ruff check apps/studio/server
uv run pyright apps/studio/server/src apps/studio/server/tests
```

## 发布后端镜像

更新 `src/tinkerfin_studio/version.py` 中的版本、部署示例和 Compose 默认镜像版本后，
推送对应的 `studio-v<版本>` Git tag。GitHub Actions 会构建、检查并发布 `amd64` 和 `arm64`
镜像到 `ghcr.io/tinkerfin-ai/studio-server`。首次发布需在 GitHub Packages 中把该镜像设为公开，
使用者才能匿名拉取。若首次公开拉取检查失败，调整可见性后只需重跑失败的
`Verify public Studio image` 任务。

## 许可证

[Apache License 2.0](LICENSE)。
