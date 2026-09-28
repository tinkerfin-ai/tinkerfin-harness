"""在测试独占的 MySQL 库验证新会话事务及通知可读时机"""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.types import Message
from test_chat_trace_integration import _model, _ordinary_request
from test_chat_trace_integration import (
    stored_model_configs as stored_model_configs,
)
from test_chat_trace_integration import (
    test_conversation_is_announced_only_when_independent_history_can_read_it as verify_readable_notification,
)
from test_chat_trace_integration import (
    test_rejected_first_registration_rolls_back_its_new_conversation as verify_registration_rollback,
)
from test_conversation_history import (
    test_preparing_conversations_do_not_fill_history_pages as verify_history_pages,
)

from tinkerfin_gateway import Gateway
from tinkerfin_gateway.starlette import sse_response
from tinkerfin_messaging import Messaging
from tinkerfin_notifications import NotificationScope
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.conversation.run_preparation import (
    classify_intent,
    prepare_run_request,
)
from tinkerfin_studio.conversation.run_registration import ConversationRunPreparer
from tinkerfin_studio.infrastructure.database import Base, Database

pytestmark = pytest.mark.studio_mysql_integration


@pytest_asyncio.fixture
async def database(mysql_admin_url: str) -> AsyncIterator[Database]:
    """只创建与删除当前测试随机命名的专用数据库"""
    name = f"tinkerfin_admission_{uuid4().hex}"
    admin_url = make_url(mysql_admin_url)
    admin = create_async_engine(admin_url)
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f"CREATE DATABASE `{name}`")
        url = admin_url.set(database=name).render_as_string(hide_password=False)
        async with Database(url) as resource:
            async with resource.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            yield resource
    finally:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        await admin.dispose()


@pytest.mark.parametrize("caller", ["preparer", "service"])
async def test_first_registration_rollback(
    notifications, database, session, attachments, monkeypatch, caller
):
    await verify_registration_rollback(
        notifications, database, session, attachments, monkeypatch, caller
    )


async def test_notification_history_readiness(
    notifications, database, session, attachments
):
    await verify_readable_notification(notifications, database, session, attachments)


@pytest.mark.parametrize("query", [None, "Trace"])
async def test_preparing_records_do_not_occupy_history_pages(session, query):
    await verify_history_pages(session, query, pinned=True)


@pytest.mark.parametrize("commit_first", [False, True])
async def test_simultaneous_same_run_keeps_one_thread_and_registration(
    database, notifications, attachments, commit_first
):
    """相同运行的并发插入在提交或回滚后都只登记一份会话"""
    insert_entered = asyncio.Event()
    request = _ordinary_request()
    intent = classify_intent(request)

    def inserting(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("INSERT INTO conversation_threads"):
            insert_entered.set()

    async with database.session() as first, database.session() as second:
        owner = ConversationRunPreparer(
            first, user_id=1, attachments=attachments, notifications=notifications
        )
        contender = ConversationRunPreparer(
            second, user_id=1, attachments=attachments, notifications=notifications
        )
        resolved = await owner.resolve_thread(
            thread_id="", run_id=request.run_id, intent=intent
        )
        thread_id = resolved.thread.thread_id
        prepared = prepare_run_request(request, user_id=1, thread_id=thread_id)
        event.listen(database.engine.sync_engine, "before_cursor_execute", inserting)
        joining = asyncio.create_task(
            contender.resolve_thread(thread_id="", run_id=request.run_id, intent=intent)
        )
        try:
            await insert_entered.wait()
            if commit_first:
                await owner.register(
                    intent=intent,
                    prepared=prepared,
                    model=_model(),
                    thread=resolved.thread,
                    thread_created=True,
                )
            else:
                await first.rollback()
            shared = await joining
            assert shared.thread.thread_id == thread_id
            assert shared.created is not commit_first
            accepted = await contender.register(
                intent=intent,
                prepared=prepared,
                model=_model(),
                thread=shared.thread,
                thread_created=shared.created,
            )
            assert accepted.registered.created is not commit_first
        finally:
            await first.rollback()
            if not joining.done():
                joining.cancel()
            await asyncio.gather(joining, return_exceptions=True)
            event.remove(
                database.engine.sync_engine, "before_cursor_execute", inserting
            )
    async with database.session() as check:
        assert len(list(await check.scalars(select(ConversationThread)))) == 1
        assert len(list(await check.scalars(select(ConversationRunRegistration)))) == 1


async def test_notification_disconnect_returns_borrowed_mysql_connection(
    database, notifications
):
    """断连完成时授权检查已归还借用连接，共享数据库仍可使用"""
    entered = asyncio.Event()
    checked_out = 0
    checks = 0

    def checkout(*_args):
        nonlocal checked_out
        checked_out += 1

    def checkin(*_args):
        nonlocal checked_out
        checked_out -= 1

    async def authorize() -> bool:
        nonlocal checks
        checks += 1
        if checks < 3:
            return True
        async with database.session() as borrowed:
            assert await borrowed.scalar(text("SELECT 1")) == 1
            entered.set()
            await asyncio.Event().wait()
        return True

    async def receive() -> Message:
        await entered.wait()
        assert checked_out == 1
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        pass

    event.listen(database.engine.sync_engine, "checkout", checkout)
    event.listen(database.engine.sync_engine, "checkin", checkin)
    try:
        async with Messaging() as messaging:
            gateway = Gateway(messaging=messaging, notifications=notifications)
            response = await sse_response(
                gateway.notifications(
                    scopes=[NotificationScope("ns_1")], authorize=authorize
                )
            )
            await response(
                {"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send
            )
            assert checked_out == 0
            async with database.session() as borrowed:
                assert await borrowed.scalar(text("SELECT 1")) == 1
            assert checked_out == 0
    finally:
        event.remove(database.engine.sync_engine, "checkout", checkout)
        event.remove(database.engine.sync_engine, "checkin", checkin)
