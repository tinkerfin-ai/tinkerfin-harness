"""SSE preflight, scope isolation, authorization, and response ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from langchain_core.messages import AIMessage
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
