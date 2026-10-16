"""Test-owned SQLite connections for Sandbox State contracts."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.support.sql_engines import SqlEngineFactory


@pytest.fixture(params=["sqlite"])
async def sandbox_sql_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    instance = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'state.db'}", pool_size=4, max_overflow=0
    )
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest.fixture
async def sql_engine() -> AsyncIterator[SqlEngineFactory]:
    factory = SqlEngineFactory()
    try:
        yield factory
    finally:
        await factory.aclose()
