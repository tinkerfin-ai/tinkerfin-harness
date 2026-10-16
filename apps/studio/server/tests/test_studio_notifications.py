"""登录范围、资源提交与浏览器通知的联合契约"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import event
from starlette.requests import Request

import tinkerfin_gateway.notifications as gateway_notifications
from tinkerfin_gateway import Gateway
from tinkerfin_messaging import Messaging
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.repository import RedisTokenRepository, TokenRecord
from tinkerfin_studio.auth.types import AuthenticatedSession, UserContext


@pytest.fixture
async def notification_route(database, notifications, monkeypatch):
    clock = SimpleNamespace(now=datetime(2030, 1, 1, tzinfo=UTC), monotonic=0.0)
    monkeypatch.setattr(gateway_notifications, "_now", lambda: clock.now)
    monkeypatch.setattr(gateway_notifications, "_monotonic", lambda: clock.monotonic)
    # 登录令牌在本场景始终未到期；连接的固定到期由受控时钟单独验证
    monkeypatch.setattr(TokenRecord, "is_expired", property(lambda self: False))
    auth = AuthenticatedSession(
        token="login-token",
        expires_at=clock.now + timedelta(hours=1),
        user=UserContext(7, "alice", (), False),
    )
    records = {auth.token: TokenRecord(auth.token, 7, auth.expires_at)}

    async def get_record(_repository, token):
        return records.get(token)

    monkeypatch.setattr(RedisTokenRepository, "get", get_record)
    async with database.session() as session:
        session.add(User(id=7, username="alice", password_hash="unused"))
        await session.commit()
    connections = [0]

    def checkout(*_args):
        connections[0] += 1

    def checkin(*_args):
        connections[0] -= 1

    event.listen(database.engine.sync_engine, "checkout", checkout)
    event.listen(database.engine.sync_engine, "checkin", checkin)
    async with Messaging() as messaging:
        app = FastAPI()
        app.state.resources = SimpleNamespace(
            gateway=Gateway(messaging=messaging, notifications=notifications),
            database=database,
            redis_runtime=object(),
            settings=SimpleNamespace(auth_token_expire_seconds=3600),
        )
        request = Request(
            {
                "type": "http",
                "app": app,
                "method": "GET",
                "path": "/api/notifications",
                "headers": [],
                "query_string": b"namespace=ns_99&owner_id=99",
            }
        )
        try:
            yield request, auth, clock, records, connections
        finally:
            event.remove(database.engine.sync_engine, "checkout", checkout)
            event.remove(database.engine.sync_engine, "checkin", checkin)


pytestmark = pytest.mark.usefixtures("projects")


async def test_notifications_cover_owned_projects_without_replaying_history(
    notification_route, notifications, database
):
    from tinkerfin_notifications import Notification, NotificationScope
    from tinkerfin_studio.api.notification_router import follow_notifications
    from tinkerfin_studio.projects.repository import ProjectRepository

    request, auth, clock, _records, connections = notification_route
    async with database.session() as session:
        first = await ProjectRepository(session, 7).create("第一项目")
        second = await ProjectRepository(session, 7).create("第二项目")
    await notifications.publish(
        Notification(
            scope=NotificationScope("studio_automation", f"7:{first.id}"),
            topic="automation.execution.changed",
            key="offline",
        )
    )
    response = await follow_notifications(request, auth)
    body = aiter(response.body_iterator)
    try:
        assert await anext(body) == b"event: ready\ndata: {}\n\n"
        await notifications.publish(
            Notification(
                scope=NotificationScope("studio_automation", "8:foreign"),
                topic="automation.execution.changed",
                key="foreign",
            )
        )
        for project in (first, second):
            await notifications.publish(
                Notification(
                    scope=NotificationScope("studio_automation", f"7:{project.id}"),
                    topic="automation.execution.changed",
                    key=project.id,
                )
            )
            frame = await anext(body)
            assert isinstance(frame, bytes)
            assert project.id.encode() in frame
            assert b"offline" not in frame and b"foreign" not in frame
            assert connections == [0]
        async with database.session() as session:
            await ProjectRepository(session, 7).create("新增项目")
        clock.monotonic = 15.0
        await notifications.publish(
            Notification(
                scope=NotificationScope("ns_7"),
                topic="studio.projects.changed",
                key="new",
            )
        )
        with pytest.raises(StopAsyncIteration):
            await anext(body)
    finally:
        await response.aclose()
    assert connections == [0]
