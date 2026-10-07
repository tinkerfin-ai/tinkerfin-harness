from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.infrastructure.database import Base, Database


@pytest.fixture(
    params=("sqlite", pytest.param("mysql", marks=pytest.mark.docker_integration))
)
async def summary_database(
    request: pytest.FixtureRequest, database: Database
) -> AsyncIterator[Database]:
    """仅在测试自有数据库验证摘要事务和微秒顺序"""
    if request.param == "sqlite":
        yield database
        return
    admin_url = request.getfixturevalue("mysql_admin_url")
    assert isinstance(admin_url, str)
    name = f"tinkerfin_summary_{uuid4().hex}"
    admin = create_async_engine(admin_url)
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"
            )
        url = make_url(admin_url).set(database=name)
        async with Database(url.render_as_string(hide_password=False)) as resource:
            async with resource.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            yield resource
    finally:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        await admin.dispose()


async def test_summary_order_preserves_microseconds_rejects_conflicts_and_allows_recovery(
    summary_database: Database,
) -> None:
    """同前缀按存储观测排序，冲突需新观测，失活状态可随新证据恢复"""
    async with summary_database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            project_id="project-1",
            user_id=1,
            thread_id="ordered",
            title="摘要顺序",
            model_id="main",
        )
        registration = await repository.create_run_registration(
            thread_id=thread.id,
            run_id="run",
            parent_run_id=None,
            model_id="main",
            input_json={"runId": "run"},
        )
        assert registration.trace_generation is None
        assert registration.trace_as_of_seq is None
        assert registration.trace_observed_at is None
        thread_pk = thread.id
        await repository.commit()

    first = datetime(2026, 9, 5, microsecond=1)
    second = first + timedelta(microseconds=1)

    async def write(
        *,
        stamp: datetime,
        seq: int = 10,
        status: str = "running",
        count: int = 2,
        generation: str = "generation",
    ) -> str:
        async with summary_database.session() as session:
            repository = ConversationRepository(session)
            result = await repository.update_trace_summary(
                thread_pk=thread_pk,
                run_id="run",
                status=status,
                message_count=count,
                tool_call_count=1,
                has_pending_interrupt=False,
                pending_interaction_kind=None,
                terminal_outcome=None,
                updated_at=datetime(2026, 9, 4),
                trace_generation=generation,
                trace_as_of_seq=seq,
                trace_observed_at=stamp,
            )
            await repository.commit()
            return result.status

    assert await write(stamp=first) == "applied"
    assert await write(stamp=second, status="error") == "applied"
    assert await write(stamp=first) == "stale"
    assert await write(stamp=second, seq=9) == "stale"
    assert await write(stamp=second, status="error") == "applied"
    assert await write(stamp=second) == "ambiguous"
    assert await write(stamp=second, status="error", count=3) == "ambiguous"
    assert await write(stamp=second, generation="unrelated") == "generation_conflict"
    async with summary_database.session() as session:
        repository = ConversationRepository(session)
        registration = await repository.get_run(thread_pk=thread_pk, run_id="run")
        thread = await repository.get_thread_by_pk(thread_pk)
        assert registration is not None and thread is not None
        assert registration.trace_observed_at == second
        assert registration.trace_as_of_seq == 10
        assert registration.trace_generation == "generation"
        assert registration.finished_at is None
        assert registration.terminal_outcome is None
        assert thread.status == "error" and thread.message_count == 2
    assert await write(stamp=second + timedelta(microseconds=1)) == "applied"
    async with summary_database.session() as session:
        thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
        assert thread is not None and thread.status == "running"


async def test_summary_observation_schema_preserves_fractional_seconds(
    summary_database: Database,
) -> None:
    """数据库列必须保留观测顺序所需的完整微秒"""
    async with summary_database.engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("conversation_run_registrations")
        )
    actual = {column["name"]: column for column in columns}
    for name in ("trace_generation", "trace_as_of_seq", "trace_observed_at"):
        assert actual[name]["nullable"] is True
    if summary_database.engine.dialect.name == "mysql":
        observation_type = actual["trace_observed_at"]["type"]
        assert isinstance(observation_type, DATETIME)
        assert observation_type.fsp == 6


pytestmark = pytest.mark.usefixtures("projects")
