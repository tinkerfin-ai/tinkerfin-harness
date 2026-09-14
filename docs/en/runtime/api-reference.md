# Runtime API

[Runtime](index.md) · [中文](../../cn/runtime/api-reference.md)

AG-UI entry points require `pip install "tinkerfin[agui]"`.

## Builder

| API | Purpose |
| --- | --- |
| `TinkerFin(checkpointer=..., run_coordinator=..., store=..., runtime_profile=...)` | Configure shared borrowed resources |
| `.with_namespace(namespace)` | Select the application's isolation scope; required before `build()` |
| `.with_observer(observer)` | Add a Runtime observer |
| `.with_observer(on_terminal=callback)` | Add one asynchronous terminal callback |
| `.with_plan(...)` | Enable Plan and choose its model, forms, content, and review actions |
| `.with_attachments(support)` | Enable host-authorized attachment resolution |
| `.build(model, tools, ...)` | Capture agent configuration and return `AgentRuntime` |

Configuration methods return independent builders. `build()` performs no execution I/O
and borrows supplied resources.

### Main build parameters

| Parameter | Purpose |
| --- | --- |
| `model`, `system_prompt` | Model and instructions |
| `tools`, `middleware` | Agent tools and middleware |
| `subagents` | Declarative, compiled, or remote subagents |
| `skills`, `memory` | Skill directories and memory files |
| `backend`, `permissions` | Filesystem backend or lazy workspace and its file rules |
| `interrupt_on` | Require decisions before selected tools execute |
| `response_format` | Structured output contract |
| `state_schema`, `context_schema` | Persistent state fields and invocation context type |

`model` is required. Configure persistence on `TinkerFin(checkpointer=..., store=...)`.
`context_schema` determines the type of each execution method's `context` argument.

## AgentRuntime

| API | Result |
| --- | --- |
| `runtime.namespace` | Namespace fixed when the Runtime was built |
| `runtime.thread_identity(thread_id)` | Complete namespaced thread identity |
| `runtime.run_identity(thread_id, run_id)` | Complete namespaced run identity |
| `await runtime.ainvoke(...)` | Final defensive state mapping |
| `runtime.open_run(...)` | Lazy `NativeRunStream` |
| `runtime.open_agui_run(...)` | Lazy `AgUiRunStream` |
| `runtime.agui.history(tracer)` | Recorded AG-UI conversations in this Runtime's namespace |

All execution methods accept `thread_id`, `run_id`, `input` or AG-UI input, optional
`mode`, Graph `config`, typed `context`, observation callbacks, and supported LangGraph
stream controls.

`open_agui_run()` accepts exactly one of:

| Input | Use |
| --- | --- |
| `messages` | Standard AG-UI user messages with final unique IDs |
| `input` | Advanced native agent state |
| `resume` | `AgUiResumeRequest` for all pending interrupts |

Resume-only callbacks are `on_resume_saved` and `on_resume_not_saved`. They settle host
state around the durable resume marker and are awaited.

## Recorded AG-UI conversations

Install `tinkerfin[agui,tracing]` and select an existing Tracer as the history source:

```python
from tinkerfin.agui import AgUiHistory

history = AgUiHistory(tracer, namespace=namespace)
view = await history.get(thread_id)
snapshot = view.snapshot

async with view.follow() as updates:
    async for update in updates:
        await publish(update)
```

With an existing Runtime, `runtime.agui.history(tracer)` uses its namespace.
Neither entry point creates an agent or takes ownership of the Tracer or its Store.
The application authorizes the namespace and conversation before querying.

Snapshots and updates include live AG-UI references for messages, tools, subagents,
and pending interactions. A missing reference or omitted interaction content is
represented by `agui=None`; it cannot be reconstructed into an approval request.
`view.trace` exposes original facts and registered application projections.

`await view.load_older(limit=100)` expands the loaded history at the same fixed
prefix. For a filtered execution graph, use `await history.query(thread_id,
where=filters)` and read its `snapshot` or `follow()` updates. Graph cursors apply
to the same filter and graph tail; query again if that tail changes. Use `async with`
for followers when stopping early. Cancellation and query errors propagate, and
closing a follower leaves the caller's history source open.

Use `open_live()` to resume a running conversation after a reload or on a new device:

```python
channel = messaging.agui_channel(name="conversations")
live = await history.open_live(thread_id, channel=channel, head_run_id=run_id)
try:
    await send_snapshot(live.history.snapshot, replay=live.body is not None)
    if live.body is not None:
        async for frame in live.body:
            await send_bytes(frame)
finally:
    await live.aclose()
```

The host implements `send_snapshot` and `send_bytes`. An active run returns history
before its output, followed by committed deltas and new events. A completed run returns
full history with `body=None`. For a connection loss with the entire view retained,
pass `last_event_id` and consume only subsequent bytes; omit it after a page reload.
SSE frames marked `event: replay` contain previously committed events; subsequent events use `message`.
History cursors and message SSE IDs are different positions and cannot be mixed.
Closing `live` detaches the reader without cancelling the producer. Expired or
unavailable delivery raises an error and never re-executes the agent.

## Streams

| API | Purpose |
| --- | --- |
| `NativeRunStream` | Iterate validated native objects; `to_sse()` uses canonical replay values |
| `AgUiRunStream` | Iterate AG-UI events; `abort()` returns the remaining cancellation events |
| `stream.aclose()` | Finish owned cleanup; safe to await again after a cleanup timeout |
| `stream.to_sse()` | Return a single-use `SseBody[bytes]` of UTF-8 SSE frames |
| `SseBody.prepare(preflight=...)` | Run checks before the response starts |

`AgUiSettlementTimeoutError` means the stream still owns cleanup. Await `aclose()` again
before closing shared resources.

`NativeStreamPart` is the finite native replay value. Its Python field
`graph_namespace` identifies Graph position; its third-party wire alias remains `ns`.
Business isolation always comes from `RunIdentity.namespace`.

## Public modules

Common Runtime types, streams, SSE helpers, and errors are exported from `tinkerfin`.
Extension contracts use focused modules:

| Module | API family |
| --- | --- |
| `tinkerfin.agui` | `AgUiHistory` and recorded conversation models |
| `tinkerfin.tools` | `ToolRuntime` with business context, run identity, and workspace access |
| `tinkerfin.media` | `Attachment`, `AttachmentContent`, and `AttachmentSupport` |
| `tinkerfin.subagents` | Declarative `SubAgent` configuration |
| `tinkerfin.runtime_profile` | Deep Agents Runtime profiles |
| `tinkerfin.native_driver` | Native stream drivers and reasoning extractors |
| `tinkerfin.coordination` | Run coordinator protocol and in-memory implementation |
| `tinkerfin.redis` | Redis coordinator, leases, and Redis lease errors |
| `tinkerfin.plan` | Plan forms, content models, decisions, and Plan errors |
| `tinkerfin.deep_agent` | Caller-managed Graph construction |

The application opens and closes models, checkpointers, Stores, database pools,
and supplied service clients. Managed runs close the Graph, prepared workspace, streams,
and per-run resources they create.
