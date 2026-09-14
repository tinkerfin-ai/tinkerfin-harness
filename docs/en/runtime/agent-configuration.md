# Configure an agent

[Runtime](index.md) · [中文](../../cn/runtime/agent-configuration.md)

Configure models, tools, file access, delegation, and approval with `build()`.
It returns a reusable `AgentRuntime` for running the agent within your application.

```python
from tinkerfin import TinkerFin

runtime = (
    TinkerFin(checkpointer=checkpointer, store=store)
    .with_namespace("support")
    .build(
        name="support-agent",
        model="openai:gpt-5.4",
        tools=[search_orders],
        system_prompt="Answer questions about customer orders.",
    )
)
```

## Build options

| Capability | Parameter |
| --- | --- |
| Model and instructions | `model`, `system_prompt` |
| Tools and middleware | `tools`, `middleware` |
| Delegation | `subagents` |
| Skills and memory files | `skills`, `memory` |
| Filesystem access | `backend`, `permissions` |
| Tool approval | `interrupt_on` |
| Structured output | `response_format` |
| State and invocation context | `state_schema`, `context_schema` |

`model` is required. Configure the checkpointer and Store on `TinkerFin(...)`.
Supplied resources are borrowed. `build()` validates and captures configuration but
performs no execution I/O.

Store-backed file, memory, skill, and summarization middleware must omit the
`StoreBackend(store=...)` argument and use the Store passed to `TinkerFin(store=...)`.
The Runtime applies namespace isolation and asynchronous file access to these
built-in declarations. Their caller-owned configuration remains unchanged.

With a workspace, the Runtime owns the filesystem middleware and built-in file tools;
do not replace them. With an ordinary backend, custom filesystem middleware or
file-tool replacements are allowed only for roles without effective `permissions`.
Conflicting declarations raise `ValueError` during `build()`.
Configure delegation through `subagents` and tool approval through `interrupt_on`.
Their middleware cannot be replaced; `task` and the tools of configured remote agents
are reserved for delegation.

## Persistence and approval

A concrete checkpointer is required for resumable tool approval and Plan. Submit only
new user messages when the checkpointer already contains the thread history.

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

Checkpointed approval uses `durability="sync"`. This waits for asynchronous checkpoint
writes; it does not select a synchronous database driver.

## Plan before execution

Enable Plan on the builder, then select `mode="plan"` for a request:

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

Plan can clarify requirements and ask the user to approve a draft before agent
execution. Its built-in filesystem access is read-only. `mode="default"` executes the
agent directly. Plan models and review actions are exported from `tinkerfin.plan`.

## Lazy workspaces and tools

Pass a `Workspace` declaration as the backend when a run needs a Sandbox or another
prepared filesystem:

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

The application chooses `workspace_key`; user, session, and project keys are all valid
policies. The Runtime combines its namespace with that key, prepares the workspace only
for an admitted run, and releases the run's handle during cleanup.

Define tools before execution and annotate their injected runtime:

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

`ToolRuntime[ContextT, WorkspaceT, StateT]` preserves the original business `context`
and exposes the current run's `identity` and borrowed `workspace`. `StateT` defaults
to `DeepAgentState`. The runtime parameter is hidden from the model's tool schema.
Accessing `workspace` without a configured workspace or after the run closes raises
`TinkerFinLifecycleError`. Tools must not close or retain it for background work.

## Subagents

An automatic `general-purpose` subagent inherits the root model, tools, skills,
permissions, and approval rules. Declare other roles with `tinkerfin.subagents.SubAgent`.
Omitted model, tools, permissions, and approval rules inherit from the root; skills
must be configured explicitly. An explicit tool list replaces inherited tools.

Compiled and remote subagents do not inherit workspace or attachment access. Their
authors choose their file-input mechanism; framework-created graphs can use the
attachment policy configured on their own Runtime.
Registered compiled subagents inherit the Runtime checkpointer and Store; do not bind
separate checkpoint coordinates to them. Add `TodoListMiddleware` explicitly when the
agent needs task lists.

## Direct Graph access

Advanced callers can build an asynchronous Graph:

```python
from tinkerfin.deep_agent import create_graph

graph = await create_graph(runtime)
result = await graph.ainvoke(graph_input, config=config)
```

The caller owns direct Graph execution and cleanup. Direct Graphs do not create managed
Runtime observations, AG-UI events, Messaging delivery, or per-run workspace
preparation. A Runtime using `workspace(...)` must use managed execution methods.
Tool workspace and run identity access also require managed execution.

Next: [Streams and SSE](streams-and-sse.md).
