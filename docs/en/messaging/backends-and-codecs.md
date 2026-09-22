# Storage, codecs, and custom backends

[Cancellation, deferred sources, and recovery](cancellation-and-recovery.md) · [中文](../../cn/messaging/backends-and-codecs.md)

The default `MemoryBackend` is for one-process development. To share events, run state, and cancellation across processes, use `SqlAlchemyBackend` or `RedisBackend`, or integrate custom shared storage through the public contract below.

## Use SQLAlchemy

Install the SQL extra and your asynchronous driver:

```bash
pip install "tinkerfin-messaging[sqlalchemy]" aiosqlite
```

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

You can pass an existing Engine. The application owns its connection pool and shutdown;
Messaging prepares its tables automatically. Select `postgresql+asyncpg` with `asyncpg`,
`mysql+asyncmy` with `asyncmy`, or `sqlite+aiosqlite` with `aiosqlite`. The SQL extra
installs no database driver. SQLite in-memory Engines require exclusive checkouts:
use `AsyncAdaptedQueuePool` with `pool_size=1, max_overflow=0`.

`SqlAlchemyBackend` accepts `producer_lease_seconds=15`, `poll_interval_seconds=0.1`,
`limits=MessagingLimits()` and `retention_policy=MessagingRetentionPolicy()`.
All channels in the database share these settings and the total capacity budget.
Configure connection and statement timeouts on the Engine.

## Use Redis

```bash
pip install "tinkerfin-messaging[redis]"
```

```python
from redis.asyncio import Redis
from tinkerfin_messaging import (
    Messaging,
    MessagingLimits,
    MessagingRetentionPolicy,
    RedisBackend,
)


redis = Redis.from_url(
    "redis://localhost:6379/0",
    decode_responses=False,
)
backend = RedisBackend(
    redis,
    key_prefix="my-app:tinkerfin",
    producer_lease_seconds=15.0,
    generation_cleanup_retry_seconds=0.1,
    limits=MessagingLimits(),
    retention_policy=MessagingRetentionPolicy.expire_after(86_400),
)
messaging = Messaging(backend=backend)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `client` | required | Async Redis client returning bytes |
| `key_prefix` | `tinkerfin-messaging` | Prefix reserved for this application |
| `producer_lease_seconds` | `15.0` | Producer ownership duration on the Redis clock |
| `generation_cleanup_retry_seconds` | `0.1` | Delay before retrying cleanup ownership |
| `limits` | `MessagingLimits()` | Individual, thread, and total retained-storage limits |
| `retention_policy` | disabled | Terminal thread-generation replay window |

The caller owns the Redis client and closes it during application shutdown. Each
`RedisBackend` uses one shared connection while waiting for messages or cancellation;
the framework releases it when the last wait ends. Size the pool for that connection
and concurrent ordinary commands.

An enabled retention policy starts at terminal settlement. Active producers do not
expire, and a new Run before the deadline clears the timer. An expired generation raises
`StreamExpired`; an explicit `after=0` start creates the next empty generation. `delete_stream()` explicitly deletes history and reports `StreamDeleted` to old readers.

## Select a built-in codec explicitly

Install the matching codec extra:

```bash
pip install "tinkerfin-messaging[agui]"
# or: pip install "tinkerfin-messaging[native]"
```

```python
from tinkerfin_messaging import AgUiCodec


codec = AgUiCodec()
channel = messaging.channel(
    name="agent-events",
    codec=codec,
    renderer=codec,
)
```

| Installation | API | Use |
| --- | --- | --- |
| `[agui]` | `AgUiCodec` | AG-UI encoding, decoding, and SSE |
| `[native]` | `NativeStreamPartCodec` | Canonical Native replay encoding, decoding, and SSE |
| `[sqlalchemy]` | `SqlAlchemyBackend` | SQL message storage; install an async driver separately |
| `[redis]` | `RedisBackend` | Multi-process durable backend |

TinkerFin streams carry their codec and complete RunIdentity. When wrapping one,
a channel needs only its name; identity and codec do not need to be repeated.
Custom sources require an explicit codec and RunIdentity.

Workers sharing a channel must agree on all limits and retention; workers sharing a
Redis prefix must agree on total limits. All channels in that prefix use one Redis
Cluster hash slot.

Default limits are 16 MiB per encoded message, 1 MiB per checkpoint position, 100,000
messages and 1 GiB of payload per thread generation, and 1 GiB / 100,000 retained records
across one MemoryBackend instance, SQL database, or Redis prefix. Configure `max_total_bytes` and
`max_total_records` in `MessagingLimits` to change the totals.

Total bytes count payloads, per-message checkpoint evidence, and each Run's latest
checkpoint. Checkpoint size includes its position and UTF-8 message ID; replacing the
latest Run checkpoint charges only the difference. Each channel, thread, live
generation, Run, message, and tombstone counts as one record. These are logical limits,
not a measurement of Python or Redis allocation. Custom backends expose the same limits
through `messaging_settings` and reject quota overflow before mutation.

An idempotent message retry consumes no additional quota. `MessagingQuotaExceeded`
identifies the exhausted resource. Cancellation, settlement, and deletion remain
available when full. Cleanup releases messages and Runs and converts the generation
record to a tombstone. Channel, thread, and tombstone records remain charged. Retention is opt-in. New writes can reclaim expired history; active or unexpired history is not evicted.

## Define a custom message format

```python
import json


