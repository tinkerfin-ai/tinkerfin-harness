"""Memory and isolated SQLite backends for shared Messaging contracts."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from backend_harness import MessagingBackendHarness
from sql_messaging_support import messaging_engine
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_messaging import MemoryBackend, SqlAlchemyBackend


@pytest.fixture(
    params=(pytest.param("memory", id="memory"), pytest.param("sqlite", id="sqlite"))
)
async def messaging_backend(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncGenerator[MessagingBackendHarness, None]:
    """Yield an isolated backend for the shared runtime contract."""
    if request.param == "memory":
        yield MessagingBackendHarness(MemoryBackend())
        return
    async with messaging_engine(tmp_path) as engine:
        yield MessagingBackendHarness(SqlAlchemyBackend(engine))


@pytest.fixture(params=["sqlite"])
async def messaging_sql_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    async with messaging_engine(tmp_path) as engine:
        yield engine
