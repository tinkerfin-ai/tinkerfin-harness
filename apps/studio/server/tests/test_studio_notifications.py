"""登录范围、资源提交与浏览器通知的联合契约"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import event
from starlette.requests import Request
from test_attachments import png

import tinkerfin_gateway.notifications as gateway_notifications
from tinkerfin_gateway import Gateway
from tinkerfin_messaging import Messaging
from tinkerfin_notifications import Notification, NotificationScope
from tinkerfin_studio.api.errors import BusinessException, GlobalErrorCode
from tinkerfin_studio.api.notification_router import follow_notifications
from tinkerfin_studio.attachments.service import AttachmentService, byte_chunks
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.repository import RedisTokenRepository, TokenRecord
from tinkerfin_studio.auth.types import AuthenticatedSession, UserContext
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.titles import summarize_conversation_title


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


async def test_notification_feed_uses_only_authenticated_scopes_without_holding_sql(
    notification_route, notifications
):
    request, auth, _clock, _records, connections = notification_route
    response = await follow_notifications(request, auth, "project-7")
    chunks = aiter(response.body_iterator)
    try:
        assert await anext(chunks) == b"event: ready\ndata: {}\n\n"
        assert connections == [0]
        for scope, key in (
            (NotificationScope("ns_99"), "foreign-user"),
            (NotificationScope("studio_automation", "99"), "foreign-owner"),
            (
                NotificationScope("studio_automation", "7:another-project"),
                "other-project",
            ),
            (NotificationScope("studio_automation"), "unowned"),
            (NotificationScope("ns_7"), "thread"),
            (NotificationScope("studio_automation", "7:project-7"), "execution"),
        ):
            await notifications.publish(
                Notification(scope=scope, topic="test.changed", key=key)
            )
        changes = []
        for _ in range(2):
            frame = await anext(chunks)
            assert isinstance(frame, bytes)
            changes.append(json.loads(frame.split(b"data: ", 1)[1]))
            assert connections == [0]
        assert {item["key"] for item in changes} == {"thread", "execution"}
    finally:
        await response.aclose()
    assert connections == [0]


@pytest.mark.parametrize("reason", ["revoked", "expired"])
async def test_notification_feed_stops_at_revocation_or_fixed_expiry(
    notification_route, reason
):
    request, auth, clock, records, connections = notification_route
    response = await follow_notifications(request, auth)
    chunks = aiter(response.body_iterator)
    try:
        assert await anext(chunks) == b"event: ready\ndata: {}\n\n"
        if reason == "revoked":
            records[auth.token] = records[auth.token].with_revoked(True)
            clock.monotonic = 15.0
        else:
            clock.now = auth.expires_at
        with pytest.raises(StopAsyncIteration):
            await anext(chunks)
    finally:
        await response.aclose()
    assert connections == [0]


async def test_notification_preflight_rejects_a_revoked_session(notification_route):
    request, auth, _clock, records, connections = notification_route
    records[auth.token] = records[auth.token].with_revoked(True)
    with pytest.raises(BusinessException) as failure:
        await follow_notifications(request, auth)
    assert failure.value.error_code is GlobalErrorCode.UNAUTHORIZED
    assert connections == [0]


async def test_title_claim_and_saved_title_notify_only_committed_sequence(
    database, notifications
):
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            project_id="project-1",
            user_id=1,
            thread_id="title",
            title="临时",
            model_id=None,
        )
        await repository.commit()
        thread_pk = thread.id
    release = asyncio.Event()

    async def invoke(*_args, **_kwargs):
        await release.wait()
        return AIMessage(content="分析结果")

    model = create_autospec(BaseChatModel, instance=True)
    model.ainvoke.side_effect = invoke
    async with notifications.subscribe(scope=NotificationScope("ns_1")) as changes:
        task = asyncio.create_task(
            summarize_conversation_title(
                database=database,
                notifications=notifications,
                thread_pk=thread_pk,
                text="保密的模型输入",
                model=model,
            )
        )
        try:
            running = await anext(changes)
            assert isinstance(running, Notification)
            assert running.topic == "studio.conversation.title.changed"
            assert running.details == {"title_seq": 1}
            async with database.session() as session:
                saved = await ConversationRepository(session).get_thread_by_pk(
                    thread_pk
                )
                assert saved is not None and saved.title_generation_status == "running"
            release.set()
            result = await task
            completed = await anext(changes)
            assert isinstance(completed, Notification)
            assert completed.details == {"title_seq": 2}
            assert result is not None and result.title_seq == 2
            assert "保密" not in completed.model_dump_json()
            assert "分析结果" not in completed.model_dump_json()
        finally:
            release.set()
            await task


async def test_late_execution_attachment_notifies_its_collection(
    attachments, notifications, database, attachment_storage
):
    # 服务重开后仍按已保存集合发布迟到文件，不依赖运行仍然活跃
    async with notifications.subscribe(
        scope=NotificationScope("ns_1"), key="execution"
    ) as changes:
        await attachments.create_collection(
            project_id="project-1",
            user_id=1,
            collection_id="execution",
            purpose="execution",
            attachment_ids=(),
            configuration={},
        )
        created = await anext(changes)
        assert isinstance(created, Notification)
        assert created.details == {"collection_id": "execution"}
        reopened = AttachmentService(
            database, attachment_storage, notifications=notifications
        )
        file = await reopened.upload(
            project_id="project-1",
            user_id=1,
            name="private.png",
            chunks=byte_chunks(png()),
            collection_id="execution",
            source="tool",
        )
        change = await anext(changes)
        assert isinstance(change, Notification)
        assert change.topic == "studio.attachments.changed"
        assert change.details == {
            "collection_id": "execution",
            "attachment_id": file.id,
        }
        assert [
            item.id
            for item in await attachments.list_collection(
                user_id=1, collection_id="execution"
            )
        ] == [file.id]
        assert "private.png" not in change.model_dump_json()


pytestmark = pytest.mark.usefixtures("projects")
