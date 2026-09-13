"""Current Automation DDL and live database reflection describe the same schema."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_automation import (
    AutomationStoreProtocolError,
    SqlAlchemyAutomationStore,
)
from tinkerfin_automation.sql_schema import (
    get_automation_store_schema,
    metadata,
    tasks,
)


async def test_offline_ddl_and_concurrent_setup_accept_the_same_schema(
    automation_sql_engine: AsyncEngine,
) -> None:
    engine = automation_sql_engine
    schema = get_automation_store_schema(engine.dialect.name)
    async with engine.begin() as connection:
        for statement in schema.statements:
            await connection.exec_driver_sql(statement)
    first, second = SqlAlchemyAutomationStore(engine), SqlAlchemyAutomationStore(engine)
    try:
        await asyncio.gather(first.setup(), second.setup(), first.setup())
        async with engine.connect() as connection:
            actual = await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )
            assert set(actual) == set(schema.table_names)
            if engine.dialect.name != "sqlite":
                for table in metadata.sorted_tables:
                    assert (
                        await connection.run_sync(
                            lambda sync: inspect(sync).get_table_comment(table.name)[
                                "text"
                            ]
                        )
                        == table.comment
                    )
    finally:
        await first.close()
        await second.close()


async def test_existing_timestamp_without_fractional_precision_is_rejected(
    automation_sql_engine: AsyncEngine,
) -> None:
    engine = automation_sql_engine
    if engine.dialect.name == "sqlite":
        pytest.skip("SQLite stores timestamps as text without a declaration precision")
    first = SqlAlchemyAutomationStore(engine)
    await first.setup()
    await first.close()
    async with engine.begin() as connection:
        if engine.dialect.name == "mysql":
            await connection.exec_driver_sql(
                "ALTER TABLE tinkerfin_automation_tasks MODIFY created_at DATETIME(0) "
                "NOT NULL COMMENT 'Database UTC creation time'"
            )
        else:
            await connection.exec_driver_sql(
                "ALTER TABLE tinkerfin_automation_tasks ALTER COLUMN created_at "
                "TYPE TIMESTAMP(0) WITHOUT TIME ZONE"
            )
    candidate = SqlAlchemyAutomationStore(engine)
    try:
        with pytest.raises(AutomationStoreProtocolError, match="stale type"):
            await candidate.setup()
    finally:
        await candidate.close()


async def test_changed_database_comment_is_rejected(
    automation_sql_engine: AsyncEngine,
) -> None:
    engine = automation_sql_engine
    if engine.dialect.name == "sqlite":
        pytest.skip("SQLite does not persist database comments")
    first = SqlAlchemyAutomationStore(engine)
    await first.setup()
    await first.close()
    async with engine.begin() as connection:
        if engine.dialect.name == "mysql":
            await connection.exec_driver_sql(
                "ALTER TABLE tinkerfin_automation_tasks COMMENT='Incorrect purpose'"
            )
        else:
            await connection.exec_driver_sql(
                "COMMENT ON TABLE tinkerfin_automation_tasks IS 'Incorrect purpose'"
            )
    candidate = SqlAlchemyAutomationStore(engine)
    try:
        with pytest.raises(AutomationStoreProtocolError, match="stale comment"):
            await candidate.setup()
    finally:
        await candidate.close()


async def test_clock_uses_database_utc_and_preserves_host_settings(
    automation_sql_engine: AsyncEngine,
) -> None:
    engine = automation_sql_engine
    store = SqlAlchemyAutomationStore(engine)
    await store.setup()
    if engine.dialect.name == "mysql":
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SET SESSION time_zone = '+08:00'")
    try:
        # Separate connections retain their host timezone while the Store returns
        # aware UTC. The endpoints bound one real operation without fixed sleeps.
        async with engine.connect() as connection:
            if engine.dialect.name == "mysql":
                statement = text("SELECT UTC_TIMESTAMP(6)")
                setting = "SELECT @@SESSION.time_zone"
            elif engine.dialect.name == "postgresql":
                statement = text("SELECT timezone('UTC', clock_timestamp())")
                setting = "SHOW TIME ZONE"
            else:
                statement = text("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")
                setting = "PRAGMA busy_timeout"
            before = await connection.scalar(statement)
            old_setting = await connection.exec_driver_sql(setting)
            expected_setting = old_setting.scalar_one()
            current = await store.current_time()
            after = await connection.scalar(statement)
            new_setting = await connection.exec_driver_sql(setting)
            assert new_setting.scalar_one() == expected_setting
        if isinstance(before, str):
            before = datetime.fromisoformat(before)
        if isinstance(after, str):
            after = datetime.fromisoformat(after)
        assert isinstance(before, datetime) and isinstance(after, datetime)
        assert before.replace(tzinfo=UTC) <= current <= after.replace(tzinfo=UTC)
        assert current.tzinfo is UTC
        async with engine.connect() as connection:
            assert (await connection.execute(select(tasks))).all() == []
    finally:
        await store.close()


async def test_mysql_host_collation_does_not_relax_declared_text_length(
    automation_sql_engine: AsyncEngine,
) -> None:
    engine = automation_sql_engine
    if engine.dialect.name != "mysql":
        pytest.skip("MySQL column collation reflection is dialect-specific")
    original = SqlAlchemyAutomationStore(engine)
    await original.setup()
    await original.close()
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "ALTER TABLE tinkerfin_automation_tasks MODIFY namespace "
            "VARCHAR(64) COLLATE utf8mb4_unicode_ci NOT NULL "
            "COMMENT 'Host-selected isolation namespace'"
        )
    candidate = SqlAlchemyAutomationStore(engine)
    try:
        with pytest.raises(AutomationStoreProtocolError, match="stale type"):
            await candidate.setup()
    finally:
        await candidate.close()
