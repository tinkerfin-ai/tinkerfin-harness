# TinkerFin Notifications

Publish resource changes to independent asynchronous listeners. Notifications are
advisory: consumers read authoritative state after a change, on reconnect, and at
an application-appropriate reconciliation interval.

## Installation

```bash
pip install tinkerfin-notifications
```

## Quick start

```python
from tinkerfin_notifications import Notification, NotificationScope, Notifications


async def example():
    async with Notifications() as notifications:
        scope = NotificationScope(namespace="project-a")
        async with notifications.subscribe(scope=scope) as changes:
            await notifications.publish(
                Notification(scope=scope, topic="documents.changed", key="document-42")
            )
            change = await anext(changes)
```

Enter a subscription before reading its initial snapshot. Delivered hints are
broadcast to matching listeners; repeated changes to a resource can be coalesced. A full
listener buffer produces `ResyncRequired`, requiring a fresh authoritative read.

Scopes are filters, not authorization. Hosts authenticate clients and choose the
namespaces and owners they may observe. Keep message content and credentials out
of notification details.

The service owns its backend and subscriptions. Backend resources explicitly
borrowed from the host remain host-owned. Closing a listener never cancels the
operation that changed the resource.

`await publish(...)` freezes the notification and accepts it into a bounded sending
queue. It does not wait for network delivery. Pending changes to the same resource
are combined; admission of a new resource fails with `NotificationCapacityExceeded`
when the queue is full. Configure `NotificationLimits.max_pending_publications`
when the default of 128 pending resources does not fit the application.

## Cross-process notifications

```bash
pip install 'tinkerfin-notifications[redis]'
```

```python
from tinkerfin_notifications.redis import RedisBackend

# The host owns redis_client and closes it after Notifications exits.
notifications = Notifications(
    backend=RedisBackend(redis_client, channel="my-application:changes")
)
```

Each process uses its own service with the same Redis channel. Startup waits for
subscription acknowledgement. Disconnects and resubscriptions produce
`ResyncRequired`; Pub/Sub cannot recover messages sent while disconnected.
The service owns bounded transport waits and reports failed delivery through its
diagnostics. Accepted hints can be lost without changing already-committed source
state. A failed publication requires matching local listeners to resynchronize;
remote listeners still rely on their own transport signals and repair reads.
Closing discards queued hints and waits for a send already in progress before
the host closes the borrowed client. Consumers must keep their authoritative repair reads.

## Documentation and license

- [TinkerFin documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/notifications/index.md)
- [Source repository](https://github.com/tinkerfin-ai/tinkerfin-harness)
- [Apache-2.0 license](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
