# TinkerFin Tracing

TinkerFin Tracing records Agent runs, messages, model calls, tools, approvals, and
subagents. Query conversation history and execution graphs, follow live updates, or
keep traces in memory or shared storage.

## Installation

```bash
pip install tinkerfin-tracing
```

For shared SQL storage, install the extra and an asynchronous database driver:

```bash
pip install "tinkerfin-tracing[sqlalchemy]" asyncpg
```

Use `asyncpg` for PostgreSQL, `asyncmy` for MySQL, or `aiosqlite` for SQLite.

## Quick Start

Install `tinkerfin` and `langchain-openai`, and set `OPENAI_API_KEY` for this example.
Replace the model ID with one available to your account.

```python
import asyncio

from tinkerfin import TinkerFin
from tinkerfin_tracing import Tracer


async def main() -> None:
    tracer = Tracer()
    runtime = (
        TinkerFin()
        .with_namespace("company-a")
        .with_observer(tracer)
        .build(model="openai:gpt-5.4")
    )
    await runtime.ainvoke(
        thread_id="thread-1",
        run_id="run-1",
        input={"messages": [{"role": "user", "content": "Hello"}]},
    )
    history = await tracer.get(runtime.thread_identity("thread-1"))
    print(history.messages)
    print(history.summary.status.execution)
    print(history.graph.nodes)


asyncio.run(main())
```

Runtime invocation and streaming record the same lifecycle, including failures before
model output. Consume streams completely or close them with `await stream.aclose()`.
Direct execution of a compiled graph does not automatically produce a complete Trace.

## Read and follow history

One Tracer can serve multiple Runtime namespaces. Query with
`runtime.thread_identity(thread_id)` or a `tinkerfin_contracts.ThreadIdentity` that
the host has authorized. Equal thread and run IDs in different namespaces have
independent histories.

`tracer.get(thread)` returns messages, state, approvals, run status, and the execution
graph at a fixed point in history. Select `head_run_id` for a branch. Use
`tracer.query(thread, where=TraceGraphFilter(...))` to filter model calls, tools, and
subagents. Model nodes expose `request_reference` for retrieving retained inputs
with `tracer.model_request(...)`.

Follow updates with `history.follow()` or the current first graph page's `follow()`;
close the subscription on early exit. Graph cursors bind the thread, branch, filter,
and current history tail, so a new commit invalidates an earlier graph cursor.
History cursors retain their fixed view. Queries are bounded; a structural overflow
raises `TraceQuotaExceeded`.

## Choose what to retain

The default `CapturePolicy.public_history()` retains sanitized public content,
including tool inputs and results. Use `CapturePolicy.public_safe()` for
metadata-only tool capture, or `ToolTraceCapture` for individual tools.

Credential redaction and provider-private reasoning removal always apply. Add business
rules with `Tracer(redactor=...)`. Redactors must be deterministic, synchronous,
free of I/O, and leave their input unchanged; invalid results reject capture.
Reasoning content requires both an enabled Runtime extractor and
`ReasoningCapturePolicy.content()` on the Tracer. The default retains neither
reasoning content nor its digest.

Direct `TraceWriter` calls accept already captured facts. Use `Tracer` when content
requires framework and business redaction.

## Durable storage and ownership

The default `InMemoryTraceStore` is bounded and local to one process. For SQL
storage, create `SqlAlchemyTraceStore(engine)`, await `store.setup()`, and pass it
to `Tracer(store=store)`. The Tracer borrows the Store; the SQL Store borrows the
application's `AsyncEngine`. Finish all writers, followers, and accepted operations
before disposing the Engine.

Setup creates an empty schema or validates existing tables and indexes. Partial or
mismatched schemas are rejected. Cancellation waits for accepted setup to finish.
SQLite files work with the default pool; in-memory SQLite requires
`AsyncAdaptedQueuePool` with `pool_size=1, max_overflow=0`. MySQL graph queries
require MySQL 8 or newer, and payload tables require InnoDB file-per-table tablespaces.

Pass a started `Notifications` service to the Store's `notifications` argument for
cross-instance wakeups, using the same Redis channel in each process. The service
remains host-owned and must outlive writers and followers. Hints contain identities,
not captured content; followers repair missed notifications through authoritative
reads. Without Notifications, followers poll storage.

See the guide for capture limits, encrypted codecs, graph rebuilding, and custom
storage contracts.

## Documentation

- [Tracing guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/tracing/index.md)
- [API reference](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/tracing/api-reference.md)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
