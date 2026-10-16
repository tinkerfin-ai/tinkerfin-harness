"""Control database clock results and observe real row-lock waits."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tinkerfin_automation.clock import ManualClock


def control_database_clock(
    engine: AsyncEngine, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_scalar = AsyncConnection.scalar

    async def scalar(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        # Only the clock result changes; real domain queries, transactions, locks,
        # and driver result consumption continue through SQLAlchemy.
        value = await original_scalar(connection, statement, *args, **kwargs)
        if connection.engine is engine and any(
            name in str(statement)
            for name in ("utc_timestamp(", "clock_timestamp(", "strftime(")
        ):
            return clock.now().replace(tzinfo=None)
        return value

    monkeypatch.setattr(AsyncConnection, "scalar", scalar)


async def wait_for_lock(
    engine: AsyncEngine, identity: int, pending: asyncio.Task[object]
) -> None:
    async with asyncio.timeout(10), engine.connect() as connection:
        while True:
            if engine.dialect.name == "postgresql":
                await connection.execute(text("SELECT pg_stat_clear_snapshot()"))
                statement = text(
                    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :identity"
                )
            else:
                statement = text(
                    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS waits "
                    "JOIN performance_schema.threads AS threads "
                    "ON threads.THREAD_ID = waits.REQUESTING_THREAD_ID "
                    "WHERE threads.PROCESSLIST_ID = :identity"
                )
            if await connection.scalar(statement, {"identity": identity}):
                return
            if pending.done():
                await pending
                raise AssertionError(
                    "Operation exited before encountering the held row"
                )
            await connection.rollback()
