<p align="right">
  <a href="README.en.md">English</a>
</p>
<p align="center">
  <img src="apps/studio-web/public/brand/tinkerfin.svg" alt="TinkerFin" width="88" />
</p>
<h1 align="center">TinkerFin</h1>
<p align="center">面向企业级智能体应用与业务工作流的 Python Agent Harness</p>
<p align="center">
  <a href="docs/cn/runtime/quick_start.md"><img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" /></a>
  <a href="https://www.typescriptlang.org/"><img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-0891B2?style=flat-square&amp;logo=typescript&amp;logoColor=white" /></a>
  <a href="https://docs.langchain.com/oss/python/deepagents/overview"><img alt="Deep Agents" src="https://img.shields.io/badge/Deep_Agents-7C3AED?style=flat-square" /></a>
  <a href="https://docs.langchain.com/oss/python/langchain/overview"><img alt="LangChain" src="https://img.shields.io/badge/LangChain-D97706?style=flat-square&amp;logo=langchain&amp;logoColor=white" /></a>
  <a href="https://fastapi.tiangolo.com/"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&amp;logo=fastapi&amp;logoColor=white" /></a>
  <a href="https://github.com/opensandbox-group/OpenSandbox"><img alt="OpenSandbox" src="https://img.shields.io/badge/OpenSandbox-E11D48?style=flat-square" /></a>
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/License-Apache--2.0-52617A?style=flat-square" /></a>
  <a href="docs/cn/index.md"><img alt="Docs: English / 中文" src="https://img.shields.io/badge/Docs-English%20%2F%20中文-2563EB?style=flat-square" /></a>
</p>

## 项目说明

**TinkerFin 是面向企业级应用的 Python Agent Harness。** 它连接模型、工具和工作区，
提供多模态输入、任务编排、持久状态、人工审批与执行追踪。

既可独立运行智能体，也可与业务工作流组合，用于调研分析、文档处理、数据分析和文件生成。

## 个人空间、项目与多端同步

Studio 为每个账号提供独立管理的项目、模型与服务连接、个人技能库。

- **按项目组织工作**：不同项目分别保存会话、工作区文件、长期记忆与自动化任务。同一项目中的会话可共用文件和记忆，个人技能可跨项目复用，也可安装项目专属技能。
- **在多个客户端继续工作**：同一账号登录不同浏览器或设备，可查看最新会话和运行状态；项目资源与文件变化会通知其他客户端刷新，重新连接后恢复最新状态。
- **使用 Mac 上的目录与工具**：在本机客户端点击“连接”并完成浏览器授权，选择项目后切换本地执行。客户端自动准备工作区，审批和结果在会话中查看，见[本机协作](docs/cn/studio/local_devices.md)。
- **草稿与文件预览**：未发送草稿保存在当前页面。已打开的工作区文本发生变化时，页面提示刷新预览，避免覆盖正在阅读的内容。

![个人项目中的会话、文件交付与多端同步](docs/assets/screenshots/studio-cn.gif)

## 安装

安装 Python 框架和要使用的模型集成：

```bash
pip install tinkerfin langchain-openai
```

Studio 使用 Docker Compose 启动依赖，本机后端使用 Python 和 uv，[Studio 上手指南](docs/cn/studio/quick_start.md)
说明所需服务和配置。

## 快速开始

### 体验 Studio

执行 `./apps/studio-server/deploy/start.sh --local` 准备依赖和本机配置，再启动后端和 Web 客户端；也支持容器部署。初始账号为 `tinkerfin`，密码为 `123456`；登录后配置自己的模型。

[打开 Studio 上手指南 →](docs/cn/studio/quick_start.md)

### 接入 Python 框架

需要 Python 3.11 或更高版本。配置模型密钥后，通过 `TinkerFin().with_namespace(...).build(...)` 构建
`AgentRuntime` 并接收执行结果。

[运行第一个智能体 →](docs/cn/runtime/quick_start.md)

## 核心能力

- **按需组合 Harness 能力** — 配置模型、工具、技能和子智能体，按需组合运行管理、消息、通知、追踪与沙箱能力。
- **搭配业务工作流使用** — 在应用工作流中调用智能体，或将编译后的 LangGraph 工作流注册为子智能体，组合固定步骤与模型驱动的工具调用。
- **多模态输入与文件交付** — 传递图片、音频、视频和文档附件，结合支持相应格式的模型与工具完成处理，并交付生成的文件。
- **计划审阅与工具审批** — 先澄清需求、审阅计划，再进入执行；关键工具操作可单独审批。
- **实时事件与断线续传** — 通过持久消息通道推送与回放 AG-UI 事件，业务事件可共用同一通道。
- **统一命令与客户端刷新** — 通过 [Gateway](docs/cn/gateway/index.md) 受理运行命令和订阅持久输出，通过 [Notifications](docs/cn/notifications/index.md) 通知各客户端读取资源变化。
- **追踪每次调用** — 查看模型、工具和子智能体的调用关系、输入输出与耗时，实时跟踪或查询历史。
- **管理隔离工作区** — 读写文件、执行命令，支持环境复用、预热、暂停和恢复。
- **持久状态与运行协调** — 保存会话状态并在审批后继续执行，协调多进程下的运行归属、重复请求与取消。
- **多租户集成** — 按租户、用户或项目划分会话、存储与沙箱范围；认证和访问授权由应用负责。
- **后台任务调度** — 立即执行任务，或设置一次性、固定速率和 Cron 调度；通过 [Automation](docs/cn/automation/index.md) 查询结果、取消执行或发起重试。

## Studio：支持多模态的智能体工作台

用文字、图片和文档发起任务，在对话中完成分析、生图与文件交付。看图和生图需配置相应模型。

### 将业务目标转为执行计划

业务目标先转成可审阅的计划，确认范围和步骤后再进入执行。

![业务需求澄清与计划审阅](docs/assets/screenshots/plan-cn.gif)

### 掌握任务执行过程

从业务对话一路追踪到模型、工具和子智能体，查看任务如何执行、时间花在哪里。

![门店数据核算的模型与子智能体调用链路](docs/assets/screenshots/trace-cn.gif)

### 安排任务并查看结果

创建一次性或周期任务，查看执行进度和结果；任务完成后可打开关联会话继续处理。

![自动化任务与结果会话](docs/assets/screenshots/automation-cn.gif)

*在 Studio 演示项目中使用示例数据实际录制。*

## 项目组成

| 部分 | 用途 |
| --- | --- |
| `packages/` | 企业级 Agent Harness 框架包，提供执行、持久化、消息、人工审阅、追踪与工作区能力 |
| `apps/studio-server/` | Studio API、会话执行与业务存储 |
| `apps/studio-web/` | Studio 网页工作台 |
| `apps/desktop/` | 可直接安装的 macOS 本机工具客户端 |
| `docs/` | 覆盖应用搭建、部署运维与深度集成的双语文档 |

## 文档

[中文文档首页 →](docs/cn/index.md)

从应用搭建到部署运维，提供上手指南、架构说明与集成参考。

## 参与贡献

欢迎提交问题、改进文档或贡献代码。开发环境和验证命令见[开发指南](docs/cn/development.md)。

- [参与开发](docs/CONTRIBUTING.cn.md)
- [安全漏洞报告](SECURITY.md)

## 许可证

仓库默认采用 [Apache License 2.0](LICENSE)。
`tinkerfin-langgraph-store` 采用包内 [MIT License](packages/tinkerfin-langgraph-store/LICENSE)
与 [NOTICE](packages/tinkerfin-langgraph-store/NOTICE)。
