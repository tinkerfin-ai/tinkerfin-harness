# 体验 Studio

[快速开始](../quick_start.md) · [English](../../en/studio/quick_start.md)

## 准备环境

- Docker、Docker Compose 2.24+ 和 Bash；Windows 使用 WSL
- Node.js 20.19+（20.x）或 22.12+，pnpm 10.8.0
- 可用的模型服务地址、模型名称与 API 密钥

后端、数据库、Redis、OpenSandbox 和 MinIO 通过 Docker 启动；Web 客户端单独运行。

## 启动后端

从仓库源码构建并启动：

```bash
git clone https://github.com/tinkerfin-ai/tinkerfin-harness.git
cd tinkerfin-harness/apps/studio/server/deploy
./setup.sh
# 在 .env 中填写 S3_STORAGE_BUCKET，桶名没有默认值
./deploy.sh --build
```

首次执行生成配置和依赖凭据、构建后端镜像并等待服务就绪。默认按需创建工作区，不预热沙箱；每个新沙箱上限为 1 CPU、1 GiB 内存。首次使用时，缺少运行镜像还需下载。
启动后可查看[健康状态](http://127.0.0.1:8090/health/ready)和 [Swagger 接口文档](http://127.0.0.1:8090/docs)。

已有可用的发布镜像时也可使用 `./deploy.sh`。外部数据库、配置参数和日志命令见[服务端部署说明](../../../apps/studio/server/README.md)。

## 启动 Web

另开一个终端，从仓库根目录执行：

```bash
cd apps/studio/web
corepack enable
corepack prepare pnpm@10.8.0 --activate
pnpm install --frozen-lockfile
pnpm dev
```

打开终端显示的地址，默认 `http://localhost:5190`。

## 登录并配置模型

全新数据库初始化时会创建以下账号：

| 项目 | 值 |
| --- | --- |
| 用户名 | `tinkerfin` |
| 密码 | `123456` |

已有数据卷不会重新执行初始化 SQL，也不会覆盖已有账号。当前没有公开注册入口。

登录后打开左下角用户菜单，进入“模型配置”，点击“添加提供方”。选择服务商或自定义服务，填写地址和认证信息并保存。

选择连接，点击“获取模型”添加所需模型，或手动填写服务商提供的 Model ID。启用一个对话模型并设为默认，发送一句“你好”确认连接。

使用 Ollama 时，先启动服务并安装模型，连接选择 Ollama 原生 API，无需占位密钥。容器部署需填写容器可访问的地址。本机、内网及 HTTP 地址须由管理员加入 `MODEL_ALLOWED_ORIGINS`，见[服务端说明](../../../apps/studio/server/README.md)。

## 修改初始密码

对外开放前更换初始密码。当前由管理员更新账号；在后端 `deploy` 目录生成新密码哈希：

```bash
docker compose exec server python -c 'import asyncio, getpass; from tinkerfin_studio.auth.passwords import hash_password; print(asyncio.run(hash_password(getpass.getpass("New password: "))))'
```

在数据库客户端连接 Studio 数据库，将输出的完整哈希用于：

```sql
UPDATE users SET password_hash = '<生成的完整哈希>' WHERE username = 'tinkerfin';
```

对话默认使用完全访问，可通过模型旁的权限选择器改为写入需审批。
定时任务的设置和结果查看见 [Studio 自动化](automation.md)。

## 遇到问题

| 现象 | 检查 |
| --- | --- |
| 页面无法连接服务 | 后端健康检查是否成功、Web 代理地址是否正确 |
| 登录失败 | 当前数据卷中是否存在该账号、密码是否已调整 |
| 无法发送消息 | 是否已添加并启用默认模型，附件是否上传完成 |
| 会话异常 | 查看后端日志和模型配置，并确认 OpenSandbox 可用 |

[返回文档首页](../index.md) · [Web 开发说明](../../../apps/studio/web/README.md)
