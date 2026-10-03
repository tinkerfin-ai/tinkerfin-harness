"""Observable delivery, isolation, and ownership of resource-change hints."""

from __future__ import annotations

import asyncio
import gc
from contextlib import AsyncExitStack

import pytest
from pydantic import ValidationError

from tinkerfin_notifications import (
    MemoryBackend,
    Notification,
    NotificationCapacityExceeded,
    NotificationError,
    NotificationLimits,
    Notifications,
    NotificationsClosed,
    NotificationScope,
    NotificationsNotStarted,
    NotificationUnavailable,
    ResyncRequired,
)
from tinkerfin_notifications.backend import NotificationReceiver


def change(key: str = "one", *, owner: str | None = None) -> Notification:
    return Notification(
        scope=NotificationScope("project", owner),
        topic="documents.changed",
        key=key,
    )


async def test_failed_publication_requires_resync_for_affected_listeners() -> None:
    failed = asyncio.Event()

    class FailingPublisher(MemoryBackend):
        async def publish(self, payload: bytes) -> None:
            notification = Notification.model_validate_json(payload)
            if notification.key == "missing":
                failed.set()
                raise NotificationUnavailable("Controlled publication failure")
            await super().publish(payload)

    async with Notifications(backend=FailingPublisher()) as notifications:
        async with (
            notifications.subscribe(
                scope=NotificationScope("project", "first")
            ) as affected,
            notifications.subscribe(
                scope=NotificationScope("project", "second")
            ) as other,
        ):
            await notifications.publish(change("missing", owner="first"))
            await failed.wait()
            await notifications.publish(change("next", owner="first"))
            await notifications.publish(change("next", owner="second"))
            assert await anext(affected) == ResyncRequired("disconnected")
            assert await anext(affected) == change("next", owner="first")
            assert await anext(other) == change("next", owner="second")


async def test_each_listener_receives_an_independent_snapshot() -> None:
    source = change().model_copy(update={"details": {"ids": ["original"]}})
    async with Notifications() as notifications:
        async with (
            notifications.subscribe() as first,
            notifications.subscribe() as second,
        ):
            await notifications.publish(source)
            source.details["ids"] = ["modified"]
            received = await anext(first)
            assert isinstance(received, Notification)
            assert received.details == {"ids": ["original"]}
            received.details["ids"] = ["consumer mutation"]
            other = await anext(second)
            assert isinstance(other, Notification)
            assert other.details == {"ids": ["original"]}


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


async def test_topic_and_key_filters_and_empty_topics() -> None:
    async with Notifications() as notifications, AsyncExitStack() as stack:
        selected = await stack.enter_async_context(
            notifications.subscribe(topics={"documents.changed"}, key="selected")
        )
        empty = await stack.enter_async_context(notifications.subscribe(topics=set()))
        await notifications.publish(change("ignored"))
        await notifications.publish(change("selected"))
        assert await anext(selected) == change("selected")
        await empty.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(empty)


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


async def test_closing_listener_wakes_reader_and_keeps_service_usable() -> None:
    async with Notifications() as notifications:
        async with notifications.subscribe() as subscription:
            entered = asyncio.Event()

            async def read() -> None:
                entered.set()
                with pytest.raises(StopAsyncIteration):
                    await anext(subscription)

            reader = asyncio.create_task(read())
            await entered.wait()
            await subscription.aclose()
            await reader
        async with notifications.subscribe() as remaining:
            await notifications.publish(change())
            assert await anext(remaining) == change()


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


async def test_subscription_and_message_capacity_are_explicit() -> None:
    async with Notifications(
        limits=NotificationLimits(max_subscriptions=1, max_notification_bytes=256)
    ) as notifications:
        async with notifications.subscribe():
            with pytest.raises(NotificationCapacityExceeded):
                async with notifications.subscribe():
                    pytest.fail("capacity must be checked before context entry")
            with pytest.raises(NotificationCapacityExceeded):
                await notifications.publish(
                    change().model_copy(update={"details": {"x": "x" * 256}})
                )


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


