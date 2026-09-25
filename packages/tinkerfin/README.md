# tinkerfin

## What it is

`tinkerfin` runs agents with asynchronous output, tool approval, Plan review, and
execution observations. It provides native streams and optional AG-UI events.
`tinkerfin-messaging` adds durable delivery and replay.

The built-in integration uses Deep Agents. Applications choose their models, tools,
storage, resource providers, and isolation namespaces.

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin
```

Install the integrations you use:

```bash
pip install "tinkerfin[agui,redis]" langchain-openai
```

`agui` enables AG-UI conversion. `redis` provides `RedisRunCoordinator` and
`RedisLeaseLock`. Model providers use their corresponding LangChain packages.

## Quick Start

```python
import asyncio

from tinkerfin import TinkerFin

runtime = (
    TinkerFin()
    .with_namespace("company-a")
    .build(
        model="openai:gpt-5.4",
    )
)


async def main() -> None:
    result = await runtime.ainvoke(
        thread_id="conversation-1",
        run_id="request-1",
        input={"messages": [{"role": "user", "content": "Hello"}]},
    )
    print(result["messages"][-1].content)


asyncio.run(main())
```

## Core concepts

### Configure, then run

`TinkerFin` configures capabilities. `with_namespace(...).build(...)` returns an
`AgentRuntime` with a fixed namespace and agent configuration. `build()` performs
no execution I/O. Deriving a builder leaves existing builders and Runtimes unchanged.

Applications choose the namespace and authorize access within it.

Use `ainvoke()` for the final state, `open_run()` for native objects, or
`open_agui_run()` for AG-UI events. Execution takes `thread_id` and `run_id`.
Cross-package integrations can obtain complete identities with
`runtime.thread_identity(thread_id)` and `runtime.run_identity(thread_id, run_id)`.

### Streams and delivery

Stream factories are synchronous and lazy. Consume each stream once and close it
when leaving early:

```python
from contextlib import aclosing

async with aclosing(
    runtime.open_agui_run(
        thread_id="conversation-1",
        run_id="request-2",
        messages=[{"id": "message-2", "role": "user", "content": "Continue"}],
    )
) as events:
    async for event in events:
        print(event)
```

Use exactly one of `messages`, native `input`, or `resume`. Submit only new user
messages when a checkpointer already owns the conversation history. Messages need
final, distinct IDs. Invalid input produces an error lifecycle before agent execution.

Pass the stream directly to a configured Messaging channel:

```python
events = runtime.open_agui_run(
    thread_id=thread_id,
    run_id=run_id,
    messages=messages,
)
body = await channel.open_sse(events)
```

A stream's optional `to_sse()` method provides UTF-8 SSE bytes without durable delivery.
Use your ordinary HTTP response; see the [SSE guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/streams-and-sse.md)
for response examples and cleanup requirements.

### Persistence and tool approval

Supply a borrowed checkpointer to retain conversation state and resume approvals:

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .build(
        model=model,
        tools=tools,
        interrupt_on={"send_report": True},
    )
)
```

Checkpointed built-in Graphs wait for each asynchronous checkpoint write by default.
Tool review, Plan, and resume require `durability="sync"`; this setting waits for
asynchronous persistence and does not select a synchronous database driver.

Runs in the same Runtime namespace and thread share conversation history.

After stopping a conversation's runs, remove its checkpoints without building an agent:

```python
from tinkerfin.checkpoints import delete_thread

await delete_thread(checkpointer, thread=runtime.thread_identity(thread_id))
```

Pass long-term memory with `TinkerFin(store=store)`. Each Runtime uses its own namespace
root while tools see relative memory paths. Use asynchronous Store methods.
`StoreBackend` obtains this scoped Store from the running Graph; omit its `store`
constructor argument. The host retains ownership of the underlying Store.

Resume with `AgUiResumeRequest` entries covering every pending interrupt. See the
[approval guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/interrupts-and-resume.md)
for decisions, cancellation, and custom Graph requirements.

### Plan review

Enable planning before building the Runtime:

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

Plan can reply, ask for clarification, or present a draft for review. It uses the
configured tools and workspace to inspect inputs and run analysis under the same
permissions. Required tool approvals remain in force; approving a tool does not
approve the Plan draft.

Plan requires a concrete checkpointer and an explicit model. Approving the current
draft starts the configured agent. Rejection returns to planning. Editing is available
only when explicitly included in `allowed_review_actions`. Public forms and Plan content
models are available from `tinkerfin.plan`.

### Observations and attachments

`with_observer(tracer)` attaches a runtime observer. For a single asynchronous
terminal callback, use `with_observer(on_terminal=record_terminal)`. The callback
receives `RunTerminalObservation` and is awaited. Callback failure propagates without
emitting a second terminal event. A terminal notification does not guarantee that
all resources have closed or provide delivery after a process crash.

Framework-created agents check native media against the destination model's declared
input capabilities by default. `with_attachments(AttachmentSupport(...))` customizes
that policy and can add authorized reads for stored attachment references. The optional
asynchronous `read_content` function returns `AttachmentContent(data=..., mime_type=...)`
and authorizes and bounds each storage read. Without a reader, stored attachments
remain references for file-reading tools. Model profiles declare image, audio, video,
and PDF input capabilities; `supports_content(model, mime_type)` can provide an explicit
check. When support is unknown or a format is unsupported, model requests contain
text descriptions without changing the original messages.

With a reader, each request reads at most `max_attachments` recent supported files
(default 5).
`max_bytes` limits their total content before base64 encoding (default 20 MiB);
oversized content raises `ValueError` before the model call. Compiled and remote
agents configure their own attachment access. See the runtime guide for submission
parsing and file references.

### Resource ownership

Runtimes borrow supplied models, stores, checkpointers, backends, and observers.
The host initializes and closes shared resources. Each managed run closes its own
execution resources, including after errors or cancellation. Closing an unused
stream does not start preparation.

An `AgUiSettlementTimeoutError` means cleanup is still owned by the stream. Await
`aclose()` again before disposing shared resources. Subscriber disconnect, producer
cancellation, and destruction of a stable Sandbox are separate operations.

### Advanced Graph integration

Registered compiled subagents inherit the Runtime's checkpointer and Store. Build
them without separate storage or fixed checkpoint coordinates. Static tags, metadata,
and business configuration are allowed. Custom Python tools and Runnables own any
external resources they open themselves.

```python
from tinkerfin.deep_agent import create_graph

graph = await create_graph(runtime)
result = await graph.ainvoke(graph_input, config=config)
```

Direct Graph calls are asynchronous Runnables. The caller owns consumption and
closure; direct resume uses LangGraph `Command`. They do not create managed AG-UI,
Messaging, or runtime observations.

Profiles and drivers are available from `tinkerfin.runtime_profile` and
`tinkerfin.native_driver`. Select a profile when constructing `TinkerFin`.
`DeepAgentsV3RuntimeProfile` uses LangGraph's experimental event stream. Checkpoint
resume requires the same profile that created the pending run.

## Documentation

- [Recorded AG-UI conversations](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/api-reference.md#recorded-ag-ui-conversations) — use `AgUiHistory` with `tinkerfin[agui,tracing]` to read and follow an existing Tracer
- [Runtime guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/index.md)
- [AG-UI guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/index.md)
- [Tracing guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/tracing/index.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md)

## License

[Apache-2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
