"""Offline conversion preserves the source, history and command identities."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from scripts.prepare_automation_database import prepare_database
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.support.docker_services import MySQLTestService
from tinkerfin_automation import (
    AutomationService,
    OnceSchedule,
    SqlAlchemyAutomationStore,
)
from tinkerfin_studio.application import create_application
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.infrastructure.database import Base


@pytest.fixture(
    params=["sqlite", pytest.param("mysql", marks=pytest.mark.docker_integration)]
)
async def conversion_databases(request, tmp_path):
    if request.param == "sqlite":
        source = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
        destination = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'destination.db'}"
        )
        try:
            yield source, destination
        finally:
            await destination.dispose()
            await source.dispose()
        return
    service = request.getfixturevalue("mysql_test_service")
    assert isinstance(service, MySQLTestService)
    names = [f"conversion_test_{uuid4().hex}" for _ in range(2)]
    admin = create_async_engine(service.url("tinkerfin_test_admin"))
    source, destination = [create_async_engine(service.url(name)) for name in names]
    try:
        async with admin.begin() as connection:
            for name in names:
                await connection.execute(
                    text(
                        f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
        yield source, destination
    finally:
        await destination.dispose()
        await source.dispose()
        try:
            async with admin.begin() as connection:
                for name in names:
                    await connection.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
        finally:
            await admin.dispose()


async def test_conversion_copies_into_empty_database_and_preserves_original_permissions(
    conversion_databases,
):
    create_application(lifespan=None)
    source, destination = conversion_databases
    source_store = SqlAlchemyAutomationStore(source)
    destination_store = SqlAlchemyAutomationStore(destination)
    schedule = OnceSchedule(at=datetime.now(UTC) + timedelta(days=1))
    try:
        async with source.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(source, expire_on_commit=False) as session:
            repository = ConversationRepository(session)
            thread = await repository.create_thread(
                user_id=1, thread_id="thread", title="History", model_id="main"
            )
            await repository.create_run_registration(
                thread_id=thread.id,
                run_id="run",
                parent_run_id=None,
                model_id="main",
                input_json={
                    "forwardedProps": {"model": "main", "command": {"plan": "off"}}
                },
            )
            thread.last_run_id = "run"
            registration = await repository.get_run(thread_pk=thread.id, run_id="run")
            assert registration is not None
            registration.status = "waiting"
            await session.commit()
        async with AutomationService(namespace="app", store=source_store) as service:
            task = await service.create_task(
                owner_id="1",
                name="Straße",
                schedule=schedule,
                target="summary",
                request_id="create",
            )
            execution = await service.run_task_now(
                owner_id="1", task_id=task.task_id, request_id="run"
            )
        async with source.begin() as connection:
            for table, field in [
                ("conversation_threads", "last_access_mode"),
                ("conversation_run_registrations", "access_mode"),
                ("tinkerfin_automation_tasks", "search_name"),
                ("tinkerfin_automation_runs", "task_name"),
                ("tinkerfin_automation_runs", "search_name"),
            ]:
                quote = connection.dialect.identifier_preparer.quote
                await connection.exec_driver_sql(
                    f"ALTER TABLE {quote(table)} DROP COLUMN {quote(field)}"
                )
            for table, column in [
                ("tinkerfin_automation_tasks", "payload"),
                ("tinkerfin_automation_runs", "payload"),
                ("tinkerfin_automation_operations", "result_payload"),
            ]:
                rows = (
                    (await connection.execute(text(f"SELECT {column} FROM {table}")))
                    .scalars()
                    .all()
                )
                for raw in rows:
                    value = json.loads(raw)
                    if "schedule" in value:
                        value["schedule"].pop("active_from")
                        value["schedule"].pop("active_until")
                    value.pop("task_name", None)
                    await connection.execute(
                        text(
                            f"UPDATE {table} SET {column}=:updated WHERE {column}=:original"
                        ),
                        {"updated": json.dumps(value), "original": raw},
                    )
        preview = await prepare_database(source, destination)
        assert preview["conversation_run_registrations"] == 1
        async with destination.connect() as connection:
            assert (
                await connection.run_sync(lambda sync: inspect(sync).get_table_names())
                == []
            )
        assert await prepare_database(source, destination, apply=True) == preview
        await destination_store.setup()
        async with AutomationService(
            namespace="app", store=destination_store
        ) as restored:
            repeated = await restored.create_task(
                owner_id="1",
                name="Straße",
                schedule=schedule,
                target="summary",
                request_id="create",
            )
            assert repeated.task_id == task.task_id
            assert (
                await restored.get_execution(
                    owner_id="1", execution_id=execution.execution_id
                )
            ).task_name is None
        async with destination.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT access_mode, input_json FROM conversation_run_registrations"
                    )
                )
            ).one()
            assert row.access_mode == "write_approval"
            assert (
                json.loads(row.input_json)["forwardedProps"]["accessMode"]
                == "write_approval"
            )
        async with source.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: inspect(sync).get_columns("conversation_run_registrations")
            )
            assert "access_mode" not in {column["name"] for column in columns}
        with pytest.raises(ValueError, match="empty"):
            await prepare_database(source, destination, apply=True)
        with pytest.raises(ValueError, match="Source and destination"):
            await prepare_database(source, source, apply=True)
    finally:
        await destination_store.close()
        await source_store.close()
        await destination.dispose()
        await source.dispose()
