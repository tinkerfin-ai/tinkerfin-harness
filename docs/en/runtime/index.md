# Runtime

[Documentation](../index.md) · [中文](../../cn/runtime/index.md)

`TinkerFin` configures an agent. `with_namespace(...).build(...)` returns an
`AgentRuntime` that executes that configuration. Building does not open a model,
database, Sandbox, or Graph.

## Installation

```bash
pip install tinkerfin
```

Install `tinkerfin[agui]` to produce AG-UI events. Install the LangChain package for
the model provider you use.

## Build and run

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
    input={"messages": [{"role": "user", "content": "Summarize this request"}]},
)

async with aclosing(stream):
    async for part in stream:
        await handle(part)
```

Choose the execution method by result:

| Goal | API |
| --- | --- |
| Get the final state | `await runtime.ainvoke(...)` |
| Consume native objects | `runtime.open_run(...)` |
| Consume AG-UI events | `runtime.open_agui_run(...)` |
| Build a caller-managed Graph | `await tinkerfin.deep_agent.create_graph(runtime)` |

Stream factories are synchronous and lazy. Preparation starts only when a stream is
preflighted or consumed. Close a stream when the consumer stops early.

## Identity and isolation

The application chooses the Runtime namespace. It may represent a tenant, user,
project, or another business scope. TinkerFin treats it as opaque text.

| Value | Meaning |
| --- | --- |
| `runtime.namespace` | Business data and resource scope fixed at build time |
| `thread_id` | Continuing conversation inside that namespace |
| `run_id` | One semantic execution inside the thread |
| `graph_namespace` | Position of an event inside the execution Graph |

Use `runtime.run_identity(thread_id, run_id)` when another TinkerFin package needs the
complete identity. Graph namespace is execution lineage and does not replace the
business namespace.

## Persistence and resources

Pass a checkpointer to retain conversation state and resume tool or Plan approval.
Pass a Store for long-term memory. The Runtime scopes both by its namespace.

```python
runtime = (
    TinkerFin(checkpointer=checkpointer, store=store)
    .with_namespace(namespace)
    .build(model=model, tools=tools)
)
```

Models, stores, checkpointers, coordinators, and supplied backends remain owned
by the application. Each run owns its Graph, lazy workspace preparation, stream, and
cleanup.

## Next steps

- [Run your first agent](quick_start.md)
- [Configure agents and Plan](agent-configuration.md)
- [Streams and SSE](streams-and-sse.md)
- [Runtime extensions](extensions.md)
- [Runtime API](api-reference.md)
