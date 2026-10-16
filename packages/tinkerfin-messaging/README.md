# tinkerfin-messaging

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

## Quick Start

This example streams a Runtime's output as durable SSE. Install the AG-UI and
OpenAI integrations, set `OPENAI_API_KEY`, and choose a model available to your account:

```bash
pip install "tinkerfin[agui]" "tinkerfin-messaging[agui]" langchain-openai
```

```python
import asyncio
from contextlib import aclosing

from tinkerfin import TinkerFin
from tinkerfin_messaging import Messaging


async def main() -> None:
    runtime = TinkerFin().with_namespace("customer-1").build(model="openai:gpt-5.4")
    source = runtime.open_agui_run(
        thread_id="thread-1",
        run_id="run-1",
        messages=[{"id": "message-1", "role": "user", "content": "Hello"}],
    )
    async with Messaging() as messaging:
        channel = messaging.channel(name="agent-events")
        body = await channel.open_sse(source, after=0)
        async with aclosing(body):
            async for frame in body:
                print(frame.decode("utf-8"), end="")


asyncio.run(main())
```

Runtime supplies identity and codec. Messaging opens the Agent or Sandbox only after
selecting this lazy, single-use source as producer. Another request can attach to the
same run without repeating that work. Pass object streams directly; Messaging creates
the durable SSE bytes. Your application supplies authentication and the HTTP response.

Close the returned body on early exit or failed HTTP setup. Closing a subscription
only detaches its reader. The producer remains owned by Messaging until it settles
or Messaging closes; request cancellation explicitly through the channel.

## Replay and command identity

A channel is a named stream family with one stable codec. A complete `RunIdentity`
contains the application's namespace, thread ID, and run ID. Reuse it to retry or
attach to the same run; use a new run ID for new input. Hosts authorize identities.

An optional lowercase SHA-256 `request_digest` binds complete command content while
the run record is retained. A changed binding, including a bound/unbound pair, raises
`RunRequestConflict`. Hosts calculate their own digest; use
[Gateway](https://github.com/tinkerfin-ai/tinkerfin-harness/tree/main/packages/tinkerfin-gateway)
for Runtime commands with automatic content binding. External side effects still
require application-level idempotency.

`after=None` starts at the tail observed during preparation, `after=0` replays all
retained messages, and `after=N` returns positions greater than `N`. Negative cursors
and cursors beyond the current tail raise `InvalidCursor`. Use `channel.read()` for
a finite page or `channel.follow()` to replay and wait for a selected run to finish.

## Storage and resource ownership

| Backend | Scope |
| --- | --- |
| `MemoryBackend` | One process; the default |
| `SqlAlchemyBackend(engine)` | Shared SQLite, MySQL or PostgreSQL database |
| `RedisBackend(client)` | Shared Redis storage under a configurable `key_prefix` |

Pass the backend to `Messaging(backend=...)`. SQL tables are prepared automatically.
For SQLite, install `"tinkerfin-messaging[sqlalchemy]" aiosqlite`; PostgreSQL uses
`asyncpg` and MySQL uses `asyncmy`. The application owns its `AsyncEngine` or Redis
client and closes it after Messaging. Configure connection and statement timeouts
on the Engine. Workers sharing SQL storage must use equal backend settings.
Unknown commit outcomes are reported without replaying the operation.

In-memory SQLite requires `AsyncAdaptedQueuePool` with `pool_size=1, max_overflow=0`.
Redis clients must return bytes (`decode_responses=False`). Custom storage, codecs,
and recoverable sources are covered in the storage and recovery guides below.

## Capacity, retention, and shutdown

Storage is bounded by `MessagingLimits`. Limits count logical payload and control
records, not process memory. `MessagingQuotaExceeded` identifies an exhausted
resource; cancellation, settlement, and deletion remain available.

Retention is disabled by default. `MessagingRetentionPolicy.expire_after(seconds)`
sets the replay window after a thread becomes terminal. A new run before expiry
continues that history. Active or unexpired history is not evicted to admit new data.
An expired generation raises `StreamExpired`; starting with `after=0` can create its
replacement. Explicit deletion requires no live producer and causes old handles to
raise `StreamDeleted`. Replacement generations restart sequence numbering at one.

`Messaging(settlement_timeout=...)` limits a caller's wait for shutdown. A timeout
does not release ownership of accepted work: await `aclose()` again before closing
borrowed resources. Async delivery callbacks must bound their I/O. Callback failure
cannot undo an already committed message; replay does not repeat commit callbacks.

## Documentation

- [Messaging basics](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/index.md)
- [Delivery and replay](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/delivery-and-replay.md)
- [Cancellation and recovery](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/cancellation-and-recovery.md)
- [Storage and codecs](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/backends-and-codecs.md)
- [API reference](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/messaging/api-reference.md)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
