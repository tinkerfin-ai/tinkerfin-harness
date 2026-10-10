"""Own the Engines borrowed by Automation SQL tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sql_test_support import control_database_clock
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin_automation import MemoryAutomationStore, SqlAlchemyAutomationStore
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore
from tinkerfin_notifications import Notifications


@pytest.fixture
async def notification_service() -> AsyncGenerator[Notifications, None]:
    async with Notifications() as service:
        yield service


@pytest.fixture(params=["sqlite"])
async def automation_sql_engine(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[AsyncEngine]:
    async with _sql_engine(tmp_path) as engine:
        yield engine


@asynccontextmanager
async def _sql_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    instance = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'automation.db'}", pool_size=4, max_overflow=0
    )
    try:
        async with instance.begin() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        yield instance
    finally:
        await instance.dispose()


_STORE_FAMILIES = ["memory", "sqlite"]


@pytest.fixture(params=_STORE_FAMILIES)
async def store_with_clock(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AutomationStore, ManualClock]]:
    async with _store_with_clock(
        request, tmp_path, monkeypatch, notifications=None
    ) as pair:
        yield pair


@pytest.fixture(params=_STORE_FAMILIES)
async def notifying_store_with_clock(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    notification_service: Notifications,
) -> AsyncIterator[tuple[AutomationStore, ManualClock]]:
    async with _store_with_clock(
        request, tmp_path, monkeypatch, notifications=notification_service
    ) as pair:
        yield pair


@asynccontextmanager
async def _store_with_clock(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    notifications: Notifications | None,
) -> AsyncGenerator[tuple[AutomationStore, ManualClock], None]:
    clock = ManualClock(datetime(2026, 9, 9, 8, tzinfo=UTC))
    if request.param == "memory":
        store = MemoryAutomationStore(clock=clock, notifications=notifications)
        try:
            yield store, clock
        finally:
            await store.close()
        return
    async with _sql_engine(tmp_path) as engine:
        sql_store = SqlAlchemyAutomationStore(engine, notifications=notifications)
        await sql_store.setup()
        control_database_clock(engine, clock, monkeypatch)
        try:
            yield sql_store, clock
        finally:
            await sql_store.close()
