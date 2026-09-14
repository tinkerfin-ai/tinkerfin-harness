# TinkerFin Tracing

TinkerFin Tracing records Agent runs, messages, model calls, tools, approvals, and
subagents. Query conversation history and execution graphs, follow live updates, or
keep traces in memory or shared storage.

## Installation

```bash
pip install tinkerfin-tracing

# SQL storage, with the asynchronous driver of your choice
pip install "tinkerfin-tracing[sqlalchemy]" asyncpg

```

Use `asyncpg` for PostgreSQL, `asyncmy` for MySQL, or `aiosqlite` for SQLite. The
`sqlalchemy` extra installs asynchronous SQLAlchemy support without choosing a driver.

## Quick Start

Install `tinkerfin` and `langchain-openai`, and set `OPENAI_API_KEY` for this example.

```python
from langchain_core.messages import HumanMessage
from tinkerfin import TinkerFin
from tinkerfin_tracing import Tracer

tracer = Tracer()
runtime = (
    TinkerFin()
    .with_namespace("default")
    .with_observer(tracer)
    .build(
        model="openai:gpt-5.4",
        tools=[],
    )
)

result = await runtime.ainvoke(
    thread_id="thread-1",
    run_id="run-1",
    input={"messages": [HumanMessage(content="Hello")]},
)
thread = runtime.thread_identity("thread-1")
history = await tracer.get(thread)
print(history.messages)
print(history.summary.status.execution)
print(history.graph.nodes)
```

Runtime invocation and streaming record the same lifecycle, including failures before
model output. Consume streams completely or close them with `await stream.aclose()`.
Direct execution of a compiled graph does not automatically produce a complete Trace.

## History and execution graphs

One Tracer and Store can serve multiple Runtime namespaces. Writers use the Runtime's
identity; queries use `runtime.thread_identity(thread_id)` or a shared
`tinkerfin_contracts.ThreadIdentity(namespace=..., thread_id=...)`. The host chooses
namespace ownership and authorizes access. Equal thread and Run IDs in different
namespaces have independent histories, writer ownership, and capacity budgets.

`tracer.get(thread)` returns messages, state, approvals, run status, and the execution
graph at one fixed point in the history. Select `head_run_id` when the thread has
multiple branches. Follow its updates with `history.follow()`.

```python
from tinkerfin_tracing import TraceGraphFilter, TraceGraphNodeKind

graph = await tracer.query(
    thread,
    where=TraceGraphFilter(
        kinds={TraceGraphNodeKind.MODEL, TraceGraphNodeKind.TOOL},
    ),
)
print(graph.nodes, graph.matched_node_ids)
```

Filters cover kind, status, model, provider, agent, graph namespace, time, and literal
content search. Search uses retained, sanitized content. ASCII queries ignore ASCII
letter case; queries containing non-ASCII characters are case-sensitive.

Each turn groups one user task. Subagents contain their own events. Model associations
link related tools and messages without changing their display nesting. Missing
relationships or incomplete call history are reported explicitly.

Graph cursors belong to the selected thread, branch, filter, and current history tail.
A new commit invalidates an earlier Graph cursor. `graph.follow()` is available on the
current first page; history cursors retain their original fixed view. Follow streams
release database connections while waiting. Changes from another Store instance are
checked every 0.5 seconds by default, configurable through
`TraceStoreOptions.follow_poll_seconds`.

Query limits bound matches, searched content, expanded subagents, and response bytes.
Oversized details can be omitted while retaining graph structure; a structural overflow
raises `TraceQuotaExceeded`. Subagent nesting is limited to 64 levels.

## Capture and redaction

`CapturePolicy.public_history()` retains sanitized public content, including tool
inputs and results. Use `CapturePolicy.public_safe()` for metadata-only tool capture,
or configure individual tools with `ToolTraceCapture`.

Credential redaction and provider-private reasoning removal always apply. Add business
rules through `Tracer(redactor=...)`; `CompositeRedactor` combines rules, and
`redact_json_paths()` removes selected RFC 6901 paths. A redactor receives detached
JSON and a `RedactionContext`. It must be deterministic, synchronous, free of I/O, and
leave its input unchanged. Invalid results reject capture.

Reasoning requires both an explicitly enabled Runtime extractor and
`ReasoningCapturePolicy.content()` on the Tracer. The default retains no reasoning
content or digest. Incomplete model output is marked rather than presented as a
complete response.

## Durable storage

`InMemoryTraceStore` is bounded and local to one process. `SqlAlchemyTraceStore`
accepts a borrowed asynchronous PostgreSQL, MySQL, or SQLite Engine:

```python
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_tracing import SqlAlchemyTraceStore, Tracer

engine = create_async_engine("postgresql+asyncpg://user:password@db/tinkerfin")
store = SqlAlchemyTraceStore(engine)
await store.setup()
tracer = Tracer(store=store)

# Dispose after accepted operations and all writers have finished.
await engine.dispose()
```

`setup()` creates an empty schema or validates the complete existing schema. Partial
or mismatched tables are rejected. Cancellation waits for accepted setup to finish.
`get_trace_store_schema(dialect="postgresql").ddl` provides the same tables, indexes,
and comments for offline setup.

SQLite file databases work with the default pool. In-memory SQLite requires
`AsyncAdaptedQueuePool` with `pool_size=1, max_overflow=0`; `StaticPool` does not isolate
concurrent borrowers. The Store never disposes the Engine or changes its pool size.
MySQL Graph queries require MySQL 8 or newer.

The `max_tracer_threads` and `max_tracer_bytes` limits apply separately to each namespace.
Graph indexes can be rebuilt with `await tracer.rebuild_graph(thread)` without
rewriting recorded events. A codec can encrypt stored event and checkpoint bytes.

## Custom storage

`DurableTraceStore.supports_graph_queries` and `supports_graph_rebuild` report the
borrowed backend's available Graph features. A Ledger-only backend still supports
ordinary history. Indexed queries and rebuilding require their respective extensions.

Implement `TraceLedgerBackend` for shared durable storage; Graph query and rebuild
extensions use `TraceGraphQueryBackend` and `TraceGraphRebuildBackend`. Use
`verify_trace_ledger_backend()` to check the Ledger contract across cooperating instances.
Replace `TraceStore` to customize query and persistence behavior together.

Direct `TraceWriter` calls accept already captured facts. Use `Tracer` when content
requires framework and business redaction. Messaging delivery and replay use their own
storage contracts.

## Documentation

- [Tracing guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/tracing/index.md)
- [API reference](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/tracing/api-reference.md)

## License

Apache License 2.0. See [LICENSE](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE).
