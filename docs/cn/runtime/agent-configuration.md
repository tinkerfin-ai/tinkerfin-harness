# 配置智能体

[Runtime](index.md) · [English](../../en/runtime/agent-configuration.md)

通过 `build()` 配置模型、工具、文件访问、任务委派和审批，取得可复用的
`AgentRuntime`，在应用中运行智能体。

```python
from tinkerfin import TinkerFin

runtime = (
    TinkerFin(checkpointer=checkpointer, store=store)
    .with_namespace("support")
    .build(
        name="support-agent",
        model="openai:gpt-5.4",
        tools=[search_orders],
        system_prompt="回答客户订单相关问题。",
    )
)
```

## 构建选项

| 能力 | 参数 |
| --- | --- |
| 模型与指令 | `model`、`system_prompt` |
| 工具与 middleware | `tools`、`middleware` |
| 子智能体 | `subagents` |
| 技能与记忆文件 | `skills`、`memory` |
| 文件系统 | `backend`、`permissions` |
| 工具审批 | `interrupt_on` |
| 结构化输出 | `response_format` |
| 状态与调用上下文 | `state_schema`、`context_schema` |

`model` 必填；checkpointer 和 Store 在 `TinkerFin(...)` 中配置。
传入的资源仍由应用管理。`build()` 只校验和保存配置，不执行 I/O。

使用 Store 的文件、记忆、技能和摘要 middleware 时，不要为 `StoreBackend` 单独传入
`store`，统一通过 `TinkerFin(store=...)` 提供。Runtime 为这些内置声明应用 namespace
隔离和异步文件访问，不修改调用方提供的配置对象。

使用 workspace 时，文件系统 middleware 和内置文件工具由 Runtime 提供，不可替换。
使用普通 backend 时，仅未配置有效 `permissions` 的角色允许自定义文件系统 middleware
或替换文件工具；冲突配置会在 `build()` 时抛出 `ValueError`。
任务委派通过 `subagents` 配置，工具审批通过 `interrupt_on` 配置，不可替换对应
middleware；`task` 和已配置远程智能体使用的委派工具名称也不可覆盖。

## 持久化与审批

可恢复的工具审批和 Plan 必须使用具体 checkpointer。checkpointer 已保存 thread 历史时，只提交新的用户消息。

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .build(
        model=model,
        tools=[send_report],
        interrupt_on={"send_report": True},
    )
)
```

审批流程使用 `durability="sync"`，表示等待异步 checkpoint 写入完成，不表示使用同步数据库驱动。

## 执行前审阅 Plan

在 builder 上启用 Plan，并在请求中选择 `mode="plan"`：

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .with_plan(planner_model=model)
    .build(model=model, tools=tools)
)

result = await runtime.ainvoke(
    thread_id=thread_id,
    run_id=run_id,
    input=graph_input,
    mode="plan",
)
```

Plan 根据对话选择普通回复、澄清需求或生成计划草稿，使用已配置的工具、工作区、技能和权限规则。
backend 支持命令执行时，Plan 可以运行分析脚本；需要审批的工具仍须先获批准。
工具批准只授权本次操作，之后继续规划；只有批准当前草稿才开始执行该计划。
普通回复完成后仍保留 Plan 模式。
`mode="default"` 直接执行智能体。Plan 数据模型和审阅动作由 `tinkerfin.plan` 导出。

## 惰性 workspace 与工具

一次运行需要 Sandbox 或其他待准备文件系统时，把 `Workspace` 声明传给 backend：

```python
runtime = (
    TinkerFin()
    .with_namespace(namespace)
    .build(
        model=model,
        backend=sandboxes.workspace(workspace_key),
    )
)
```

应用自行决定 `workspace_key`，可以按用户、session 或项目划分。Runtime 将自己的 namespace 与该 key 组合，只为已准入的运行准备 workspace，并在清理时释放本次运行的句柄。

工具在执行前定义，通过参数注解取得本次运行信息：

```python
from langchain_core.tools import tool
from tinkerfin.tools import ToolRuntime
from tinkerfin_sandbox import RootedOpenSandboxBackend


@tool
async def read_report(runtime: ToolRuntime[None, RootedOpenSandboxBackend]) -> str:
    """Read the report in this run's workspace."""
    content = await runtime.workspace.aread_bytes("/report.txt", max_bytes=64 * 1024)
    return content.decode("utf-8")


runtime = (
    TinkerFin()
    .with_namespace(namespace)
    .build(
        model=model,
        tools=[read_report],
        backend=sandboxes.workspace(workspace_key),
    )
)
```

`ToolRuntime[ContextT, WorkspaceT, StateT]` 保留原始业务 `context`，并提供本次运行的
`identity` 和借用的 `workspace`。`StateT` 默认为 `DeepAgentState`，runtime 参数不会出现在
模型看到的工具参数中。未配置工作区或运行关闭后访问 `workspace` 会抛出
`TinkerFinLifecycleError`；工具不得关闭工作区，也不得持有它执行超出本次运行的后台任务。

## 子智能体

自动提供的 `general-purpose` 子智能体继承主智能体的模型、工具、技能、权限和审批规则。
其他角色通过 `tinkerfin.subagents.SubAgent` 声明：省略的模型、工具、权限和审批规则继承自
主智能体，技能需显式配置；显式工具列表替换继承的工具。

compiled 和 remote subagent 不继承工作区或附件访问权限，其作者自行选择文件输入方式；
框架创建的图可以使用自身 Runtime 配置的附件策略。注册的 compiled subagent 继承
Runtime 的 checkpointer 和 Store，不应另行固定 checkpoint 坐标。需要智能体维护任务清单时，
显式添加 `TodoListMiddleware`。

## 直接使用 Graph

高级调用方可以构建异步 Graph：

```python
from tinkerfin.deep_agent import create_graph

graph = await create_graph(runtime)
result = await graph.ainvoke(graph_input, config=config)
```

直接 Graph 的执行和清理由调用方负责。它不会创建受管 Runtime 观察、AG-UI 事件、Messaging 投递或每次运行的 workspace 准备。配置了 `workspace(...)` 的 Runtime 必须使用受管执行入口；工具访问工作区和运行身份也需要受管执行。

下一步：[流与 SSE](streams-and-sse.md)。
