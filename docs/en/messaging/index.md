# Messaging basics

[Documentation](../index.md) · [中文](../../cn/messaging/index.md)

Messaging turns a single-use object stream into a durable producer that supports replay, attachment, and remote cancellation. An agent can keep running after a browser disconnects, and a later request resumes from the last durable sequence.

## Installation

Core messaging is protocol-neutral:

```bash
pip install tinkerfin-messaging
```

Install the codecs and backend used by the host. The example below needs AG-UI:

```bash
pip install "tinkerfin[agui]" "tinkerfin-messaging[agui]"
pip install "tinkerfin-messaging[native]"
pip install "tinkerfin-messaging[agui,redis]"
```

## Turn a TinkerFin stream into resumable SSE

```python
from contextlib import aclosing

from tinkerfin import TinkerFin
from tinkerfin_messaging import Messaging

runtime = TinkerFin().with_namespace("customer-1").build(model=model, tools=tools)
source = runtime.open_agui_run(
    thread_id="thread-42",
    run_id="run-7",
    input=graph_input,
)

async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")
    body = await channel.open_sse(source, after=0)
    async with aclosing(body):
        async for chunk in body:
            await send_to_client(chunk)
```

Runtime supplies the namespace, identity and codec. Messaging selects the producer
before opening the Agent or Sandbox; an attachment reuses the existing run. Each source
is single-use, including an unused attachment candidate. The caller closes the returned
SSE body on early return or failed HTTP setup. Pass Runtime object streams directly to
Messaging; do not encode them as SSE first.

Use `on_source_ready` and `on_delivery_not_started` on the channel only when application
delivery state needs activation or cleanup. Attachments invoke neither callback.
`model`, `tools`, `graph_input` and the HTTP sender belong to the application.

Use `[agui]` for `AgUiCodec`, `[native]` for `NativeStreamPartCodec`, `[redis]` for
`RedisBackend`, or `[sqlalchemy]` plus an async driver for `SqlAlchemyBackend`.

## Custom sources

Custom sources have no identity profile, so provide one explicitly:

```python
channel = messaging.channel(name="custom", codec=codec)
subscription = await channel.wrap(source, identity=identity, after=0)
```

If a source has a RunIdentity profile and an explicit different RunIdentity is supplied, preflight fails before backend preparation or source opening.

## Core concepts

| Name | Purpose |
| --- | --- |
| channel name | Stable payload format, such as AG-UI |
| `RunIdentity.namespace` | Application-defined isolation scope |
| `RunIdentity.thread_id` | Ordered log, generation, and replay cursor scope |
| `RunIdentity.run_id` | Semantic producer and caller idempotency key |
| `seq` | One-based committed position in the thread log |

The same RunIdentity always means the same semantic run. Reuse it for retries and attachment; use a new run_id for new input. Messaging does not compare request bodies—authorization and business idempotency belong to the caller.

## `after`

| Value | Behavior |
| --- | --- |
| `None` | Capture the current tail during prepare and receive later data |
| `0` | Replay from the first retained message |
| `N` | Return messages where `seq > N` |

Negative cursors and cursors beyond the current generation tail raise `InvalidCursor`.

## Application lifecycle

```python
async with Messaging(backend=backend) as messaging:
    channel = messaging.channel(name="agent-events")
    await serve_application(channel)
```

Closing waits for producers and their cleanup. Close Messaging before releasing its borrowed storage resources.

## Next steps

- [Delivery, replay, and SSE](delivery-and-replay.md)
- [Cancellation, deferred sources, and recovery](cancellation-and-recovery.md)
- [Storage, codecs, and custom backends](backends-and-codecs.md)
- [Messaging usage reference](api-reference.md)
