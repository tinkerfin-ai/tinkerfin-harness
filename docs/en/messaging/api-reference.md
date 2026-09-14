# Messaging usage reference

[Messaging basics](index.md) · [中文](../../cn/messaging/api-reference.md)

## Application entry points

| API | Parameters | Purpose |
| --- | --- | --- |
| `Messaging(...)` | `backend=None`, `settlement_timeout=None` | Create one application lifecycle |
| `Messaging.channel(...)` | name, optional codec and renderer | Create a reusable channel |
| `Messaging.agui_channel(name=...)` | channel name | Create an AG-UI channel with transforms, main-run notifications, and replay |
| `Messaging.aclose()` | none | Settle preflight, producers, and cleanup |

The default backend is `MemoryBackend`.

## Channel methods

| Method | Main parameters | Result |
| --- | --- | --- |
| `open_sse(...)` | source, optional identity, after, callbacks | Caller-owned closeable SSE byte iterator |
| `publish(...)` | message, identity, optional message_id | Persist a notification in the existing run; return MessageEnvelope |
| `wrap(...)` | source, optional identity, after, callbacks | `MessageSubscription` |
| `wrap_recoverable(...)` | recoverable source, optional identity, after, callbacks | Recoverable subscription |
| `read(...)` | RunIdentity, `after=0`, `limit=100` | Ascending finite page |
| `follow(...)` | RunIdentity, `after=0` | Follow a run to its terminal state |
| `get_run_status(...)` | RunIdentity | Current authoritative run status |
| `latest_seq(...)` | RunIdentity | Current thread tail |
| `validate_cursor(...)` | RunIdentity, after | Read-only cursor validation |
| `cancel(...)` | RunIdentity | Request cancellation and wait for settlement |
| `delete_stream(...)` | RunIdentity | Delete the current thread generation |

RunIdentity is optional only when the source advertises an immutable profile.

## Subscription and Envelope

| API | Purpose |
| --- | --- |
| `MessageSubscription` | Returned by channel `wrap()`/`follow()`; asynchronously iterates `DecodedMessage` and supports `to_sse()`/`aclose()`; do not construct directly |
| `DecodedMessage` | Committed `envelope` plus codec-decoded `data` |
| `MessageEnvelope` | Immutable committed durable message |

### `MessageEnvelope` fields

| Envelope field | Meaning |
| --- | --- |
| `channel` | Non-empty channel name |
| `identity` | Nested shared RunIdentity |
| `seq` | One-based thread position |
| `message_id` | Stable message idempotency ID |
| `codec` | Persisted format ID |
| `payload` | Encoded bytes |
| `created_at` | Aware UTC timestamp |

## Common helpers

| API | Purpose |
| --- | --- |
| `parse_sse_event_id(value)` | Parse `None` or canonical non-negative ASCII decimal SSE IDs |
| `is_active_run_status(status)` | TypeGuard for `running` and `cancel_requested` |
| `is_final_run_status(status)` | TypeGuard for all terminal durable statuses |
| `is_failed_run_status(status)` | TypeGuard for `failed` and `owner_lost` |

Status helpers perform no backend I/O. For AG-UI use `messaging.agui_channel(name=...)`.
Its `open_sse()` accepts `transform_event` for business fields while preserving event
types and run, message, tool, and approval identities. `on_run_started` and
`on_run_finished` receive committed main-run events; child runs and replay do not
repeat those business notifications.

With an existing Runtime and an open Messaging lifecycle:

```python
from ag_ui.core import BaseEvent


def add_label(event: BaseEvent) -> BaseEvent:
    return event.model_copy(update={"label": "Report"})


channel = messaging.agui_channel(name="conversations")
body = await channel.open_sse(
    runtime.open_agui_run(
        thread_id="conversation-1",
        run_id="run-1",
        messages=[
            {"id": "message-1", "role": "user", "content": "Summarize this report"}
        ],
    ),
    after=0,
    transform_event=add_label,
)
```

The HTTP host sends `body` and calls `await body.aclose()` on completion or disconnect.
Closing the reader leaves the producer running. `open_sse()` owns source preparation
and failed-start cleanup; the application retains Messaging and borrowed databases.

