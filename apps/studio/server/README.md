# TinkerFin Studio 后端

提供用户认证、模型配置、Agent 会话、自动化任务、附件、运行历史和 Sandbox 工作区。
首次使用见 [Studio 上手指南](../../../docs/cn/studio/quick_start.md)；后端启动后可查看 [Swagger 接口文档](http://127.0.0.1:8090/docs)。

## 快速部署

需要 Docker、Docker Compose 2.24 或更高版本，以及 Bash。Windows 使用 Docker Desktop
的 WSL 2 后端，并为所用发行版开启 WSL 集成。在 WSL 终端中克隆和执行以下命令；
仓库及凭据应保存在 WSL 的 Linux 文件系统中，以保证 `.env` 的文件权限生效。
这两个 Bash 脚本不能直接在 PowerShell 中执行。

在仓库根目录一键准备本机开发所需服务：

```bash
./apps/studio/server/deploy/start.sh --local
uv sync --package tinkerfin-studio --locked
uv run --package tinkerfin-studio python -m tinkerfin_studio
```

本机后端需要 Python 3.11+ 和 uv。`--local` 只启动依赖，首次从 `apps/studio/.env.example` 生成唯一配置 `apps/studio/.env` 和随机密码，不启动 Python 或 Web 客户端。默认使用桶 `tinkerfin`，后端启动时自动创建。重复执行复用配置、密码和数据。

Docker 项目为 `tinkerfin`，依赖容器为 `mysql8`、`redis-runtime`、`minio`、`opensandbox`。MySQL 8.4 同实例包含业务库 `tinkerfin` 和组件库 `tinkerfin_components`，分别使用只能访问本库的账号。用户、模型、会话和附件索引保存在业务库；长期记忆、轨迹、沙箱分配和自动化记录保存在组件库。

默认 API 为 `http://127.0.0.1:8090/api`，就绪检查为 `http://127.0.0.1:8090/health/ready`。后端允许任意浏览器来源，受保护接口仍要求 Bearer 令牌。Web 客户端单独启动，见 [Web 说明](../web/README.md)。

全新业务库预置账号 `tinkerfin`，密码 `123456`。已有数据卷不重新初始化或覆盖账号。
当前没有公开注册接口；对外开放前按[上手指南](../../../docs/cn/studio/quick_start.md#修改初始密码)修改初始密码。
自动化的日程与结果查看见[使用指南](../../../docs/cn/studio/automation.md)。

脚本可从任意目录通过完整路径执行，默认使用 `apps/studio/.env`。指定其他配置时，初始化使用 `init-env.sh /path/to/.env`，启动使用 `start.sh --env-file /path/to/.env`；本机后端通过 `STUDIO_ENV_FILE=/path/to/.env` 选择同一文件。固定容器名和端口只支持同一主机上的一套实例，脚本不会接管其他资源。

## 修改配置

需要自定义密码、端口或部署地址时，先生成配置，再编辑并启动：

```bash
./apps/studio/server/deploy/init-env.sh
# 编辑 apps/studio/.env
./apps/studio/server/deploy/start.sh --local
```

`init-env.sh` 只准备配置，不启动服务；`start.sh` 会自动调用它。`.env` 同时用于本机与容器部署，权限为 `0600`，不提交 Git。首次启动组件前可将生成的密码替换为自己的密码。已选内置组件缺少凭据时停止初始化，不自动生成替代密码。保留该文件，避免配置与持久化数据中的账号不一致。

在 `.env` 中修改应用连接参数：

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `COMPOSE_PROFILES` | `bundled` | 启动全部内置依赖；填写服务名选择部分依赖，留空使用全部外部依赖 |
| `S3_STORAGE_BUCKET` | `tinkerfin` | 应用存储桶，不存在时自动创建 |
| `S3_STORAGE_PUBLIC_ENDPOINT` | `http://127.0.0.1:9000` | 浏览器上传和下载的存储地址 |
| `STUDIO_IMAGE` | `ghcr.io/tinkerfin-ai/studio-server:0.1.0` | 后端镜像 |
| `STUDIO_BIND_ADDRESS` | `127.0.0.1` | 后端监听地址；允许远程访问时设为 `0.0.0.0` |
| `STUDIO_PORT` | `8090` | 后端对外端口 |
| `DEPLOY_WAIT_TIMEOUT` | `600` | 等待服务就绪的秒数 |
| `MYSQL_HOST` / `MYSQL_PORT` | 本机为 `127.0.0.1` / 发布端口；容器为 `mysql` / `3306` | 使用外部数据库时覆盖 |
| `MYSQL_BUSINESS_DATABASE` / `MYSQL_BUSINESS_USER` | `tinkerfin` / `tinkerfin` | 业务库名和账号 |
| `MYSQL_COMPONENTS_DATABASE` / `MYSQL_COMPONENTS_USER` | `tinkerfin_components` / `tinkerfin_components` | 组件库名和账号 |
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

本机默认使用依赖的发布端口，容器默认通过服务名和内部端口连接；无需维护两套连接地址。
数据库账号密码使用 `MYSQL_BUSINESS_PASSWORD` 和 `MYSQL_COMPONENTS_PASSWORD`，连接 URL 由应用构造。
需要独立数据库地址时可设置 `BUSINESS_DATABASE_URL`、`COMPONENTS_DATABASE_URL`。
配置值按字面填写，不在 `.env` 中引用其他变量。密码包含 `$` 等特殊字符时使用单引号；进程环境变量可覆盖本机配置，部署所用的组件凭据也会同步传给后端容器。

HTTP、本机或内网模型服务需要在 `.env` 的 `MODEL_ALLOWED_ORIGINS` 中列出精确的协议、主机和端口，修改后重启后端。例如本机 Ollama 使用 `MODEL_ALLOWED_ORIGINS=["http://localhost:11434","http://127.0.0.1:11434"]`；后端容器访问宿主机 Ollama 时使用 `http://host.docker.internal:11434`，并将该来源加入允许列表。该列表控制模型服务访问，与浏览器 CORS 无关。

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
或库名必须先由管理员调整，再同步 `.env`；更改配置不会修改现有数据库账号。

## 使用自己的镜像

生产容器部署在 `deploy/` 目录执行：

```bash
./init-env.sh
# 编辑 ../../.env，按需填写部署地址
./start.sh --container
```

`docker-compose.yaml` 包含基础 Compose 并增加后端容器。`--local` 与 `--container` 不能同时使用；两种模式均按 `COMPOSE_PROFILES` 启动依赖。沙箱对调用方公开的地址由 `OPEN_SANDBOX_PUBLIC_HOST` 指定，容器模式默认为 `host.docker.internal`，本机模式固定为 `127.0.0.1`。

在 `apps/studio/.env` 中设置 `STUDIO_IMAGE` 后执行 `./start.sh`。私有仓库需要先执行 `docker login`。

修改源码后，可直接构建并部署：

```bash
./start.sh --container
```

默认生成 `tinkerfin-studio-server:local`；已设置自定义镜像名时使用该名称。
构建在 Docker 内完成，不需要宿主机安装 Python 或 uv。脚本使用当前检出的源码，
构建后直接启动本地镜像。

## 只启动基础依赖

```bash
./start.sh --local
```

随后可在 PyCharm 或命令行启动 Studio。宿主机连接 MySQL 使用 `127.0.0.1:13306`，
Redis 使用 `127.0.0.1:6379`，OpenSandbox 使用 `127.0.0.1:8091`，MinIO 使用 `127.0.0.1:9000`。
本机和容器共用 `apps/studio/.env`，无需复制密码。

## 使用外部依赖

执行 `./init-env.sh`，在 `apps/studio/.env` 中选择需要启动的依赖；无需修改 Compose：

| `COMPOSE_PROFILES` | 启动的内置依赖 |
| --- | --- |
| `bundled` | 全部四个组件 |
| `mysql,redis-runtime,opensandbox` | 除 MinIO 外的三个组件 |
| 空值 | 不启动内置依赖 |

例如复用已有 MinIO，其他组件仍由项目启动：

```dotenv
COMPOSE_PROFILES=mysql,redis-runtime,opensandbox
S3_STORAGE_ENDPOINT=https://storage.example.com
S3_STORAGE_PUBLIC_ENDPOINT=https://files.example.com
S3_STORAGE_BUCKET=tinkerfin
S3_STORAGE_ACCESS_KEY=<已有访问账号>
S3_STORAGE_SECRET_KEY=<已有密钥>
```

全部使用已有服务时，填写各服务连接，并将 `COMPOSE_PROFILES` 留空：

```dotenv
COMPOSE_PROFILES=
MYSQL_HOST=db.example.com
MYSQL_PORT=3306
MYSQL_BUSINESS_DATABASE=tinkerfin
MYSQL_BUSINESS_USER=tinkerfin
MYSQL_BUSINESS_PASSWORD=<业务库密码>
MYSQL_COMPONENTS_DATABASE=tinkerfin_components
MYSQL_COMPONENTS_USER=tinkerfin_components
MYSQL_COMPONENTS_PASSWORD=<组件库密码>
REDIS_RUNTIME_HOST=redis.example.com
REDIS_RUNTIME_PORT=6379
REDIS_RUNTIME_PASSWORD=<Redis 密码>
OPEN_SANDBOX_DOMAIN=sandbox.example.com:8090
OPEN_SANDBOX_API_KEY=<沙箱密钥>
S3_STORAGE_ENDPOINT=https://storage.example.com
S3_STORAGE_PUBLIC_ENDPOINT=https://storage.example.com
S3_STORAGE_ACCESS_KEY=<存储账号>
S3_STORAGE_SECRET_KEY=<存储密钥>
```

本机可直接启动后端；容器部署执行：

```bash
./start.sh --container
```

未选择内置 MySQL 时不需要填写 `MYSQL_ROOT_PASSWORD`；后端使用业务与组件账号连接。
存储账号须能创建桶、管理生命周期及读写对象。桶内 `attachments/`
用于附件，应保持私有，不要配置匿名访问；其他前缀可供应用的其他文件用途使用。
外部 MinIO 还需允许 Studio 浏览器来源的跨域上传与下载；跨域放行不授予匿名对象访问权限。
外部 MySQL 需要以 `utf8mb4` 字符集和 `utf8mb4_0900_ai_ci` 排序规则创建两个数据库及各自账号，仅向业务库导入 `database/mysql/schema.sql`。组件库账号需有本库建表与读写权限，表由组件启动时创建。

使用现成后端镜像时执行 `./start.sh`。容器访问宿主机服务时可使用 `host.docker.internal`。
修改依赖选择不会删除已经运行的组件或数据卷。

## 日志与数据

以下命令在 `deploy/` 目录执行：

```bash
docker compose --env-file ../../.env ps
docker compose --env-file ../../.env logs -f server
docker compose --env-file ../../.env down
```

配置或镜像变化时，Compose 会重新创建受影响的容器并短暂中断服务；配置未变时复用容器。数据卷会保留。`docker compose down` 也会保留
数据；`docker compose down -v` 会永久删除本项目的数据库、Redis、OpenSandbox 和 MinIO 数据卷。
删除前应完成备份。

默认输出控制台日志，`server` 服务的 Docker 日志按 50 MiB 滚动，最多保留 3 个文件。
需要独立文件日志时，在 `.env` 中启用 `LOG_FILE_ENABLED=true`。容器的根文件系统只读，
需将 `LOG_FILE_PATH` 设置为容器内绝对路径，自行给对应目录挂载可写卷，并允许用户
`10001:10001` 写入。启动脚本不创建日志卷，也不会自动关闭已启用的文件日志。

自动化任务和运行记录保存在 MySQL，运行检查点保存在 Redis。备份恢复时须保持数据库、
检查点和附件数据一致。任务参考文件及运行附件保留持久引用，删除任务仍保留历史文件。

附件保存在所配置桶的 `attachments/` 下，内置 MinIO 使用 `minio-data` 卷。Sandbox 工作区跨会话
保留，不会因闲置自动删除；OpenSandbox 需要访问宿主机 Docker，请只在受信任的主机部署。

## 本地开发

在仓库根目录执行：

```bash
uv sync --all-packages --group dev --locked
./apps/studio/server/deploy/start.sh --local
uv run python -m tinkerfin_studio --host 127.0.0.1 --port 8090 --reload
```

使用 IDE 启动服务时，将运行工作目录设为仓库根目录，使 `--reload` 同时覆盖 Studio 与
`packages/` 的 Python 源码。

本地配置来自 `apps/studio/.env`，唯一模板为 `apps/studio/.env.example`。模板中的文件日志默认关闭，
启用后写入 `apps/studio/server/logs/studio.log`。相对路径以配置文件所在目录为基准。

```bash
uv run pytest -q apps/studio/server/tests
uv run ruff check --no-cache apps/studio/server
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
