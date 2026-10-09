# 安装与可选组件

[文档首页](index.md) · [English](../en/installation.md)


要求 Python 3.11 或更高版本。

| 需要的能力 | 安装命令 |
| --- | --- |
| 原生 Runtime、Plan Mode 与 Observation | `pip install tinkerfin` |
| 示例使用的 OpenAI 模型适配包 | `pip install langchain-openai` |
| AG-UI Runtime | `pip install "tinkerfin[agui]"` |
| 只使用 AG-UI 转换器 | `pip install tinkerfin-agui-adapter` |
| 共享运行与观察契约 | `pip install tinkerfin-contracts` |
| 共享 Native 流契约 | `pip install tinkerfin-native-stream` |
| 进程内语义 Trace | `pip install tinkerfin-tracing` |
| SQL Trace 持久化 | `pip install "tinkerfin-tracing[sqlalchemy]"`，另装所选异步数据库驱动 |
| SQLAlchemy LangGraph 长期记忆 | `pip install "tinkerfin-langgraph-store[sqlalchemy]"` 并安装异步数据库驱动 |
| 进程内资源变化通知 | `pip install tinkerfin-notifications` |
| 跨进程资源变化通知 | `pip install "tinkerfin-notifications[redis]"` |
| 运行命令与持久输出 | `pip install tinkerfin-gateway` |
| Gateway HTTP 响应 | `pip install "tinkerfin-gateway[starlette]"` |
| 与协议无关的内存消息流 | `pip install tinkerfin-messaging` |
| AG-UI Messaging codec | `pip install "tinkerfin-messaging[agui]"` |
| Native Messaging codec | `pip install "tinkerfin-messaging[native]"` |
| SQL 消息存储 | `pip install "tinkerfin-messaging[sqlalchemy]"`，另装异步数据库驱动 |
| Redis AG-UI 消息存储 | `pip install "tinkerfin-messaging[agui,redis]"` |
| OpenSandbox | `pip install tinkerfin-sandbox` |
| SQL 持久化 Sandbox 状态 | `pip install "tinkerfin-sandbox[sqlalchemy]"`，另装异步数据库驱动 |
| 默认内存模式的立即执行与任务调度 | `pip install tinkerfin-automation` |
| SQL 任务持久化 | `pip install "tinkerfin-automation[sqlalchemy]"`，另选异步驱动 |

不必一次安装全部组件。先安装当前要用的部分，后面需要时再增加。
