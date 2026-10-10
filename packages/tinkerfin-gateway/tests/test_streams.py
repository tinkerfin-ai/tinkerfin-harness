"""SSE preflight, scope isolation, authorization, and response ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from tinkerfin_gateway import Gateway, GatewayClosed
from tinkerfin_gateway.starlette import sse_response
from tinkerfin_messaging import Messaging
from tinkerfin_notifications import (
    Notification,
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


async def test_missing_scope_is_rejected_instead_of_opening_global_access() -> None:
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        # Exercise untyped HTTP integration input without claiming it is valid.
        invalid: object = [None]
        with pytest.raises(TypeError):
            await gateway.notifications(
                scopes=cast(Collection[NotificationScope], invalid)
            )
