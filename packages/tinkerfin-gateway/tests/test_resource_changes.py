"""Bound resource watches use Gateway authorization and response ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Collection
from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from starlette.requests import Request

from tinkerfin_gateway import Gateway, GatewayError
from tinkerfin_gateway.starlette import sse_response
from tinkerfin_messaging import Messaging
from tinkerfin_notifications import Notification, Notifications, ResyncRequired


@pytest.fixture(autouse=True)
def controlled_time(monkeypatch: pytest.MonkeyPatch) -> None:
    async def wait(
        tasks: Collection[asyncio.Task[str | Notification | ResyncRequired]],
        _timeout: float,
    ) -> set[asyncio.Task[str | Notification | ResyncRequired]]:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return done

    monkeypatch.setattr("tinkerfin_gateway.notifications._monotonic", lambda: 0.0)
    monkeypatch.setattr(
        "tinkerfin_gateway.notifications._now", lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    monkeypatch.setattr("tinkerfin_gateway.notifications._wait_for_changes", wait)


class _Watch:
    def __init__(self) -> None:
        self.entered = 0
        self.closed = 0
        self.reading = asyncio.Event()
        self.queue: asyncio.Queue[str | ResyncRequired | None] = asyncio.Queue(2)
        self.read_error: Exception | None = None
        self.close_error: Exception | None = None

    async def _changes(self) -> AsyncGenerator[str | ResyncRequired, None]:
        while True:
            self.reading.set()
            if self.read_error:
                raise self.read_error
            value = await self.queue.get()
            if value is None:
                return
            yield value

    @asynccontextmanager
    async def watch(self) -> AsyncGenerator[AsyncIterator[str | ResyncRequired], None]:
        self.entered += 1
        try:
            async with aclosing(self._changes()) as changes:
                yield changes
        finally:
            self.closed += 1
            if self.close_error:
                raise self.close_error


async def test_watch_is_ready_before_baseline_and_keeps_change_and_resync_order() -> (
    None
):
    source = _Watch()
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        async with await gateway.resource_changes(watch_changes=source.watch) as stream:
            assert source.entered == 1
            body = stream.to_sse()
            assert await anext(body) == b"event: ready\ndata: {}\n\n"
            await source.queue.put("files_changed")
            assert (
                await anext(body)
                == b'event: change\ndata: {"kind": "files_changed"}\n\n'
            )
            await source.queue.put(ResyncRequired("disconnected"))
            assert await anext(body) == b"event: resync\ndata: {}\n\n"
            await source.queue.put(None)
            with pytest.raises(StopAsyncIteration):
                await anext(body)
        assert source.closed == 1


async def test_unused_response_and_repeated_close_release_the_watch_once() -> None:
    source = _Watch()
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        response = await sse_response(
            gateway.resource_changes(watch_changes=source.watch)
        )
        await response.aclose()
        await response.aclose()
        assert source.entered == source.closed == 1


async def test_revoked_access_never_opens_a_watch_and_late_revocation_closes_it() -> (
    None
):
    source = _Watch()
    allowed = False

    async def authorize() -> bool:
        return allowed

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        with pytest.raises(PermissionError):
            await gateway.resource_changes(
                watch_changes=source.watch, authorize=authorize
            )
        assert source.entered == 0
        allowed = True
        stream = await gateway.resource_changes(
            watch_changes=source.watch, authorize=authorize
        )
        allowed = False
        assert [frame async for frame in stream.to_sse()] == []
        assert source.closed == 1


async def test_expired_feed_cannot_deliver_an_already_queued_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _Watch()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("tinkerfin_gateway.notifications._now", lambda: now)
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(
            watch_changes=source.watch, expires_at=now + timedelta(seconds=1)
        )
        body = stream.to_sse()
        await anext(body)
        source.queue.put_nowait("private_change")
        now += timedelta(seconds=2)
        with pytest.raises(StopAsyncIteration):
            await anext(body)
        assert source.closed == 1


async def test_cancelling_a_waiting_reader_closes_only_its_watch() -> None:
    first, other = _Watch(), _Watch()
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=first.watch)
        second = await gateway.resource_changes(watch_changes=other.watch)
        body = stream.to_sse()
        await anext(body)
        pull = asyncio.create_task(anext(body))
        await first.reading.wait()
        pull.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pull
        assert first.closed == 1 and other.closed == 0
        await second.aclose()


async def test_source_failure_and_cleanup_failure_remain_observable() -> None:
    source = _Watch()
    source.read_error = ValueError("source failed")
    source.close_error = RuntimeError("close failed")
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=source.watch)
        body = stream.to_sse()
        await anext(body)
        with pytest.raises(ValueError) as caught:
            await anext(body)
        assert caught.value is source.read_error
        assert source.close_error is caught.value.__cause__
        assert source.closed == 1


async def test_oversized_hint_fails_and_still_releases_its_source() -> None:
    source = _Watch()
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=source.watch)
        body = stream.to_sse()
        await anext(body)
        await source.queue.put("x" * 1025)
        with pytest.raises(GatewayError):
            await anext(body)
        assert source.closed == 1


@pytest.mark.parametrize("consume", [False, True])
async def test_watch_context_and_iteration_keep_one_task(consume: bool) -> None:
    active = ContextVar("active_watch", default=False)
    finished: list[bool] = []

    @asynccontextmanager
    async def watch() -> AsyncGenerator[AsyncIterator[str], None]:
        owner = asyncio.current_task()
        token = active.set(True)

        async def changes() -> AsyncGenerator[str, None]:
            iteration_token = active.set(True)
            try:
                assert asyncio.current_task() is owner
                yield "files_changed"
                assert asyncio.current_task() is owner
            finally:
                active.reset(iteration_token)

        try:
            async with aclosing(changes()) as values:
                yield values
        finally:
            assert asyncio.current_task() is owner
            active.reset(token)
            finished.append(True)

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=watch)
        assert not active.get()
        if consume:
            assert len([frame async for frame in stream.to_sse()]) == 2
        await stream.aclose()
        assert finished == [True] and not active.get()


async def test_close_preserves_independent_failure_attached_to_cancelled_pull() -> None:
    reading = asyncio.Event()
    cleanup_error = OSError("source cleanup failed")

    @asynccontextmanager
    async def watch() -> AsyncGenerator[AsyncIterator[str], None]:
        async def changes() -> AsyncGenerator[str, None]:
            reading.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as cancelled:
                raise cancelled from cleanup_error
            yield "unreachable"

        async with aclosing(changes()) as values:
            yield values

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=watch)
        body = stream.to_sse()
        await anext(body)
        pull = asyncio.create_task(anext(body))
        await reading.wait()
        with pytest.raises(asyncio.CancelledError) as caught:
            await stream.aclose()
        assert caught.value.__cause__ is cleanup_error
        with pytest.raises(asyncio.CancelledError) as pulled:
            await pull
        assert pulled.value.__cause__ is cleanup_error


async def test_watch_can_own_an_anyio_task_group_until_close() -> None:
    closed = asyncio.Event()

    @asynccontextmanager
    async def watch() -> AsyncGenerator[AsyncIterator[str], None]:
        async with anyio.create_task_group():

            async def changes() -> AsyncGenerator[str, None]:
                yield "files_changed"

            yield changes()
        closed.set()

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=watch)
        await stream.aclose()
        assert closed.is_set()


async def test_source_cancellation_preserves_context_exit_failure() -> None:
    closing, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellation = asyncio.CancelledError("source cancelled itself")
    failure = OSError("context exit failed")

    @asynccontextmanager
    async def watch() -> AsyncGenerator[AsyncIterator[str], None]:
        async def changes() -> AsyncGenerator[str, None]:
            raise cancellation
            yield "unreachable"

        try:
            yield changes()
        finally:
            closing.set()
            await release.wait()
            closed.set()
            raise failure

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=watch)
        body = stream.to_sse()
        await anext(body)
        pull = asyncio.create_task(anext(body))
        await closing.wait()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await pull
        assert closed.is_set() and caught.value is cancellation
        assert caught.value.__cause__ is failure
        with pytest.raises(asyncio.CancelledError):
            await stream.aclose()


@pytest.mark.parametrize("stage", ["admission", "read"])
async def test_close_discards_late_admission_and_late_changes(stage: str) -> None:
    pending, closed = asyncio.Event(), asyncio.Event()

    async def accepted_operation() -> None:
        pending.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            # Model a provider delivering an already accepted result on cancel.
            return

    @asynccontextmanager
    async def watch() -> AsyncGenerator[AsyncIterator[str], None]:
        if stage == "admission":
            await accepted_operation()

        async def changes() -> AsyncGenerator[str, None]:
            await accepted_operation()
            yield "files_changed"
            pytest.fail("a closed stream must not request another change")

        try:
            yield changes()
        finally:
            closed.set()

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        if stage == "admission":
            preparation = asyncio.create_task(
                gateway.resource_changes(watch_changes=watch)
            )
            await pending.wait()
            preparation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await preparation
        else:
            stream = await gateway.resource_changes(watch_changes=watch)
            body = stream.to_sse()
            await anext(body)
            pull = asyncio.create_task(anext(body))
            await pending.wait()
            await stream.aclose()
            with pytest.raises(StopAsyncIteration):
                await pull
        assert closed.is_set()


class _Connection(Request):
    """Expose a transport check controlled by disconnect and handoff signals."""

    def __init__(self) -> None:
        super().__init__({"type": "http"})
        self.checking = asyncio.Event()
        self.handing_off = asyncio.Event()
        self.release_check = asyncio.Event()
        self.disconnected = False
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        self.checking.set()
        if self.checks > 1:
            self.handing_off.set()
        await self.release_check.wait()
        return self.disconnected


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_http_disconnect_closes_pending_or_concurrently_prepared_watch(
    prepared: bool,
    cleanup_fails: bool,
) -> None:
    request = _Connection()
    entered, admitted, closing, release_exit = (asyncio.Event() for _ in range(4))
    closed = asyncio.Event()
    cleanup_error = OSError("watch exit failed")

    @asynccontextmanager
    async def watch() -> AsyncGenerator[AsyncIterator[str], None]:
        async def changes() -> AsyncGenerator[str, None]:
            yield "files_changed"

        try:
            entered.set()
            await admitted.wait()
            yield changes()
        finally:
            closing.set()
            await release_exit.wait()
            closed.set()
            if cleanup_fails:
                raise cleanup_error

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)

        async def prepare() -> BaseException:
            try:
                response = await sse_response(
                    gateway.resource_changes(watch_changes=watch), request=request
                )
            except BaseException as error:  # noqa: BLE001 - retain the original cancellation and every cleanup cause
                return error
            await response.aclose()
            pytest.fail("a disconnected request must not receive a prepared response")

        task = asyncio.create_task(prepare())
        await entered.wait()
        await request.checking.wait()
        if prepared:
            admitted.set()
            await request.handing_off.wait()
        request.disconnected = True
        request.release_check.set()
        await closing.wait()
        task.cancel()
        release_exit.set()
        outcome = await task
        assert isinstance(outcome, asyncio.CancelledError) and closed.is_set()
        if cleanup_fails:
            from test_streams import retained_failures

            assert id(cleanup_error) in retained_failures(outcome)


async def test_immediate_http_preparation_returns_a_live_response() -> None:
    source = _Watch()
    request = _Connection()
    request.release_check.set()
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=source.watch)
        response = await sse_response(stream, request=request)
        assert source.closed == 0
        await response.aclose()
        assert source.closed == 1


async def test_http_disconnect_closes_a_future_completed_before_handoff() -> None:
    source = _Watch()
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=source.watch)
        admitted = asyncio.get_running_loop().create_future()

        async def receive():
            if not admitted.done():
                admitted.set_result(stream)
            return {"type": "http.disconnect"}

        request = Request({"type": "http"}, receive)
        preparing = asyncio.create_task(sse_response(admitted, request=request))
        with pytest.raises(asyncio.CancelledError):
            await preparing
        assert admitted.done() and not admitted.cancelled()
        assert source.closed == 1