async def test_cancelled_close_waits_for_owned_cleanup() -> None:
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class HeldBackend(MemoryBackend):
        async def aclose(self) -> None:
            closing.set()
            await release.wait()
            await super().aclose()
            closed.set()

    notifications = Notifications(backend=HeldBackend())
    await notifications.__aenter__()
    task = asyncio.create_task(notifications.aclose())
    await closing.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    await notifications.aclose()


async def test_context_cancellation_survives_backend_close_failure() -> None:
    failure = NotificationUnavailable("Controlled close failure")
    closed = asyncio.Event()

    class FailedClose(MemoryBackend):
        async def aclose(self) -> None:
            await super().aclose()
            closed.set()
            raise failure

    async def consume() -> None:
        async with Notifications(backend=FailedClose()):
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.Event().wait()

    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.create_task(consume())
    assert closed.is_set()
    assert caught.value.__cause__ is failure


async def test_context_process_control_survives_cancellation_during_cleanup() -> None:
    exiting = asyncio.Event()
    original = SystemExit("Controlled process stop")
    diagnostic = OSError("Controlled original cause")

    async def consume() -> BaseException:
        try:
            async with Notifications():
                exiting.set()
                raise original from diagnostic
        except BaseException as error:  # noqa: BLE001 - observe control without exiting the test process
            return error

    task = asyncio.create_task(consume())
    await exiting.wait()
    task.cancel()
    assert await task is original
    assert isinstance(original.__cause__, BaseExceptionGroup)
    assert diagnostic in original.__cause__.exceptions
    assert any(
        isinstance(error, asyncio.CancelledError)
        for error in original.__cause__.exceptions
    )


async def test_context_cleanup_group_cannot_form_a_cycle_with_the_body_failure() -> (
    None
):
    primary = ExceptionGroup("Body errors", [ValueError("one"), TypeError("two")])
    secondary = OSError("Independent cleanup failure")

    class FailedClose(MemoryBackend):
        async def aclose(self) -> None:
            await super().aclose()
            raise ExceptionGroup("Cleanup errors", [primary, secondary])

    with pytest.raises(ExceptionGroup) as caught:
        async with Notifications(backend=FailedClose()):
            raise primary
    assert caught.value is primary

    def check(error: BaseException, ancestors: tuple[BaseException, ...]) -> None:
        assert all(error is not ancestor for ancestor in ancestors)
        children = [
            item for item in (error.__cause__, error.__context__) if item is not None
        ]
        if isinstance(error, BaseExceptionGroup):
            children.extend(error.exceptions)
        for child in children:
            check(child, (*ancestors, error))

    check(primary, ())


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


@pytest.mark.parametrize("phase", ["startup", "sender"])
async def test_service_failure_retains_original_cause_and_backend_close_failure(
    phase: str,
) -> None:
    original = SystemExit("Controlled service stop")
    diagnostic = OSError("Controlled original cause")
    cleanup = NotificationUnavailable("Controlled backend close failure")
    failed = asyncio.Event()

    class FailedBackend(MemoryBackend):
        async def start(self, receive_notification: NotificationReceiver) -> None:
            await super().start(receive_notification)
            if phase == "startup":
                # A regular startup failure can be safely raised by its owned task.
                raise NotificationUnavailable(
                    "Controlled startup failure"
                ) from diagnostic

        async def publish(self, payload: bytes) -> None:
            failed.set()
            raise original from diagnostic

        async def aclose(self) -> None:
            await super().aclose()
            raise cleanup

    service = Notifications(backend=FailedBackend())
    if phase == "startup":
        with pytest.raises(NotificationUnavailable) as caught:
            await service.__aenter__()
        primary: BaseException = caught.value
    else:
        await service.__aenter__()
        await service.publish(change())
        await failed.wait()
        with pytest.raises(SystemExit) as stopped:
            await service.aclose()
        primary = stopped.value
        assert primary is original
    assert isinstance(primary.__cause__, BaseExceptionGroup)
    assert diagnostic in primary.__cause__.exceptions
    assert cleanup in primary.__cause__.exceptions


