"""Isolated SQLite databases for the public Store contract."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin_langgraph_store import SqlAlchemyStore


@pytest.fixture(params=["sqlite"])
async def storage(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    instance = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}", pool_size=4, max_overflow=0
    )
    try:
        async with instance.begin() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        yield instance
    finally:
        await instance.dispose()


@pytest.fixture
async def engine(storage: AsyncEngine) -> AsyncEngine:
    return storage


@pytest.fixture
async def store(storage: AsyncEngine) -> AsyncIterator[SqlAlchemyStore]:
    instance = SqlAlchemyStore(storage)
    try:
        yield instance
    finally:
        await instance.aclose()
