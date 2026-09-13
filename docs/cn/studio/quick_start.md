# 体验 Studio

[快速开始](../quick_start.md) · [English](../../en/studio/quick_start.md)

## 准备环境

- Docker、Docker Compose 2.24+ 和 Bash；Windows 使用 WSL
- Node.js 20.19+（20.x）或 22.12+，pnpm 10.8.0
- 可用的模型服务地址、模型名称与 API 密钥

后端、数据库、Redis 和 OpenSandbox 通过 Docker 启动；Web 客户端单独运行。

## 启动后端

从仓库源码构建并启动：

```bash
git clone https://github.com/tinkerfin-ai/tinkerfin-harness.git
cd tinkerfin-harness/apps/studio/server/deploy
./deploy.sh --build
```

首次执行生成配置和依赖凭据、构建后端镜像并等待服务就绪。首次准备隔离工作区需要下载运行镜像。
健康检查地址为 `http://127.0.0.1:8090/health/ready`。

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

打开终端显示的地址，默认 `http://localhost:5173`。开发服务器已将 `/api` 代理到本机 `8090` 端口。

## 登录并配置模型

全新数据库初始化时会创建以下账号：

| 项目 | 值 |
| --- | --- |
| 用户名 | `tinkerfin` |
| 密码 | `123456` |

已有数据卷不会重新执行初始化 SQL，也不会覆盖已有账号。当前没有公开注册入口。

登录后打开左下角用户菜单，进入模型设置，添加模型服务地址、模型名称和 API 密钥，启用模型并设为默认。
模型设置中的能力测试会实际调用所选服务，可能产生供应商费用；检查配置后按需测试。
先发送一句“你好”确认连接，再尝试上传附件或使用计划模式。发送图片需要选择支持图片输入的模型；生图服务需要单独配置。

附件支持图片、Markdown、PDF、DOCX 和 XLSX。Markdown 使用 UTF-8 编码，扩展名为 `.md` 或 `.markdown`；可上传给智能体按行读取，也可让智能体生成并交付 Markdown 文件。点击文件名可预览标题、表格和代码块，再下载原件。长文档预览显示前 10 万字符，下载内容完整保留。

DOCX 预览保留文字和表格，图片与原版式请下载查看。PDF 使用浏览器自带查看器；XLSX 显示首张表中已保存的值，最多 100 行、20 列，不重新计算公式。超出预览范围的文件仍可下载。

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
