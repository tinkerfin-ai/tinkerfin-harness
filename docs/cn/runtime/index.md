# Runtime

[文档首页](../index.md) · [English](../../en/runtime/index.md)

`TinkerFin` 用于配置智能体，`with_namespace(...).build(...)` 返回执行该配置的
`AgentRuntime`。构建过程不会打开模型、数据库、Sandbox 或 Graph。

## 安装

```bash
pip install tinkerfin
```

如需输出 AG-UI 事件，安装 `tinkerfin[agui]`。模型由应用安装对应的 LangChain
提供方包。

## 构建与执行

```python
from contextlib import aclosing

from tinkerfin import TinkerFin

runtime = (
    TinkerFin()
    .with_namespace("company-a")
    .build(model="openai:gpt-5.4", tools=[])
)

stream = runtime.open_run(
    thread_id="conversation-1",
    run_id="request-1",
    input={"messages": [{"role": "user", "content": "概括这项请求"}]},
)

async with aclosing(stream):
    async for part in stream:
        await handle(part)
```

按所需结果选择执行入口：

| 目标 | API |
| --- | --- |
| 获取最终状态 | `await runtime.ainvoke(...)` |
| 消费原生对象 | `runtime.open_run(...)` |
| 消费 AG-UI 事件 | `runtime.open_agui_run(...)` |
| 构建由调用方管理的 Graph | `await tinkerfin.deep_agent.create_graph(runtime)` |

两个流入口都是同步、惰性的工厂。只有预检或消费流时才开始准备资源。消费者提前退出时必须关闭流。

## 身份与隔离

应用自行决定 Runtime 的 namespace。它可以表示租户、用户、项目或其他业务范围，框架只把它视为不透明文本。

| 值 | 含义 |
| --- | --- |
| `runtime.namespace` | 构建时固定的业务数据和资源范围 |
| `thread_id` | namespace 内持续使用的一次对话 |
| `run_id` | thread 内的一次语义执行 |
| `graph_namespace` | 事件在执行 Graph 中的位置 |

其他 TinkerFin 包需要完整身份时，使用 `runtime.run_identity(thread_id, run_id)`。
`graph_namespace` 表示执行位置，不能代替业务 namespace。

## 持久化与资源

传入 checkpointer 可保留对话状态，并恢复工具或 Plan 审批。传入 Store 可保存长期记忆。Runtime 会按自己的 namespace 隔离两者。

```python
runtime = (
    TinkerFin(checkpointer=checkpointer, store=store)
    .with_namespace(namespace)
    .build(model=model, tools=tools)
)
```

模型、Store、checkpointer、协调器和传入的 backend 仍由应用管理。每次运行负责关闭自己创建的 Graph、惰性 workspace、流和清理资源。

## 后续阅读

- [运行第一个智能体](quick_start.md)
- [配置智能体与 Plan](agent-configuration.md)
- [流与 SSE](streams-and-sse.md)
- [Runtime 扩展](extensions.md)
- [Runtime API](api-reference.md)