async def test_close_during_failed_start_still_releases_backend() -> None:
    starting, closed = asyncio.Event(), asyncio.Event()
    failure = NotificationError("startup could not settle")

    class InterruptedBackend(MemoryBackend):
        async def start(self, receive_notification: NotificationReceiver) -> None:
            await super().start(receive_notification)
            starting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                raise failure from error

        async def aclose(self) -> None:
            await super().aclose()
            closed.set()

    service = Notifications(backend=InterruptedBackend())
    entering = asyncio.create_task(service.__aenter__())
    await starting.wait()
    with pytest.raises(NotificationError):
        await service.aclose()
    with pytest.raises(NotificationError):
        await entering
    assert closed.is_set()
    assert failure.__cause__ is not failure


async def test_cancelled_entry_before_backend_start_leaves_no_unawaited_coroutine(
    recwarn: pytest.WarningsRecorder,
) -> None:
    async def cancelled_entry() -> None:
        service = Notifications()
        opening = asyncio.create_task(service.__aenter__())
        asyncio.get_running_loop().call_soon(opening.cancel)
        with pytest.raises(asyncio.CancelledError):
            await opening
        await service.aclose()

    await cancelled_entry()
    settled = asyncio.Event()
    asyncio.get_running_loop().call_soon(settled.set)
    await settled.wait()
    gc.collect()
    assert not [
        warning for warning in recwarn if "was never awaited" in str(warning.message)
    ]


async def test_subscription_entry_cannot_revive_after_close_or_enter_twice() -> None:
    waiting, release = asyncio.Event(), asyncio.Event()

    class HeldReadiness(MemoryBackend):
        async def wait_ready(self) -> None:
            waiting.set()
            await release.wait()
            await super().wait_ready()

    async with Notifications(backend=HeldReadiness()) as notifications:
        listener = notifications.subscribe()
        entering = asyncio.create_task(listener.__aenter__())
        await waiting.wait()
        with pytest.raises(NotificationsClosed):
            await listener.__aenter__()
        await listener.aclose()
        release.set()
        with pytest.raises(NotificationsClosed):
            await entering
        with pytest.raises(StopAsyncIteration):
            await anext(listener)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
async def test_publish_revalidates_mutated_values_before_json_encoding(
    number: float,
) -> None:
    notification = change()
    notification.details["nested"] = {"number": number}
    async with Notifications() as notifications:
        with pytest.raises(ValidationError):
            await notifications.publish(notification)


async def test_pending_publications_are_bounded_coalesced_and_do_not_block_callers() -> (
    None
):
    publishing, release = asyncio.Event(), asyncio.Event()
    delivered: list[Notification] = []

    class HeldPublisher(MemoryBackend):
        async def publish(self, payload: bytes) -> None:
            value = Notification.model_validate_json(payload)
            if value.key == "in-flight":
                publishing.set()
                await release.wait()
            delivered.append(value)
            await super().publish(payload)

    async with Notifications(
        backend=HeldPublisher(), limits=NotificationLimits(max_pending_publications=1)
    ) as notifications:
        async with notifications.subscribe() as changes:
            await notifications.publish(change("in-flight"))
            await publishing.wait()
            try:
                await notifications.publish(
                    change().model_copy(update={"details": {"value": 1}})
                )
                await notifications.publish(
                    change().model_copy(update={"details": {"value": 2}})
                )
                with pytest.raises(NotificationCapacityExceeded):
                    await notifications.publish(change("excess"))
            finally:
                release.set()
            assert await anext(changes) == change("in-flight")
            latest = await anext(changes)
            assert isinstance(latest, Notification) and latest.details == {"value": 2}
    assert [note.key for note in delivered] == ["in-flight", "one"]


def test_scopes_limits_and_json_reject_invalid_values() -> None:
    with pytest.raises(ValueError):
        NotificationScope(" padded")
    with pytest.raises(ValueError):
        NotificationLimits(max_subscriptions=0)
    with pytest.raises(ValidationError):
        Notification(
            scope=NotificationScope("project"),
            topic="x",
            key="k",
            details={"n": float("nan")},
        )


def test_error_context_is_copied_readonly_and_cause_is_trusted() -> None:
    context = {"resource": "safe"}
    original = RuntimeError("credential must remain private")
    error = NotificationError("safe message", context=context, cause=original)
    context["resource"] = "changed"
    assert error.context["resource"] == "safe"
    assert str(error) == "safe message"
    assert error.__cause__ is original
    assert error.cause is original
    with pytest.raises(TypeError):
        exec("context['resource'] = 'mutation'", {"context": error.context})
