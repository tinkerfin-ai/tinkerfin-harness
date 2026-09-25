# Cancellation, deferred sources, and recovery

[Delivery, replay, and SSE](delivery-and-replay.md) · [中文](../../cn/messaging/cancellation-and-recovery.md)

This guide covers remote cancellation, creating an expensive source only for the producer owner, and rebuilding a source after owner loss.

## Remote cancellation

Register a callback when starting the producer:

```python
async def cancel_agent(context):
    running_task.cancel()
    return cancellation_tail


body = await channel.open_sse(
    source,
    identity=identity,
    cancel=cancel_agent,
)
```

Cancel from another request:

```python
cancelled = await channel.cancel(identity=identity)
```

The callback may take no arguments or one `CancelContext`. It may return a finite terminal tail for subscribers.

TinkerFin AG-UI streams already declare cancellation. Do not also pass `cancel=` when the source owns that callback.

Messaging rejects new cancellation requests after closing starts. Closing first
signals current producers, then waits for accepted cancellation, settlement and
resource cleanup to finish.

| Result or error | Meaning |
| --- | --- |
| `True` | Cancellation was requested from the active producer |
| `False` | The run had already settled |
| `RunNotFound` | No such run exists |
| `CancellationUnsupported` | The run has no cancellation callback |
| `RunProducerFailed` | Producer or cancellation work failed |

## Create the agent only for the owner

Pass the lazy Runtime stream directly to Messaging:

```python
source = runtime.open_agui_run(
    thread_id="thread-42",
    run_id="run-7",
    input=graph_input,
)
```

Messaging opens the Agent only after selecting this source as producer. Attachments
reuse existing state without repeating Agent or Sandbox preparation.

Use channel callbacks for host delivery state:

```python
body = await channel.open_sse(
    source,
    on_source_ready=activate_business_run,
    on_subscribed=refresh_delivery_state,
    on_delivery_not_started=cleanup_business_run,
)
```

`on_subscribed` runs for each successful subscription, including attachments, before
the response body is returned. Failure closes that reader without cancelling the run.

Source-owned `on_owner_preflight` is a separate advanced hook for preparing the source
itself. It is not a second name for host activation.

## Advanced deferred sources

Use `DeferredMessageSource` when a custom protocol source is expensive:

```python
from tinkerfin_messaging import (
    DeferredMessageSource,
    MessageSourceBinding,
    ProfiledDeferredMessageSource,
)


async def open_events():
    events = await connect_event_source()
    return MessageSourceBinding(source=events, cancel=events.cancel)


source = DeferredMessageSource(
    open_events,
    cancellable=True,
    cancel_after_first_item=True,
)
```

`connect_event_source()` is application code returning an async event source with
idempotent `aclose()` and a cancellation callback. The callback is passed explicitly.

| Parameter | Purpose |
| --- | --- |
| `opener` | Asynchronously creates the source and returns `MessageSourceBinding` |
| `cancellable` | Declares whether the opened source supports cancellation |
| `cancel_after_first_item` | Prevents cancellation from overtaking the first protocol event |
| `on_owner_preflight` | Optional async owner-only source preparation before opener execution |

Attachments and replay-only requests close the deferred wrapper without opening the real source. `cancel_after_first_item=True` is useful for protocols that must emit `RUN_STARTED` first.

Messaging renews acquired ownership throughout source preparation, ready callbacks,
source streaming, and settlement. Factories do not manage leases. Cancelling preparation
interrupts pending work and joins owned cleanup; lease loss prevents further source commits.

Messaging settles `on_owner_preflight` after durable owner selection. A failure releases
that prepared owner and closes the deferred wrapper before its opener runs. Use it only
for source-owned preparation; host activation belongs in `on_source_ready` after the
opener succeeds.

`MessageSourceBinding` holds the source and an optional cancel callback. Leave the callback empty when the source already declares its own.

Use `ProfiledDeferredMessageSource` when a custom opener returns a known AG-UI or Native
source and a name-only channel must know the codec and RunIdentity before opening it:

```python
from ag_ui.core import BaseEvent


source = ProfiledDeferredMessageSource(
    open_events,
    identity=identity,
    codec_profile="agui.event",
    source_type=BaseEvent,
    replay_type=BaseEvent,
    cancellable=True,
    cancel_after_first_item=True,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `opener` | required | Asynchronously returns `MessageSourceBinding` |
| `identity` | required | Complete run identity available before open |
| `codec_profile` | required | Stable profile ID matching a built-in codec |
| `source_type` | required | Live value type returned by the opener |
| `replay_type` | required | Value type decoded by the codec |
| `cancellable` | required | Whether the source supports remote cancellation |
| `cancel_after_first_item` | `False` | Prevent cancellation from overtaking the first protocol event |
| `on_owner_preflight` | `None` | Source preparation after owner selection and before opener execution |

## Fixed and transformed sources

```python
from tinkerfin_messaging import FiniteMessageSource, map_source


finite = FiniteMessageSource.from_events((started, finished))


async def enrich(event):
    return add_request_metadata(event)


mapped = map_source(source, enrich)
```

`map_source()` accepts a synchronous or asynchronous transform and preserves order, backpressure, cancellation tails, and close behavior.

If cancellation interrupts a close call, await `aclose()` again before releasing borrowed resources.

A subscription allows one active pull. Calling `subscription.aclose()` cancels and
settles that pull before closing its decoder and backend iterator; a consumer waiting
for the next message receives `CancelledError`. The producer continues independently,
and a later subscription can replay any committed messages.

A transform may change the data type, so the mapped source no longer claims the original built-in codec profile. Configure the channel codec explicitly.

## Recover after producer owner loss

Implement `RecoverableSource` and use `wrap_recoverable()` when a source can restart from a stable position:

```python
subscription = await channel.wrap_recoverable(
    recoverable_source,
    identity=identity,
    after=0,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `source` | required | Recoverable source implementing `open(checkpoint)` |
| `identity` | optional for a profiled source | Custom-source identity or equality check for the advertised RunIdentity |
| `after` | `None` | Exclusive replay cursor; `None` starts at the tail captured during prepare |
| `cancel` | `None` | Stops the source after accepted remote cancellation and may return a finite stable-ID tail |
| `on_committed` | `None` | Async observer after each successful owner commit |

Each item opened by the source is a `RecoverableMessage`:

```python
RecoverableMessage(
    message_id="source-event-17",
    data=event,
    checkpoint=RecoveryCheckpoint(
        position=b"17",
        last_message_id="source-event-17",
    ),
)
```

| Field | Purpose |
| --- | --- |
| `message_id` | Stable ID used for idempotent commits |
| `data` | Value to encode and persist |
| `checkpoint.position` | Opaque restart position understood by the source |
| `checkpoint.last_message_id` | Must match the current message ID |

Stable message IDs make commits idempotent. They do not make model calls, tool calls, or database writes exactly once; external effects still need business idempotency or an outbox.

## Close settlement timeout

`Messaging(settlement_timeout=...)` limits how long the caller waits, not the protected cleanup itself. Timeout raises `MessagingSettlementTimeout` while accepted commits, cancellation tails, source closure, and backend settlement continue. A later `aclose()` waits for that same task.

Next: [Redis, custom codecs, and backends](backends-and-codecs.md).
