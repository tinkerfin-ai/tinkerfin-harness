# tinkerfin-messaging

## What it is

`tinkerfin-messaging` persists and replays asynchronous object streams. It supports
reconnection, shared producer ownership, remote cancellation, and recovery from saved
source positions. Storage and message formats are independently replaceable.

## Installation

```bash
pip install tinkerfin-messaging
```

Install the integrations you use:

| Extra | Capability |
| --- | --- |
| `[agui]` | AG-UI encoding, replay and SSE |
| `[native]` | TinkerFin Native encoding, replay and SSE |
| `[sqlalchemy]` | SQLite, MySQL or PostgreSQL storage; install an async driver separately |
| `[redis]` | Shared Redis storage |

The Runtime example below needs both AG-UI integrations:

```bash
pip install "tinkerfin[agui]" "tinkerfin-messaging[agui]"
```

## Quick Start

Build a Runtime, create its lazy object stream, and pass it to a Messaging channel:

```python
from contextlib import aclosing

from tinkerfin import TinkerFin
from tinkerfin_messaging import Messaging

runtime = TinkerFin().with_namespace("customer-1").build(model=model, tools=tools)
source = runtime.open_agui_run(
    thread_id="thread-1",
    run_id="run-1",
    input=graph_input,
)

async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")
    body = await channel.open_sse(source, after=0)
    async with aclosing(body):
        async for frame in body:
            await send(frame)
```

`model`, `tools`, `graph_input` and the HTTP sender belong to your application.
Runtime supplies the namespace, run identity and codec. Messaging opens the Agent or
Sandbox only after choosing this source as producer; another request can attach to the
same run without repeating that work. Each source is single-use.

The caller closes the returned SSE body, including on early exit or failed HTTP setup.
Pass object streams to Messaging directly; the channel creates the durable SSE bytes.
Closing a subscription detaches that subscriber. The producer remains owned by
`Messaging` until it settles or Messaging closes.

## Core concepts

| Name | Purpose |
| --- | --- |
| Channel | A named stream family using one stable message codec |
| `RunIdentity.namespace` | Application-defined data isolation scope |
| `RunIdentity.thread_id` | Ordered message history within that namespace |
| `RunIdentity.run_id` | One semantic producer within the thread |
| `seq` | One-based message position within a thread generation |

Reuse the complete `RunIdentity` for retries, replay or attachment to the same run.
Use a new `run_id` for new input. The application owns authorization and request-body
idempotency; Messaging does not compare business request bodies.

`after=None` starts from the tail observed during preparation, `after=0` replays all
retained messages, and `after=N` returns messages with `seq > N`. Negative cursors and
cursors beyond the current tail raise `InvalidCursor`.

## Storage

| Backend | Scope |
| --- | --- |
| `MemoryBackend` | One process; the default |
| `SqlAlchemyBackend(engine)` | Shared SQLite, MySQL or PostgreSQL database |
| `RedisBackend(client)` | Shared Redis storage under a configurable `key_prefix` |

For SQLite, install `"tinkerfin-messaging[sqlalchemy]" aiosqlite`:

```python
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_messaging import Messaging, SqlAlchemyBackend

engine = create_async_engine("sqlite+aiosqlite:///messages.db")
try:
    async with Messaging(backend=SqlAlchemyBackend(engine)) as messaging:
        channel = messaging.channel(name="events", codec=codec)
finally:
    await engine.dispose()
```

Pass an existing Engine when your application already has one. SQL tables are prepared
automatically. The application owns the Engine or Redis client and closes it after
Messaging. SQL polling holds no connection between queries. Connection and statement
timeouts belong to the Engine; table setup has a 30-second lock wait. SQL writes share
one database-wide capacity budget and are serialized for admission. Workers sharing
that database must use equal backend settings. Unknown commit outcomes are reported
without automatically replaying the operation.

SQLite in-memory Engines need exclusive checkouts: use `AsyncAdaptedQueuePool` with
`pool_size=1, max_overflow=0`. Redis clients must return bytes (`decode_responses=False`).
See the [storage guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/backends-and-codecs.md)
for drivers, Redis configuration and custom backends.

## Channel operations

| Operation | Purpose |
| --- | --- |
| `wrap()` / `open_sse()` | Start or attach and return decoded messages or SSE bytes |
| `read()` | Read one finite ordered page |
| `follow()` | Replay and wait for the selected run to finish |
| `latest_seq()` / `validate_cursor()` | Inspect a thread cursor without creating a run |
| `get_run_status()` | Read authoritative producer status |
| `publish()` | Commit a message while the source permits external publication |
| `cancel()` | Request cancellation and wait for settlement |
| `delete_stream()` | Remove one thread generation when no live producer owns it |
| `wrap_recoverable()` | Resume a lost producer from a committed source checkpoint |

Statuses are `running`, `cancel_requested`, `completed`, `cancelled`, `failed` and
`owner_lost`. Stable message IDs make matching commits idempotent. External side effects
still require application-level idempotency.

## Capacity and retention

`MessagingLimits` defaults to 16 MiB per message, 1 MiB per checkpoint position,
100,000 messages and 1 GiB of payload per thread generation, and 1 GiB / 100,000 records
across one MemoryBackend instance, SQL database, or Redis prefix.

These are logical storage limits, including control records, rather than process memory
limits. `MessagingQuotaExceeded` identifies the exhausted resource; cancellation,
settlement and deletion remain available. See the [storage guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/backends-and-codecs.md)
for accounting and shared-worker configuration.

Retention is disabled by default. `MessagingRetentionPolicy.expire_after(seconds)`
sets the replay window after a thread becomes terminal. A new run before expiry
continues that history. Active or unexpired history is not evicted to admit new data.

An expired generation raises `StreamExpired`; starting with `after=0` can create its
replacement. Explicit deletion requires no live producer lease and records
`StreamDeleted` for old handles. Repeated deletion is safe. Replacement generations
start their sequence at one, without changing the errors seen by old handles.

## Custom sources and integrations

Custom sources supply an explicit codec and complete run identity:

```python
subscription = await messaging.channel(name="custom", codec=codec).wrap(
    source,
    identity=identity,
    after=0,
)
```

Use a renderer for custom SSE output. `RecoveryCheckpoint` and `RecoverableMessage`
carry an opaque source position and the message saved with it. `DeferredMessageSource`
opens an expensive custom source only for the selected producer. Runtime streams already
provide lazy opening, identity and cancellation.

Channel callbacks `on_source_ready` and `on_delivery_not_started` can activate or clean up
application delivery state; attachments invoke neither. `on_committed` observes new
commits without repeating notifications during replay. Callback failures cannot undo an
already committed message. `Messaging(settlement_timeout=...)` limits the caller's wait;
accepted work remains owned and can be awaited again with `aclose()`.

Other storage implementations import `MessagingBackend` from
`tinkerfin_messaging.backend_contract`. Verify them through
`tinkerfin_messaging.testing.verify_messaging_backend()` and their database-specific
concurrency, cancellation and commit-failure contracts.

## Documentation

- [Messaging basics](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/index.md)
- [Delivery and replay](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/delivery-and-replay.md)
- [Cancellation and recovery](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/cancellation-and-recovery.md)
- [Storage and codecs](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/backends-and-codecs.md)
- [API reference](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/api-reference.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE).
