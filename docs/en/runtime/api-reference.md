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
| `await runtime.compact(thread_id=..., run_id=...)` | Saved `CompactionResult` without adding chat messages |
| `runtime.agui.open_compaction(thread_id=..., run_id=...)` | Lazy AG-UI context compression stream |
| `runtime.agui.history(tracer)` | Recorded AG-UI conversations in this Runtime's namespace |

Chat execution methods accept `thread_id`, `run_id`, `input` or AG-UI input, optional
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

## Compress saved context

Use an existing Runtime configured with a checkpointer. Manual compression shares the
native `compact_conversation` eligibility rules and the effective summarization middleware's
model, retention, prompt, input trimming, and retry behavior. Eligibility begins at half the
automatic trigger; token thresholds require matching provider-reported usage. Below that
threshold, or without the required usage, no summary model runs. Original messages remain
saved, and retained recent context preserves tool-call pairs. Resolve pending work, approval,
or Plan input first.

```python
result = await runtime.compact(thread_id=thread_id, run_id=run_id)
if result.status == "compacted":
    print(result.summary)
```

`CompactionResult` contains `run_id`, `status`, `summary`, and `compacted_messages`.
`nothing_to_compact` skips the model call; `not_reduced` leaves context unchanged because
the generated summary was not shorter. Failures raise without returning a success result.
`compact()` also accepts the Runtime's typed `context`.

To let the main agent invoke compression during a conversation, enable the tool when building:

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace("account")
    .with_compaction_tool()
    .build(model=model)
)
```

The tool is off by default and does not propagate to subagents or the Plan planner.
It runs within the current conversation run. The framework binds it to the effective
summarization middleware and manages its native state update. Automatic and tool compression
retain native behavior; the manual endpoint additionally rejects a summary that does not
reduce context and requires successful archiving. Tracing groups summary model calls under
the compression action and confirms persistence only after checkpoint saving succeeds.

For replayable AG-UI delivery, use the same Messaging channel as ordinary chat:

```python
from contextlib import aclosing

channel = messaging.agui_channel(name="conversations")
source = runtime.agui.open_compaction(thread_id=thread_id, run_id=run_id)
body = await channel.open_sse(source)
async with aclosing(body):
    async for frame in body:
        await send_bytes(frame)
```

The host authorizes the conversation, assigns one `run_id` per operation, and owns
the transport. Reconnects reuse that ID and the channel's delivery cursor. The framework
owns execution, workspace cleanup, and persistence; it borrows the Runtime's configured
resources. Use the same execution boundary for chat and compression. Closing the
Messaging reader detaches it; cancel through the channel to stop the producer.

Compression emits one run lifecycle and no assistant message. A saved result appears
in the AG-UI state field `context_compaction`. When a Tracer is configured as a Runtime
observer, the result is also recorded in the `context_compaction` history node.
The RAW `langgraph.custom` event with `event.data.phase="saving"` indicates persistence
has begun. Cancellation or failure at that point does not prove that context was unchanged;
check history before requesting another operation.

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