For an existing run, call `await channel.follow_sse(identity=identity, last_event_id=last_id)`
without constructing another source. Omitting the cursor replays that run from its
beginning. A supplied cursor must belong to the same run and its already applied view.
`follow()` returns a closeable decoded subscription. The channel also supports
`publish()`, `get_run_status()`, `cancel()`, and `delete_stream()`.
Combine history and delivery with [AgUiHistory.open_live](../runtime/api-reference.md#recorded-ag-ui-conversations).

In replay SSE, events already committed when the subscription binds carry `event: replay`;
later events use the default `message` name. IDs and AG-UI data are unchanged.
Browser `EventSource` consumers must listen for both `replay` and `message`.

## Advanced source helpers

| API | Purpose |
| --- | --- |
| `MessageSource` | Single-use asynchronous source protocol |
| `CancellableMessageSource` | Source-owned cancellation callback |
| `ProfiledMessageSource` | Codec, RunIdentity, live type, and replay type profile |
| `MessageCodecInputSource` | Optional source-owned conversion from one live item to the inferred codec's finite input |
| `MessageSourceBinding` | Opened source and optional cancellation callback |
| `DeferredMessageSource` | Open an ordinary source only for the owner; optional owner preflight runs before producer execution |
| `ProfiledDeferredMessageSource` | Deferred source whose profile is known before open, with the same owner-preflight fence |
| `FiniteMessageSource` | Adapt a finite iterable |
| `map_source(...)` | Ordered synchronous or asynchronous transform |
| `RecoverableSource` | Rebuild from a checkpoint |
| `RecoverableMessage` | Stable ID, data, and checkpoint |
| `RecoveryCheckpoint` | Opaque position and optional last message ID |

## Codecs and backends

| API | Purpose |
| --- | --- |
| `MessagePublicationPolicy` | Optional codec validation and main-run publication boundaries |
| `MessageCodec` | Stable `codec_id` plus `encode()` and `decode()` |
| `SseRenderer` | `render(seq=..., payload=...) -> bytes` |
| `AgUiCodec` | `[agui]` AG-UI codec and renderer |
| `NativeStreamPartCodec` | `[native]` canonical Native replay codec and renderer |
| `NativeStreamPart` | `[native]` finite canonical Native replay value |
| `MemoryBackend` | In-process implementation |
| `SqlAlchemyBackend(engine)` | `[sqlalchemy]` SQLite, MySQL and PostgreSQL; borrowed AsyncEngine |
| `RedisBackend` | `[redis]` multi-process implementation |
| `MessagingRetentionPolicy` | Disabled or positive terminal replay deadline |

Backend-author contracts are exported from `tinkerfin_messaging.backend_contract`:

| API | Purpose |
| --- | --- |
| `MessagingBackend` | Six-operation custom storage protocol |
| `MessagingBackendSettings` | Immutable limits, retention, lease, renewal, and wait settings |
| `MessagingTransition` | Framework-defined atomic lifecycle intent |
| `MessagingStateSnapshot` | Bounded storage-clock-consistent transition evidence |
| `MessagingStorageEffect` | Complete replacements produced by `resolve_messaging_transition()` |

### Backend operations

| Operation | Contract |
| --- | --- |
| `messaging_settings` | Return immutable settings shared by cooperating Backend instances |
| `prepare_messaging_storage()` | Idempotently create or validate the current storage shape |
| `commit_messaging_transition(...)` | Atomically commit a framework transition |
| `load_messaging_state(...)` | Load one bounded consistent state snapshot |
| `read_committed_messages(...)` | Read one ascending exact-generation message page |
| `wait_for_messaging_change(...)` | Wait cancellation-responsively for a possible message or control change |
| `purge_stream_generation(...)` | Idempotently remove one bounded batch from a sealed generation |

`resolve_messaging_transition()` contains the reusable storage-neutral state machine.
`tinkerfin_messaging.testing.verify_messaging_backend()` verifies supported behavior
against an isolated Backend factory.

Cleanup begin can return an opaque, expiring `cleanup_token`. Messaging passes it
unchanged and serially to bounded purge and finish calls for the returned generation;
it never interprets or persists the token.

## Callbacks

| API | Purpose |
| --- | --- |
| `CancelCallback` | Zero arguments or one `CancelContext`; may return a finite tail |
| `CancelContext` | Immutable channel and RunIdentity |
| `CommittedCallback` | Receives owner commits after append |
| `on_source_ready` | Owner-only async callback after source readiness and before producer creation |
| `on_delivery_not_started` | Async cleanup when readiness and attachment were both absent |

Attachments invoke neither delivery callback. `on_owner_preflight` belongs to the source
and runs before a deferred opener; `on_source_ready` runs after that opener completes.

Cancellation waits for a delivery callback that has already started to finish.
Use database and network timeouts inside these callbacks to bound that wait.

## Common errors

| Error | Meaning |
| --- | --- |
| `MessagingNotStarted` / `MessagingClosed` | Lifecycle does not allow the operation |
| `MessagingSettlementTimeout` | Caller wait for protected cleanup expired |
| `InvalidCursor` | Cursor is invalid or beyond the tail |
| `PublicationRejected` | Run or protocol does not accept a new external message |
| `CodecMismatch` | Channel codec differs |
| `SourceProfileMismatch` | Source profile is incomplete or contradictory |
| `MessageIdConflict` | One message ID maps to different content |
| `RunAlreadyActive` | Another run owns the thread |
| `RunNotFound` | Run does not exist |
| `RunProducerFailed` | Production, encoding, commit, or cancellation failed |
| `CancellationUnsupported` | No cancellation callback exists |
| `BackendOwnershipLost` | A stale producer lost ownership |
| `StreamExpired` | Terminal generation is outside its replay retention window |
| `StreamDeleted` / `StreamDeleteConflict` | Generation is deleted or still active |

Messaging does not read or compare request bodies. Callers own body consistency for a reused run_id.
