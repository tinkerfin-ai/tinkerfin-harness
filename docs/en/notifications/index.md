# Notify listeners about resource changes

[Documentation](../index.md) · [中文](../../cn/notifications/index.md)

`tinkerfin-notifications` broadcasts small resource-change hints to independent
async listeners. Publishers and listeners share this package's contract without
knowing about each other. It has no Runtime, database, or HTTP dependency.

## Publish and subscribe

```bash
pip install tinkerfin-notifications
```

```python
from tinkerfin_notifications import Notification, Notifications, NotificationScope


async def example():
    async with Notifications() as notifications:
        scope = NotificationScope("project", owner_id="account-42")
        async with notifications.subscribe(scope=scope) as changes:
            # Commit the resource before publishing this hint.
            await notifications.publish(
                Notification(scope=scope, topic="documents.changed", key="report")
            )
            change = await anext(changes)
            return change
```

Enter the subscription before reading your initial resource snapshot. After a
matching change, read the resource again. If another hint arrives while that read
is in progress, mark the resource dirty and read again afterward.

`await publish(...)` copies and admits a hint to a bounded sending queue. Its return
does not confirm transport delivery. Pending hints with the same scope, topic, and
key can be combined. A new key beyond queue capacity raises
`NotificationCapacityExceeded`; this must not undo an already committed resource.

Subscriptions are independent and bounded. Overflow or a connection interruption
produces `ResyncRequired`, which requires a new authoritative snapshot. Keep a
periodic repair read because commits and notifications are separate operations,
and Redis Pub/Sub cannot replay hints missed during disconnection.

## Choose a scope and payload

| Input | Meaning |
| --- | --- |
| `NotificationScope(namespace)` | Subscribe to all owners in that namespace |
| `NotificationScope(namespace, owner_id)` | Subscribe only to that exact owner; ownerless hints do not match |
| `subscribe(scope=None)` | Trusted process-wide observation across namespaces |
| `topics={...}` | Select source-owned event topics |
| `key=...` | Select one resource |

Scopes filter delivery; the host authorizes access. Put only identifiers and small
routing facts in `details`. Keep message content, credentials, and signed URLs in
the authoritative service. Each consumer receives its own decoded value.

`NotificationLimits` controls concurrent subscriptions, pending resources per
subscription, encoded size, and pending publications. Defaults are 1024, 128,
4096 bytes, and 128 respectively.

## Broadcast across application workers

```bash
pip install "tinkerfin-notifications[redis]"
```

```python
from redis.asyncio import Redis
from tinkerfin_notifications import Notifications
from tinkerfin_notifications.redis import RedisBackend


async def serve(redis_url, serve_application):
    async with Redis.from_url(redis_url) as redis_client:
        async with Notifications(
            backend=RedisBackend(redis_client, channel="application:changes")
        ) as notifications:
            await serve_application(notifications)
```

Use the same Redis channel in every participating process. Each process owns its
Notifications lifecycle; the backend borrows the Redis client. Start Notifications
before its publishers and close it after them. Closing discards unsent hints and
settles the send already in progress before releasing its resources. It does not
close a borrowed Redis client. Redis failures remain visible through resync or
service errors; they do not switch delivery to process-local memory.

## Use framework publishers

Pass the same started `notifications` object to `InMemoryTraceStore`,
`SqlAlchemyTraceStore`, `MemoryAutomationStore`, or `SqlAlchemyAutomationStore`.
The stores publish after committing and releasing their mutation locks. Their
followers and workers use hints together with lease deadlines and repair reads.

| Topic | Scope | Resource key |
| --- | --- | --- |
| `trace.changed` | Runtime namespace | Thread ID; details include generation and, when available, run ID and sequence |
| `automation.task.changed` | Scheduling namespace and owner | Task ID |
| `automation.execution.changed` | Scheduling namespace and owner | Execution ID; details identify its task and Runtime identity |

Pure lease renewals do not publish resource changes. Scheduling and Runtime
namespaces can differ; derive authorization from the appropriate owner binding.

For an authorized browser SSE feed, use [Gateway](../gateway/index.md).
