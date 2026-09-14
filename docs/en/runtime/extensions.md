# Runtime extensions

[Streams and SSE](streams-and-sse.md) · [中文](../../cn/runtime/extensions.md)

## Coordinate matching runs

Use the in-memory coordinator when matching thread identities must not execute at the
same time in one process:

```python
from tinkerfin import TinkerFin
from tinkerfin.coordination import InMemoryRunCoordinator

coordinator = InMemoryRunCoordinator(
    key_resolver=lambda identity: identity.thread_id,
)
runtime = (
    TinkerFin(run_coordinator=coordinator)
    .with_namespace("company-a")
    .build(model=model, tools=tools)
)
```

The default key contains the full namespaced thread identity. A custom `key_resolver`
may deliberately choose another coordination scope. Coordination controls concurrent
execution; it does not persist messages or checkpoints.

Install `tinkerfin[redis]` and use `tinkerfin.redis.RedisRunCoordinator` when workers
must share coordination. The application owns a supplied Redis client.

## Hold a Redis resource lease

`tinkerfin.redis.RedisLeaseLock` provides renewable leases for application resources:

```python
from tinkerfin.redis import RedisLeaseLock

lock = RedisLeaseLock.from_client(redis, key_prefix="my-app:locks")

async with lock:
    async with lock.hold("invoice-42") as lease:
        await update_invoice(fencing_token=lease.fencing_token)
```

Lease loss cancels the owning task. Use the fencing token when a downstream store must
reject a stale holder.

## Observe runs

Attach a full Runtime observer or one terminal callback before building:

```python
runtime = TinkerFin().with_namespace(namespace).with_observer(tracer).build(model=model)
```

```python
runtime = (
    TinkerFin()
    .with_namespace(namespace)
    .with_observer(on_terminal=record_terminal)
    .build(model=model)
)
```

Observers are asynchronous and awaited. Their failures remain visible to the caller.
Terminal callbacks run in this process and are not durable notifications.

## Select an integration profile

Runtime profiles and native stream drivers are advanced extension contracts:

```python
from tinkerfin import TinkerFin
from tinkerfin.runtime_profile import DeepAgentsV3RuntimeProfile

builder = TinkerFin(runtime_profile=DeepAgentsV3RuntimeProfile())
```

Profile contracts are exported from `tinkerfin.runtime_profile`; driver contracts are
exported from `tinkerfin.native_driver`. The v3 profile uses LangGraph's experimental
event stream.

Next: [Runtime API](api-reference.md).
