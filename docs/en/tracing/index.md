# Semantic tracing

[API reference](api-reference.md) · [中文](../../cn/tracing/index.md)

TinkerFin Tracing records runs, messages, model calls, tools, approvals, and subagents.
Read conversation history, inspect execution graphs, or follow live changes.

## Record a conversation

```python
from tinkerfin import TinkerFin
from tinkerfin_tracing import Tracer

tracer = Tracer()
runtime = (
    TinkerFin()
    .with_observer(tracer)
    .with_namespace("company-a")
    .build(model="openai:gpt-5.4")
)
result = await runtime.ainvoke(
    thread_id="conversation-1",
    run_id="request-1",
    input={"messages": [{"role": "user", "content": "Hello"}]},
)
thread = runtime.thread_identity("conversation-1")
history = await tracer.get(thread)
print(history.messages, history.summary.status.execution)
```

One Tracer and Store can serve multiple namespaces. The host chooses each Runtime's
namespace and authorizes access. Equal thread and Run IDs in different namespaces have
independent histories and writer ownership. Without a Runtime, queries can use
`tinkerfin_contracts.ThreadIdentity(namespace=..., thread_id=...)`.

Invocation and streaming record input and initialization failures before model output.
Consume streams completely or close them explicitly. Direct compiled-graph execution
does not automatically record a complete run.

## History and execution graphs

`tracer.get(thread)` returns a fixed view of messages, reasoning, state, approvals,
status, and `history.graph`. Use `head_run_id` when multiple branches exist.
`history.load_older()` expands that fixed view; `history.events()` pages through its
retained facts. History cursors retain the original namespace, generation, and prefix.

```python
from tinkerfin_tracing import TraceGraphFilter, TraceGraphNodeKind

graph = await tracer.query(
    thread,
    where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL, TraceGraphNodeKind.TOOL}),
)
print(graph.nodes, graph.matched_node_ids)
```

Each turn represents one user task. Only subagents contain nested events; model
associations link related messages and tools without changing their nesting. Tools
retain their proposal, actual execution, result, and approval outcome together.
Missing relationships, incomplete call history, and omitted content are explicit.
A cancelled or interrupted response retains only the content actually captured.

Filters cover kind, status, model, provider, agent, graph scope, time, and retained
public content. Search is literal: ASCII queries ignore ASCII letter case; queries
containing non-ASCII characters are case-sensitive. Query limits bound direct matches,
expanded subagents, searched content, and response bytes. Subagent nesting is limited
to 64 levels. Structural or search-capacity overflow raises `TraceQuotaExceeded`.

## Live updates

`history.follow()` reports history and graph changes. `graph.follow()` is available on
the current first page. Graph cursors bind the current history tail and filter; a new
commit invalidates them. Use an async context to close the subscription even when leaving the loop early:

```python
async with history.follow() as updates:
    async for update in updates:
        await handle(update)
```

`TraceStoreOptions.follow_poll_seconds` controls how often to check changes from other
Store instances; the default is 0.5 seconds. Writer closure or lease expiry without a
recorded terminal reports `unknown` with `missing_tail=True`; it does not claim that
the Agent succeeded. A valid takeover can restore `running` at the same event sequence.

Within one generation, compare updates by `(as_of_seq, observed_at)` and preserve the
UTC timestamp's precision. Equal observations with conflicting contents require a
fresh read. Pull cancellation, pull failure, and leaving the async context close the
upstream subscription.

## Capture and redaction

`CapturePolicy.public_history()` retains sanitized public content, including tool
inputs and results. `public_safe()` uses metadata-only tool capture. Individual tools
can select a policy through `ToolTraceCapture`.

Credential redaction and verified provider-private reasoning removal always apply.
Business redactors receive detached JSON and `RedactionContext`; they must be
synchronous, deterministic, free of I/O, and leave their input unchanged. Invalid
results reject capture. Provider reasoning requires both an enabled Runtime extractor
and `ReasoningCapturePolicy.content()` on the Tracer; the default retains none.

## Storage

`InMemoryTraceStore` is local to one process. `SqlAlchemyTraceStore(engine)` accepts a
borrowed asynchronous PostgreSQL, MySQL, or SQLite Engine. Install
`"tinkerfin-tracing[sqlalchemy]"` and your chosen async driver. Call `setup()` before
serving traffic to create an empty schema or validate existing tables.

Close writers and finish accepted operations before disposing the Engine. In-memory
SQLite requires `AsyncAdaptedQueuePool` with `pool_size=1, max_overflow=0`; `StaticPool`
is rejected. Lease times use the database's UTC clock. MySQL Graph queries require
MySQL 8 or newer. `max_tracer_threads` and `max_tracer_bytes` apply per namespace.

Rebuild graph indexes with `await tracer.rebuild_graph(thread)` without rewriting events.
Codecs can encrypt stored event and checkpoint bytes. Direct writers accept already
captured facts; use `Tracer` for framework and business redaction.

Custom storage implements `TraceLedgerBackend`; graph queries and rebuilding have
separate optional protocols. `verify_trace_ledger_backend()` checks shared ledger
behavior. Replace `TraceStore` to customize queries and persistence together.
Messaging delivery and replay use their own storage contracts.
