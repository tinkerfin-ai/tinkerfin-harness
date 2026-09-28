"""SSE preflight, scope isolation, authorization, and response ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Coroutine, Generator
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

import pytest
from anyio.lowlevel import checkpoint_if_cancelled
from langchain_core.messages import AIMessage
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope
from test_gateway import Model, command

from tinkerfin import TinkerFin
from tinkerfin_gateway import Gateway, GatewayClosed, GatewayError, GatewayErrorCode
from tinkerfin_gateway.starlette import sse_response
from tinkerfin_messaging import InvalidCursor, Messaging
from tinkerfin_notifications import (
    Notification,
    NotificationCapacityExceeded,
    NotificationLimits,
    Notifications,
    NotificationScope,
    ResyncRequired,
)

_TaskResult = TypeVar("_TaskResult")


@pytest.fixture(autouse=True)
def controlled_feed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    async def wait_for_changes(
        tasks: Collection[asyncio.Task[Notification | ResyncRequired]],
        _timeout: float,
    ) -> set[asyncio.Task[Notification | ResyncRequired]]:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return done

    monkeypatch.setattr("tinkerfin_gateway.notifications._monotonic", lambda: 0.0)
    monkeypatch.setattr(
        "tinkerfin_gateway.notifications._now", lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    monkeypatch.setattr(
        "tinkerfin_gateway.notifications._wait_for_changes", wait_for_changes
    )


def change(key: str, scope: NotificationScope) -> Notification:
    return Notification(scope=scope, topic="documents.changed", key=key)


def retained_failures(error: BaseException | None) -> set[int]:
    pending = [error] if error is not None else []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend(
            item
            for item in (current.__cause__, current.__context__)
            if item is not None
        )
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return seen


async def test_context_process_control_survives_cancellation_during_cleanup() -> None:
    scope = NotificationScope("app")
    exiting = asyncio.Event()
    original = SystemExit("Controlled process stop")
    async with (
        Messaging() as messaging,
        Notifications(limits=NotificationLimits(max_subscriptions=1)) as notifications,
    ):
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.notifications(scopes=[scope])

        async def consume() -> BaseException:
            try:
                async with stream:
                    exiting.set()
                    raise original
            except BaseException as error:  # noqa: BLE001 - observe control without exiting the test process
                return error

        task = asyncio.create_task(consume())
        await exiting.wait()
        task.cancel()
        assert await task is original
        assert isinstance(original.__cause__, asyncio.CancelledError)
        async with await gateway.notifications(scopes=[scope]):
            pass


async def test_all_scopes_are_ready_before_the_baseline_and_foreign_changes_are_filtered() -> (
    None
):
    alpha, beta = NotificationScope("alpha"), NotificationScope("shared", "alice")
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        async with await gateway.notifications(scopes=[alpha, beta]) as stream:
            body = stream.to_sse()
            assert await anext(body) == b"event: ready\ndata: {}\n\n"
            await notifications.publish(
                change("private", NotificationScope("shared", "bob"))
            )
            await notifications.publish(change("one", alpha))
            await notifications.publish(change("two", beta))
            frames = [await anext(body), await anext(body)]
            assert all(frame.startswith(b"event: change\n") for frame in frames)
            assert b'"key":"one"' in b"".join(frames)
            assert b'"key":"two"' in b"".join(frames)
            assert b"private" not in b"".join(frames)
            await body.aclose()


async def test_overflow_requires_a_new_baseline_and_partial_preflight_releases_scopes() -> (
    None
):
    scope = NotificationScope("alpha")
    async with (
        Messaging() as messaging,
        Notifications(
            limits=NotificationLimits(
                max_subscriptions=1,
                max_pending_per_subscription=1,
            )
        ) as notifications,
    ):
        gateway = Gateway(messaging=messaging, notifications=notifications)
        with pytest.raises(NotificationCapacityExceeded):
            await gateway.notifications(scopes=[scope, NotificationScope("beta")])
        async with await gateway.notifications(scopes=[scope]) as stream:
            body = stream.to_sse()
            assert b"event: ready" in await anext(body)
            await notifications.publish(change("one", scope))
            await notifications.publish(change("two", scope))
            assert await anext(body) == b"event: resync\ndata: {}\n\n"
            assert b'"key":"two"' in await anext(body)
            await body.aclose()


async def test_expired_access_is_rejected_before_response_and_queued_data_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("tinkerfin_gateway.notifications._now", lambda: now)
    scope = NotificationScope("alpha")
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        with pytest.raises(PermissionError):
            await gateway.notifications(scopes=[scope], expires_at=now)
        async with await gateway.notifications(
            scopes=[scope], expires_at=now + timedelta(seconds=1)
        ) as stream:
            body = stream.to_sse()
            assert b"event: ready" in await anext(body)
            await notifications.publish(change("queued", scope))
            now += timedelta(seconds=1)
            with pytest.raises(StopAsyncIteration):
                await anext(body)


async def test_idle_authorization_recheck_uses_a_controlled_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    allowed = True
    checks: list[bool] = []

    async def authorize() -> bool:
        checks.append(allowed)
        return allowed

    async def tick(
        tasks: Collection[asyncio.Task[Notification | ResyncRequired]],
        timeout: float,
    ) -> set[asyncio.Task[Notification | ResyncRequired]]:
        nonlocal clock, allowed
        assert tasks and timeout == 15.0
        clock += timeout
        allowed = False
        return set()

    monkeypatch.setattr("tinkerfin_gateway.notifications._monotonic", lambda: clock)
    monkeypatch.setattr("tinkerfin_gateway.notifications._wait_for_changes", tick)
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        async with await gateway.notifications(
            scopes=[NotificationScope("account")], authorize=authorize
        ) as stream:
            body = stream.to_sse()
            assert b"event: ready" in await anext(body)
            with pytest.raises(StopAsyncIteration):
                await anext(body)
        assert checks[-1] is False


async def test_unsent_response_and_failed_headers_release_their_subscriptions() -> None:
    scope = NotificationScope("account")
    async with (
        Messaging() as messaging,
        Notifications(limits=NotificationLimits(max_subscriptions=1)) as notifications,
    ):
        gateway = Gateway(messaging=messaging, notifications=notifications)
        response = await sse_response(gateway.notifications(scopes=[scope]))
        await response.aclose()
        await response.aclose()
        with pytest.raises(GatewayClosed):
            response_source = await gateway.notifications(scopes=[scope])
            await response_source.aclose()
            response_source.to_sse()
        response = await sse_response(gateway.notifications(scopes=[scope]))

        async def receive() -> Message:
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(_message: Message) -> None:
            raise OSError("client left before headers")

        asgi: Scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
        with pytest.raises(ClientDisconnect):
            await response(asgi, receive, send)
        async with notifications.subscribe(scope=scope):
            pass


@pytest.mark.parametrize(
    "failure_kind", ["none", "error", "cancel_cause", "exit", "interrupt"]
)
@pytest.mark.parametrize("cancel_during_disconnect", [False, True])
async def test_disconnect_settles_authorization_resource_cleanup(
    failure_kind: str,
    cancel_during_disconnect: bool,
) -> None:
    scope = NotificationScope("account")
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
    checks = 0
    original: BaseException | None = {
        "none": None,
        "error": OSError("cleanup failed"),
        "cancel_cause": asyncio.CancelledError("cleanup cancelled"),
        "exit": SystemExit("process stop"),
        "interrupt": KeyboardInterrupt("process interrupted"),
    }[failure_kind]
    cause = OSError("cleanup transport")
    background: list[str] = []

    async def background_task() -> None:
        background.append("completed")

    async def authorize() -> bool:
        nonlocal checks
        checks += 1
        if checks < 3:
            return True
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            await checkpoint_if_cancelled()
            cleaned.set()
            if original is not None:
                if failure_kind == "cancel_cause":
                    raise original from cause
                raise original
        return True

    async def receive() -> Message:
        await entered.wait()
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        pass

    async with (
        Messaging() as messaging,
        Notifications(limits=NotificationLimits(max_subscriptions=1)) as notifications,
    ):
        gateway = Gateway(messaging=messaging, notifications=notifications)
        response = await sse_response(
            gateway.notifications(scopes=[scope], authorize=authorize)
        )
        response.background = BackgroundTask(background_task)

        async def consume() -> BaseException | None:
            try:
                await response(
                    {"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send
                )
            except BaseException as error:  # noqa: BLE001 - inspect process control without exiting the test runner
                return error
            return None

        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        armed = False
        serving: asyncio.Task[BaseException | None] | None = None

        class ObservedTask(asyncio.Task[_TaskResult]):
            def cancel(self, msg: object = None) -> bool:
                accepted = super().cancel(msg)
                # Release only after cancellation has reached the response's
                # owned work. Task.cancel forwards to its waiter synchronously.
                if armed and self is not serving and accepted:
                    release.set()
                return accepted

        def task_factory(
            loop: asyncio.AbstractEventLoop,
            coroutine: Coroutine[Any, Any, _TaskResult]
            | Generator[Any, None, _TaskResult],
            **kwargs: Any,
        ) -> asyncio.Task[_TaskResult]:
            return ObservedTask(coroutine, loop=loop, **kwargs)

        if cancel_during_disconnect:
            loop.set_task_factory(task_factory)
        serving = asyncio.create_task(consume())
        try:
            await cleaning.wait()
            if cancel_during_disconnect:
                armed = True
                serving.cancel()
            else:
                release.set()
            outcome = await serving
            assert cleaned.is_set()
            if cancel_during_disconnect and failure_kind in {
                "none",
                "error",
                "cancel_cause",
            }:
                assert isinstance(outcome, asyncio.CancelledError)
            else:
                assert outcome is original
            if original is not None:
                assert id(original) in retained_failures(outcome)
            if failure_kind == "cancel_cause" and not cancel_during_disconnect:
                assert outcome is not None and outcome.__cause__ is cause
            if failure_kind == "cancel_cause":
                assert id(cause) in retained_failures(outcome)
            assert background == (
                ["completed"]
                if original is None and not cancel_during_disconnect
                else []
            )
            async with notifications.subscribe(scope=scope):
                pass
        finally:
            loop.set_task_factory(previous_factory)
            release.set()
            await response.aclose()
            await asyncio.gather(serving, return_exceptions=True)


@pytest.mark.parametrize(
    "spec, origin, failure_type",
    [
        ("2.3", "send", asyncio.CancelledError),
        ("2.3", "receive", asyncio.CancelledError),
        ("2.3", "send", SystemExit),
        ("2.3", "receive", KeyboardInterrupt),
        ("2.4", "send", SystemExit),
        ("2.4", "send", KeyboardInterrupt),
    ],
)
async def test_asgi_control_failures_reach_the_owner_after_cleanup(
    spec: str, origin: str, failure_type: type[BaseException]
) -> None:
    entered = asyncio.Event()
    failure = failure_type("original control")
    checks = 0

    async def authorize() -> bool:
        nonlocal checks
        checks += 1
        if checks < 3:
            return True
        entered.set()
        if origin == "send":
            raise failure
        await asyncio.Event().wait()
        return True

    async def receive() -> Message:
        await entered.wait()
        if origin == "receive":
            raise failure
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        pass

    scope = NotificationScope("account")
    async with (
        Messaging() as messaging,
        Notifications(limits=NotificationLimits(max_subscriptions=1)) as notifications,
    ):
        gateway = Gateway(messaging=messaging, notifications=notifications)
        response = await sse_response(
            gateway.notifications(scopes=[scope], authorize=authorize)
        )
        outcome: BaseException | None = None
        try:
            await response(
                {"type": "http", "asgi": {"spec_version": spec}}, receive, send
            )
        except BaseException as error:  # noqa: BLE001 - inspect original control at the public caller
            outcome = error
        if (
            spec == "2.3"
            and origin == "send"
            and failure_type is asyncio.CancelledError
        ):
            assert outcome is None
        else:
            assert outcome is failure
        async with notifications.subscribe(scope=scope):
            pass


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_repeated_request_cancellation_settles_cleanup_and_retains_its_failure(
    spec: str,
) -> None:
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
    failure = OSError("resource cleanup failed")
    checks = 0

    async def authorize() -> bool:
        nonlocal checks
        checks += 1
        if checks < 3:
            return True
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()
            raise failure

    async def receive() -> Message:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        pass

    scope = NotificationScope("account")
    async with (
        Messaging() as messaging,
        Notifications(limits=NotificationLimits(max_subscriptions=1)) as notifications,
    ):
        gateway = Gateway(messaging=messaging, notifications=notifications)
        response = await sse_response(
            gateway.notifications(scopes=[scope], authorize=authorize)
        )

        async def consume() -> BaseException | None:
            try:
                await response(
                    {"type": "http", "asgi": {"spec_version": spec}}, receive, send
                )
            except BaseException as error:  # noqa: BLE001 - inspect cancellation and retained cleanup causes
                return error
            return None

        task = asyncio.create_task(consume())
        try:
            await entered.wait()
            task.cancel()
            await cleaning.wait()
            task.cancel()
            release.set()
            outcome = await task
            assert isinstance(outcome, asyncio.CancelledError)
            assert id(failure) in retained_failures(outcome)
            assert cleaned.is_set()
            async with notifications.subscribe(scope=scope):
                pass
        finally:
            release.set()
            await response.aclose()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_background_failure_is_not_mapped_to_client_disconnect(spec: str) -> None:
    failure = OSError("background failure")
    checks = 0

    async def authorize() -> bool:
        nonlocal checks
        checks += 1
        return checks < 3

    async def background() -> None:
        raise failure

    async def receive() -> Message:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        pass

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        response = await sse_response(
            gateway.notifications(
                scopes=[NotificationScope("account")], authorize=authorize
            )
        )
        response.background = BackgroundTask(background)
        with pytest.raises(OSError) as caught:
            await response(
                {"type": "http", "asgi": {"spec_version": spec}}, receive, send
            )
        assert caught.value is failure


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_closing_an_active_response_settles_sending_and_pending_reads(
    spec: str,
) -> None:
    ready = asyncio.Event()
    scope = NotificationScope("account")
    async with (
        Messaging() as messaging,
        Notifications(limits=NotificationLimits(max_subscriptions=1)) as notifications,
    ):
        gateway = Gateway(messaging=messaging, notifications=notifications)
        response = await sse_response(gateway.notifications(scopes=[scope]))

        async def receive() -> Message:
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            if message["type"] == "http.response.body":
                ready.set()

        serving = asyncio.create_task(
            response({"type": "http", "asgi": {"spec_version": spec}}, receive, send)
        )
        await ready.wait()
        await response.aclose()
        with pytest.raises(asyncio.CancelledError):
            await serving
        async with notifications.subscribe(scope=scope):
            pass


async def test_post_cursor_keeps_admission_semantics_and_follow_marks_replay() -> None:
    runtime = (
        TinkerFin()
        .with_namespace("account")
        .build(Model(responses=[AIMessage(content="answer")]))
    )
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        async with await gateway.stream(runtime, command()) as output:
            initial = [frame async for frame in output.to_sse()]
        assert initial and all(b"event: replay" not in frame for frame in initial)
        async with await gateway.stream(runtime, command()) as output:
            assert [frame async for frame in output.to_sse()] == []
        async with await gateway.stream(runtime, command(), after=0) as output:
            replayed_post = [frame async for frame in output.to_sse()]
        assert replayed_post == initial
        run = gateway.run(runtime.run_identity("thread", "run"))
        async with run.subscribe() as output:
            replayed_get = [frame async for frame in output.to_sse()]
        assert all(b"event: replay\n" in frame for frame in replayed_get)
        assert [
            frame.replace(b"event: replay\n", b"") for frame in replayed_get
        ] == initial
        with pytest.raises(InvalidCursor):
            await sse_response(gateway.stream(runtime, command(), after=100000))


async def test_missing_scope_is_rejected_instead_of_opening_global_access() -> None:
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        # Exercise untyped HTTP integration input without claiming it is valid.
        invalid: object = [None]
        with pytest.raises(TypeError):
            await gateway.notifications(
                scopes=cast(Collection[NotificationScope], invalid)
            )


async def test_late_authorization_and_ready_resume_cannot_reopen_a_closed_feed() -> (
    None
):
    waiting, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def authorize() -> bool:
        nonlocal calls
        calls += 1
        if calls == 3:
            waiting.set()
            await release.wait()
        return True

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.notifications(
            scopes=[NotificationScope("a")], authorize=authorize
        )
        body = stream.to_sse()
        pull = asyncio.create_task(anext(body))
        await waiting.wait()
        await stream.aclose()
        release.set()
        with pytest.raises(StopAsyncIteration):
            await pull
        ready_stream = await gateway.notifications(scopes=[NotificationScope("b")])
        ready_body = ready_stream.to_sse()
        assert b"event: ready" in await anext(ready_body)
        await ready_stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(ready_body)
        unused = await gateway.notifications(
            scopes=[NotificationScope("c")],
            authorize=authorize,
        )
        unused_body = unused.to_sse()
        await unused.aclose()
        accepted_checks = calls
        with pytest.raises(StopAsyncIteration):
            await anext(unused_body)
        assert calls == accepted_checks


@pytest.mark.parametrize("pause_after_ready", [False, True])
async def test_authorization_deadline_is_rechecked_across_slow_sends(
    monkeypatch: pytest.MonkeyPatch,
    pause_after_ready: bool,
) -> None:
    clock = 0.0
    allowed = True
    checks: list[bool] = []

    async def authorize() -> bool:
        checks.append(allowed)
        return allowed

    async def both_ready(
        tasks: Collection[asyncio.Task[Notification | ResyncRequired]],
        timeout: float,
    ) -> set[asyncio.Task[Notification | ResyncRequired]]:
        await asyncio.gather(*tasks)
        return set(tasks)

    monkeypatch.setattr("tinkerfin_gateway.notifications._monotonic", lambda: clock)
    monkeypatch.setattr("tinkerfin_gateway.notifications._wait_for_changes", both_ready)
    a, b = NotificationScope("a"), NotificationScope("b")
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        async with await gateway.notifications(
            scopes=[a, b], authorize=authorize
        ) as stream:
            body = stream.to_sse()
            assert b"event: ready" in await anext(body)
            await notifications.publish(change("a", a))
            await notifications.publish(change("b", b))
            if not pause_after_ready:
                assert b"event: change" in await anext(body)
            clock, allowed = 20.0, False
            with pytest.raises(StopAsyncIteration):
                await anext(body)
            assert checks[-1] is False


async def test_same_send_and_cleanup_failure_preserves_original_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = OSError("original cause")
    failure = ValueError("send and cleanup failure")
    failure.__cause__ = original
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.notifications(scopes=[NotificationScope("a")])
        close_stream = stream.aclose

        async def failed_close() -> None:
            raise failure

        async def send(_message: Message) -> None:
            raise failure

        async def receive() -> Message:
            return {"type": "http.disconnect"}

        monkeypatch.setattr(stream, "aclose", failed_close)
        response = await sse_response(stream)
        try:
            with pytest.raises(ValueError) as caught:
                await response(
                    {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
                )
            assert caught.value is failure
            assert failure.__cause__ is original
        finally:
            await close_stream()


def test_gateway_failures_copy_public_context_and_preserve_trusted_causes() -> None:
    context = {"operation": "subscribe"}
    original = RuntimeError("private diagnostic")
    error = GatewayClosed("Stream closed", context=context, cause=original)
    context["operation"] = "changed"
    assert isinstance(error, GatewayError)
    assert error.code is GatewayErrorCode.CLOSED
    assert error.context == {"operation": "subscribe"}
    assert error.__cause__ is original
    assert "private" not in str(error)
    with pytest.raises(TypeError):
        exec("context['operation'] = 'changed'", {"context": error.context})
