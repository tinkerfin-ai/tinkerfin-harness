"""Isolated SQLite databases for the Trace storage contract."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@pytest.fixture(params=["sqlite"])
async def trace_sql_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    instance = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'traces.db'}", pool_size=4, max_overflow=0
    )
    try:
        async with instance.begin() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        yield instance
    finally:
        await instance.dispose()
