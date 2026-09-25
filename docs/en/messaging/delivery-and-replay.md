# Delivery, replay, and SSE

[Messaging basics](index.md) · [中文](../../cn/messaging/delivery-and-replay.md)

## Start or attach

```python
subscription = await channel.wrap(
    source,
    identity=identity,  # omit for a profiled TinkerFin source
    after=0,
)
```

Before returning, `wrap()` atomically decides whether this caller owns a new producer or attaches an existing run. A different active run in the same thread raises `RunAlreadyActive`; a different persisted format raises `CodecMismatch`.

`run_id` identifies an idempotent run within its namespace and thread. Messaging neither reads nor stores a business request digest.

## Return SSE directly

```python
from starlette.responses import StreamingResponse
from tinkerfin_messaging import parse_sse_event_id


body = await channel.open_sse(
    source,
    identity=identity,
    after=lambda: parse_sse_event_id(request.headers.get("Last-Event-ID")),
)
return StreamingResponse(body, media_type="text/event-stream")
```

The resolver runs once before durable preparation. `parse_sse_event_id()` accepts only
canonical non-negative ASCII decimal values. Committed sequence numbers become SSE IDs.

Optional `on_source_ready` runs once for a new owner after the request-owned source is ready and
before producer creation. `on_delivery_not_started` runs only when readiness was not
reached and no attachment was established. Attachments invoke neither callback. The
returned body is caller-owned and must be closed when it will not be consumed.

Use `on_subscribed` for async work needed by every successful subscription, including
attachments. Messaging awaits it before returning the body. Failure or cancellation
closes that reader without cancelling the durable producer; callback and cleanup
failures remain in the exception chain. Accepted callbacks settle before cancellation
returns, so bound their I/O with resource timeouts.

The same byte body can be sent with `EventSourceResponse(body)`. See
[Streams and SSE](../runtime/streams-and-sse.md) for HTTP cleanup requirements.

When a valid attachment is established, Messaging closes the unused single-use
candidate source without opening it. The candidate cannot be reused.

## Publish while a run is active

```python
from ag_ui.core import CustomEvent

await channel.publish(
    CustomEvent(name="report.progress", value={"completed": 3, "total": 10}),
    identity=identity,
    message_id="report-progress-3",
)
```

The message enters the same durable log as source events. Existing subscribers receive it, and reconnecting subscribers replay it using the same sequence cursor. The host authorizes the target identity.

The codec must be bound, including on a separate worker. For AG-UI, use `messaging.agui_channel(name=...)` on every worker. Ordinary codecs accept publication after the first source commit. AG-UI accepts only `CustomEvent` after the target main `RUN_STARTED`; the main terminal atomically closes publication. Child run events do not open or close the main run. Cancellation, settlement, or producer ownership loss reject new publications with `PublicationRejected`.

Omitting `message_id` generates a fresh key per call. Repeating the same key and content returns the retained envelope, including after completion; different content raises `MessageIdConflict`. A rejected publication never starts a new run.

Codecs with protocol lifecycles can implement `MessagePublicationPolicy`: `validate_publication()` checks external values, while `starts_publication()` and `ends_publication()` identify the target run boundaries. These decisions are committed atomically with source messages; storage backends remain protocol-neutral.

## Read committed data

```python
latest = await channel.latest_seq(identity=identity)
page = await channel.read(identity=identity, after=100, limit=200)
subscription = await channel.follow(identity=identity, after=100)
status = await channel.get_run_status(identity=identity)
```

| API | Scope | Result |
| --- | --- | --- |
| `latest_seq()` | whole thread | Current maximum seq, or 0 |
| `read()` | whole thread | Finite ascending page |
| `follow()` | selected run | Replay, then wait for its terminal state |
| `get_run_status()` | selected run | Current durable run status without ownership |

Thread-level methods select the complete `(namespace, thread_id)` and accept a `RunIdentity`. Run-level operations also select `run_id`.

`get_run_status()` can atomically archive an expired producer lease as `owner_lost`.
It raises `RunNotFound` when no durable record exists and never creates or recovers a
producer.

## Envelope

| Field | Purpose |
| --- | --- |
| `channel` | Codec namespace |
| `identity` | Complete namespace, thread_id and run_id |
| `seq` | Thread-level committed position |
| `message_id` | Stable message idempotency ID |
| `codec` | Persisted format ID |
| `payload` | Encoded bytes |
| `created_at` | UTC time allocated on first commit |


## Observe owner commits

```python
async def on_committed(envelope) -> None:
    await projection_queue.put(envelope)


subscription = await channel.wrap(
    source,
    identity=identity,
    on_committed=on_committed,
)
```

Attachments do not re-notify old commits. Observer failures are logged without changing an already committed run outcome.

## Delete a thread log

```python
await channel.delete_stream(identity=identity)
```

Deletion covers the complete `(namespace, thread_id)`. A live producer lease raises `StreamDeleteConflict`; an expired lease permits deletion. A missing stream is an idempotent success. Recreating the thread allocates a new generation; old handles retain their deletion error.

Next: [Cancellation, deferred sources, and recovery](cancellation-and-recovery.md).
