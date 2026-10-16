"""Observable delivery, isolation, and ownership of resource-change hints."""

from __future__ import annotations

import asyncio

import pytest

from tinkerfin_notifications import (
    MemoryBackend,
    Notification,
    NotificationError,
    NotificationLimits,
    Notifications,
    NotificationsClosed,
    NotificationScope,
    NotificationsNotStarted,
    ResyncRequired,
)
from tinkerfin_notifications.backend import NotificationReceiver


def change(key: str = "one", *, owner: str | None = None) -> Notification:
    return Notification(
        scope=NotificationScope("project", owner),
        topic="documents.changed",
        key=key,
    )


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (None, ["public", "alice", "bob", "elsewhere"]),
        (NotificationScope("project"), ["public", "alice", "bob"]),
        (NotificationScope("project", "alice"), ["alice"]),
        (NotificationScope("other"), ["elsewhere"]),
    ],
)
async def test_scopes_filter_without_inferring_authorization(
    scope: NotificationScope | None, expected: list[str]
) -> None:
    async with Notifications() as notifications:
        async with notifications.subscribe(scope=scope) as subscription:
            for key, owner in (("public", None), ("alice", "alice"), ("bob", "bob")):
                await notifications.publish(change(key, owner=owner))
            await notifications.publish(
                Notification(
                    scope=NotificationScope("other"),
                    topic="documents.changed",
                    key="elsewhere",
                )
            )
            received = [await anext(subscription) for _ in expected]
            assert [
                item.key for item in received if isinstance(item, Notification)
            ] == expected


async def test_latest_resource_hint_is_coalesced_and_overflow_requires_resync() -> None:
    async with Notifications(
        limits=NotificationLimits(max_pending_per_subscription=1)
    ) as notifications:
        async with notifications.subscribe() as subscription:
            await notifications.publish(
                change().model_copy(update={"details": {"revision": 1}})
            )
            await notifications.publish(
                change().model_copy(update={"details": {"revision": 2}})
            )
            received = await anext(subscription)
            assert isinstance(received, Notification)
            assert received.details == {"revision": 2}
            await notifications.publish(change("old"))
            await notifications.publish(change("new"))
            assert await anext(subscription) == ResyncRequired("overflow")
            assert await anext(subscription) == change("new")
            await notifications.publish(change("during-refresh"))
            assert await anext(subscription) == change("during-refresh")


async def test_cancelled_pull_preserves_cancellation_and_context_ownership() -> None:
    async with Notifications() as notifications:
        async with notifications.subscribe() as subscription:
            entered = asyncio.Event()

            async def read() -> Notification | ResyncRequired:
                entered.set()
                return await anext(subscription)

            reader = asyncio.create_task(read())
            await entered.wait()
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader
            await notifications.publish(change())
            assert await anext(subscription) == change()


async def test_service_is_explicit_single_use_and_releases_all_listeners() -> None:
    notifications = Notifications()
    with pytest.raises(NotificationsNotStarted):
        await notifications.publish(change())
    async with notifications:
        subscription = notifications.subscribe()
        await subscription.__aenter__()
    with pytest.raises(StopAsyncIteration):
        await anext(subscription)
    with pytest.raises(NotificationsClosed):
        await notifications.publish(change())
    with pytest.raises(NotificationsClosed):
        async with notifications:
            pytest.fail("service cannot be entered again")
    await notifications.aclose()


async def test_failed_start_closes_adopted_backend_and_preserves_primary_error() -> (
    None
):
    closed = asyncio.Event()
    failure = NotificationError("cannot start")

    class FailedBackend(MemoryBackend):
        async def start(self, receive_notification: NotificationReceiver) -> None:
            await super().start(receive_notification)
            raise failure

        async def aclose(self) -> None:
            await super().aclose()
            closed.set()

    with pytest.raises(NotificationError) as caught:
        async with Notifications(backend=FailedBackend()):
            pytest.fail("failed backend must not be exposed")
    assert caught.value is failure
    assert closed.is_set()
