"""在测试独占的 MySQL 库验证新会话事务及通知可读时机"""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from docker import DockerClient
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
from tinkerfin_studio.api.errors import BusinessException, ProjectErrorCode
from tinkerfin_studio.attachments.entity import AttachmentFile
from tinkerfin_studio.attachments.service import byte_chunks
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.run_preparation import (
    classify_intent,
    prepare_run_request,
)
from tinkerfin_studio.conversation.run_registration import ConversationRunPreparer
from tinkerfin_studio.infrastructure.database import Base, Database
from tinkerfin_studio.projects.repository import ProjectRepository

pytestmark = [pytest.mark.studio_mysql_integration, pytest.mark.usefixtures("projects")]


@pytest.fixture(autouse=True)
def record_owned_mysql_resources(
    mysql_test_service,
    docker_test_client: DockerClient,
    docker_test_run_id: str,
    record_property,
) -> None:
    """在同步准备阶段记录独占容器和匿名卷，供清理结果精确核对

    Docker SDK 仅提供同步查询，此处在异步用例启动前串行调用，沿用客户端
    配置的请求超时（默认 60 秒）。查询期间不响应异步取消，不占用数据库连接。
    此处不创建资源或后台任务；容器和卷仍由测试客户端的最终清理负责。

    Args:
        mysql_test_service: 已启动的测试专用 MySQL
        docker_test_client: 同步准备阶段借用的 Docker 客户端
        docker_test_run_id: 当前测试进程独占的资源标签
        record_property: 将资源身份写入测试结果的记录入口
    """
    del mysql_test_service
    owned = docker_test_client.containers.list(
        all=True, filters={"label": f"tinkerfin.test/run={docker_test_run_id}"}
    )
    record_property("docker_test_run_id", docker_test_run_id)
    container_ids: list[str] = []
    for item in owned:
        identity = item.id
        assert identity is not None
        container_ids.append(identity)
    record_property("docker_container_ids", ",".join(container_ids))
    record_property(
        "docker_volume_names",
        ",".join(
            mount["Name"]
            for item in owned
            for mount in item.attrs["Mounts"]
            if mount["Type"] == "volume"
        ),
    )


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
            project_id="project-1", thread_id="", run_id=request.run_id, intent=intent
        )
        thread_id = resolved.thread.thread_id
        prepared = prepare_run_request(
            request, project_id="project-1", user_id=1, thread_id=thread_id
        )
        event.listen(database.engine.sync_engine, "before_cursor_execute", inserting)
        joining = asyncio.create_task(
            contender.resolve_thread(
                project_id="project-1",
                thread_id="",
                run_id=request.run_id,
                intent=intent,
            )
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


@pytest.mark.parametrize("organize", ["move", "archive"])
async def test_organizing_reads_preparing_registration_after_waiting_for_thread_lock(
    database,
    session,
    notifications,
    attachments,
    organize,
    docker_test_run_id,
    record_property,
):
    """整理请求的旧快照不能隐藏在会话锁之前提交的运行登记"""
    record_property("docker_test_run_id", docker_test_run_id)
    record_property("database", database.engine.url.database)
    repository = ConversationRepository(session)
    destination = await ProjectRepository(session, 1).create("目标项目")
    thread = await repository.create_thread(
        user_id=1,
        project_id="project-1",
        thread_id="organizing",
        title="会话",
        model_id="model-main",
    )
    await session.commit()
    waiting = asyncio.Event()

    def locking(_connection, _cursor, statement, _parameters, _context, _executemany):
        if (
            statement.startswith("SELECT conversation_threads.")
            and "FOR UPDATE" in statement
        ):
            waiting.set()

    async with database.session() as writing, database.session() as organizing:
        assert (
            await organizing.scalar(text("SELECT @@transaction_isolation"))
            == "REPEATABLE-READ"
        )
        writer = ConversationRepository(writing)
        locked = await writer.lock_thread(thread.id)
        assert locked is not None
        organizer = ConversationRepository(organizing)
        observed = await organizer.get_thread(user_id=1, thread_id=thread.thread_id)
        assert observed is not None and observed.status == "idle"
        event.listen(database.engine.sync_engine, "before_cursor_execute", locking)
        pending = asyncio.create_task(
            organizer.organize_thread(
                user_id=1,
                thread_id=thread.thread_id,
                project_id=destination.id if organize == "move" else None,
                archived=True if organize == "archive" else None,
            )
        )
        try:
            await waiting.wait()
            request = _ordinary_request(thread_id=thread.thread_id, run_id="preparing")
            await ConversationRunPreparer(
                writing, user_id=1, attachments=attachments, notifications=notifications
            ).register(
                intent=classify_intent(request),
                prepared=prepare_run_request(
                    request,
                    project_id="project-1",
                    user_id=1,
                    thread_id=thread.thread_id,
                ),
                model=_model(),
                thread=locked,
            )
            with pytest.raises(BusinessException) as denied:
                await pending
            assert denied.value.error_code == ProjectErrorCode.ACTIVE_CONVERSATION
        finally:
            await writing.rollback()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await organizing.rollback()
            event.remove(database.engine.sync_engine, "before_cursor_execute", locking)
    async with database.session() as check:
        current = await ConversationRepository(check).get_thread(
            user_id=1, thread_id=thread.thread_id
        )
        assert (
            current is not None
            and current.project_id == "project-1"
            and not current.archived
        )


@pytest.mark.parametrize("retained", [False, True])
async def test_collection_creation_checks_current_file_project_after_move(
    database, session, attachments, retained
):
    """移动先取得附件锁时，只有已有输入引用可以继续在原项目使用文件"""
    destination = await ProjectRepository(session, 1).create("目标项目")
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1,
        project_id="project-1",
        thread_id="moving-file",
        title="会话",
        model_id="model-main",
    )
    await session.commit()
    file = await attachments.upload(
        user_id=1,
        thread_id=thread.thread_id,
        name="reference.md",
        chunks=byte_chunks(b"# fixed"),
    )
    if retained:
        await attachments.create_collection(
            user_id=1,
            project_id="project-1",
            collection_id="input",
            purpose="input",
            attachment_ids=(file.id,),
            configuration={},
        )
    reached = asyncio.Event()

    def locking(_connection, _cursor, statement, _parameters, _context, _executemany):
        if (
            statement.startswith("SELECT conversation_attachments.")
            and "FOR UPDATE" in statement
        ):
            reached.set()

    async with database.session() as moving:
        await ConversationRepository(moving).organize_thread(
            user_id=1,
            thread_id=thread.thread_id,
            project_id=destination.id,
            archived=None,
        )
        event.listen(database.engine.sync_engine, "before_cursor_execute", locking)
        pending = asyncio.create_task(
            attachments.create_collection(
                user_id=1,
                project_id="project-1",
                collection_id="execution",
                purpose="execution",
                attachment_ids=(file.id,),
                configuration={},
                source_collection_id="input" if retained else None,
            )
        )
        try:
            await reached.wait()
            await moving.commit()
            if retained:
                await pending
                assert [
                    item.id
                    for item in await attachments.list_collection(
                        user_id=1, collection_id="execution"
                    )
                ] == [file.id]
            else:
                with pytest.raises(BusinessException):
                    await pending
        finally:
            await moving.rollback()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            event.remove(database.engine.sync_engine, "before_cursor_execute", locking)
    async with database.session() as check:
        current = await check.get(AttachmentFile, file.id)
        assert current is not None and current.project_id == destination.id


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
