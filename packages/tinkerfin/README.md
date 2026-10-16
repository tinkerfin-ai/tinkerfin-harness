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

Install `tinkerfin[agui]` for AG-UI output or `tinkerfin[redis]` for shared run
coordination and resource leases. Model providers use their corresponding
LangChain packages.

## Quick Start

The example uses the OpenAI integration. Install its provider package and set
`OPENAI_API_KEY` in your environment. Replace the model ID with one available to
your account.

```bash
pip install langchain-openai
```

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

## Persistent files shared with an editor

Bind a collection once to use the same persistent files from an application and
agent file tools. The Store must implement atomic conditional writes and exact
collection queries, such as
`tinkerfin-langgraph-store`'s `SqlAlchemyStore`. The application owns and closes
its Store and Engine; the file collection and Runtime borrow them.

```python
import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin import TinkerFin
from tinkerfin.files import FileConflict
from tinkerfin_langgraph_store import SqlAlchemyStore


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///files.db")
    try:
        async with SqlAlchemyStore(engine) as store:
            configured = TinkerFin(store=store).with_namespace("account-a")
            files = configured.files(("project-a", "memories"))
            runtime = configured.build(model="openai:gpt-5.4", backend=files.backend)
            try:
                snapshot = await files.read("/preferences.md")
            except FileNotFoundError:
                snapshot = await files.create("/preferences.md", b"Initial preferences")

            draft = b"Prefer concise explanations"
            try:
                saved = await files.update(
                    snapshot.path, draft, expected_etag=snapshot.etag
                )
                print(saved.content)
            except FileConflict:
                # Retain draft and reread before asking its author to resolve the conflict.
                print("The file changed; the draft has not been saved")

            await runtime.ainvoke(
                thread_id="conversation-1",
                run_id="request-1",
                input={
                    "messages": [{"role": "user", "content": "Read /preferences.md"}]
                },
            )
    finally:
        await engine.dispose()


asyncio.run(main())
```

This SQLite example additionally needs
`pip install "tinkerfin-langgraph-store[sqlalchemy]" aiosqlite` and the model
provider configured in Quick Start.

File paths are canonical absolute virtual paths. `create()` refuses existing files;
updates and deletes require the observed `expected_etag`. On `FileConflict`, retain
the draft and reread before resolving the change. Agent file tools use the same
conditional writes. A collection's backend must run under the namespace that created it.

Agent directory deletion is recursive and conditions each file on its observed
snapshot. A conflict or cancellation can leave earlier files deleted; new or
changed files are retained. A reported incomplete deletion requires reading the
remaining files before retrying. Directory deletion is not one transaction.

## Stream output

Use `ainvoke()` for the final state, `open_run()` for native objects, or
`open_agui_run()` for AG-UI events. Install `tinkerfin[agui]` for this continuation of
the Quick Start example:

```python
from contextlib import aclosing


async def stream_reply() -> None:
    async with aclosing(
        runtime.open_agui_run(
            thread_id="conversation-1",
            run_id="request-2",
            messages=[{"id": "message-2", "role": "user", "content": "Hello again"}],
        )
    ) as events:
        async for event in events:
            print(event)


asyncio.run(stream_reply())
```

Streams are lazy and single-use. Close them when leaving early. Supply exactly one
of `messages`, native `input`, or `resume`; messages need final, distinct IDs. With a
checkpointer, submit only new user messages because it already owns the history.

Use `stream.to_sse()` for UTF-8 SSE bytes and your application's HTTP response.
For durable delivery and replay, pass the object stream to
[`tinkerfin-messaging`](https://github.com/tinkerfin-ai/tinkerfin-harness/tree/main/packages/tinkerfin-messaging)
or use [Gateway](https://github.com/tinkerfin-ai/tinkerfin-harness/tree/main/packages/tinkerfin-gateway).
The [SSE guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/streams-and-sse.md)
includes response examples and cleanup requirements.

## Persistence, approval, and Plan

Supply a checkpointer with `TinkerFin(checkpointer=...)` to retain conversations and
resume human-input requests. Runs with the same Runtime namespace and thread share
history. Add long-term memory with `TinkerFin(store=...)`; Store operations must be
asynchronous. Applications select namespaces and authorize access within them.

Configure tool review with `build(..., interrupt_on={"send_report": True})`.
Enable Plan with `.with_plan(planner_model=...)` before `build()`, then select
`mode="plan"` for a run. Both require a concrete checkpointer, and Plan requires an
explicit model. Tool review, Plan, and resume require `durability="sync"`, which
waits for asynchronous persistence rather than selecting a blocking database driver.

Plan may reply, request clarification, or present a draft. It can inspect inputs and
run analysis with the configured tools, workspace, and permissions. Tool approvals
remain in force and do not approve the Plan draft. Approving the current draft
starts the configured agent. See
[agent configuration](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/agent-configuration.md)
and [approval and resume](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/interrupts-and-resume.md)
for complete setup and decisions covering every pending interrupt.

## Other capabilities

- Record execution history with `.with_observer(tracer)` and
  [`tinkerfin-tracing`](https://github.com/tinkerfin-ai/tinkerfin-harness/tree/main/packages/tinkerfin-tracing).
  A terminal callback is awaited but does not guarantee that all resources have
  closed or provide delivery after a process crash.
- Configure attachment access with `.with_attachments(AttachmentSupport(...))`.
  An optional async `read_content` function must authorize and bound each stored
  file read. Unsupported media becomes a text description; without a reader,
  stored attachments remain references for file-reading tools. See
  [attachment inputs](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/agent-configuration.md#attachment-inputs)
  for the reader contract and request limits.
- Configure tools, skills, memory, subagents, and workspace providers through
  `build()`. [Runtime extensions](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/extensions.md)
  cover observers, shared coordination, and integration profiles.

## Resource ownership

`build()` validates configuration without execution I/O. Runtimes borrow supplied
models, stores, checkpointers, backends, and observers. The host initializes and
closes shared resources; each managed run closes its own execution resources,
including after errors or cancellation. Closing an unused stream does not start it.

An `AgUiSettlementTimeoutError` means the stream still owns cleanup. Await `aclose()`
again before disposing shared resources. Disconnecting a Messaging subscriber,
cancelling its producer, and destroying a stable Sandbox are separate operations.
Custom Python tools own any external resources they open themselves.

## Documentation

- [Runtime guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/index.md)
- [AG-UI guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/index.md)
- [Recorded AG-UI conversations](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/api-reference.md#recorded-ag-ui-conversations)
- [API reference](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/api-reference.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md)

## License

[Apache-2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