class JsonEventCodec:
    codec_id = "my-app.event"

    def encode(self, item: dict[str, object]) -> bytes:
        return json.dumps(item, separators=(",", ":")).encode()

    def decode(self, payload: bytes) -> dict[str, object]:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("event must be an object")
        return value
```

`codec_id` identifies the durable format. Do not reuse it for an incompatible payload after data exists.

Add an SSE renderer when needed:

```python
class JsonEventRenderer:
    def render(self, *, seq: int, payload: dict[str, object]) -> bytes:
        data = json.dumps(payload, separators=(",", ":"))
        return f"id: {seq}\nevent: custom\ndata: {data}\n\n".encode()
```

```python
channel = messaging.channel(
    name="custom-events",
    codec=JsonEventCodec(),
    renderer=JsonEventRenderer(),
)
```

## Define a custom source

`MessageSource` supports async iteration and idempotent close:

```python
class QueueSource:
    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is STOP:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        self.closed = True
```

Implement `ProfiledMessageSource` only when the source can declare a complete immutable codec profile. For most application sources, an explicit channel codec is simpler.

## Define a custom backend

Import the storage extension contract from its focused module:

```python
from tinkerfin_messaging.backend_contract import (
    MessagingBackend,
    MessagingStateSnapshot,
    MessagingStorageEffect,
    MessagingTransition,
    resolve_messaging_transition,
)
```

Implement `MessagingBackend` only when another durable store is required. Messaging owns
producer tasks, cancellation, follow loops, settlement, retention decisions, and error
conversion. A Backend author implements one immutable settings property and six storage
operations:

| Extension member | Backend responsibility |
| --- | --- |
| `messaging_settings` | Return immutable limits, retention, producer lease, renewal, and wait settings shared by cooperating workers |
| `prepare_messaging_storage()` | Idempotently create or validate the one current storage shape without taking ownership of the injected client |
| `commit_messaging_transition(transition)` | Atomically commit one framework-defined transition and return its exact durable result |
| `load_messaging_state(query)` | Return a storage-clock-consistent bounded snapshot for the requested channel, generation, run, and message evidence |
| `read_committed_messages(query)` | Return an ascending exact-generation page and change cursor; `run_state` is required in the same atomic view when `stop_at_run_terminal=True` |
| `wait_for_messaging_change(wait)` | Wait for a possible message or control change; spurious timeout returns are allowed and cancellation must release subscriptions or pinned connections |
| `purge_stream_generation(purge)` | Idempotently remove one bounded batch from a generation already sealed for cleanup, without removing shared control or its tombstone |

`MessagingTransition`, `MessagingStateSnapshot`, and `MessagingStorageEffect` are frozen
storage-neutral values. A transactional Backend loads the requested state while holding
its transaction, calls `resolve_messaging_transition()`, and atomically applies the
returned effect. It must retry only proven optimistic conflicts; cancellation and an
uncertain external commit cannot be converted into an unverified success.

`begin_generation_cleanup` may return an opaque, bounded-lifetime `cleanup_token`.
Messaging does not inspect or persist the token. It passes the value unchanged and
serially to `purge_stream_generation()` and `finish_generation_cleanup` for the exact
generation and authoritative cleanup reason returned by begin. A Backend that returns a
token must bound its external lease, tolerate an idempotent retry, and allow a different
cleanup attempt to take over after cancellation or process loss.

All database and network calls must be natively asynchronous. The host owns injected
clients, connection pools, and shutdown. `wait_for_messaging_change()` must propagate
`CancelledError` after releasing its own wait resource. `purge_stream_generation()` must
remain bounded. Different cleanup attempts may overlap, but their generation and token
fences must prevent either attempt from deleting another generation.

Run the public contract verifier against an empty isolated namespace:

```python
from contextlib import asynccontextmanager

from tinkerfin_messaging.testing import verify_messaging_backend


@asynccontextmanager
async def open_backend():
    backend = MyBackend(client, namespace="contract-test")
    try:
        yield backend
    finally:
        await delete_contract_test_namespace()


await verify_messaging_backend(open_backend)
```

The verifier covers supported Messaging behavior. A distributed implementation must
also test real contention, lease expiry, process loss, uncertain transport outcomes,
and database-specific cleanup recovery.

Next: [Messaging usage reference](api-reference.md).
