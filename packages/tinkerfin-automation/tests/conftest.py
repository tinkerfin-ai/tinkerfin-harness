"""Own the Engines borrowed by Automation SQL tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sql_test_support import control_database_clock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.support.docker_services import (
    MySQLTestService,
    PostgreSQLTestService,
)
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_automation import MemoryAutomationStore, SqlAlchemyAutomationStore
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore
from tinkerfin_notifications import Notifications


@pytest.fixture
async def notification_service() -> AsyncGenerator[Notifications, None]:
    async with Notifications() as service:
        yield service


@pytest.fixture
async def sql_engine_factory() -> AsyncIterator[SqlEngineFactory]:
    factory = SqlEngineFactory()
    try:
        yield factory
    finally:
        await factory.aclose()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("mysql", marks=pytest.mark.docker_integration),
        pytest.param("postgresql", marks=pytest.mark.docker_integration),
    ]
)
async def automation_sql_engine(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[AsyncEngine]:
    async with _sql_engine(request.param, request, tmp_path) as engine:
        yield engine


@asynccontextmanager
async def _sql_engine(
    family: str, request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncGenerator[AsyncEngine]:
    if family == "sqlite":
        instance = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'automation.db'}",
            pool_size=4,
            max_overflow=0,
        )
        try:
            async with instance.begin() as connection:
                await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            yield instance
        finally:
            await instance.dispose()
        return
    name = f"automation_test_{uuid4().hex}"
    service = request.getfixturevalue(f"{family}_test_service")
    if isinstance(service, MySQLTestService):
        admin = create_async_engine(service.url("tinkerfin_test_admin"))
        async with admin.begin() as connection:
            await connection.execute(
                text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
            )
        instance = create_async_engine(
            service.url(name),
            pool_size=4,
            max_overflow=0,
            connect_args={"init_command": "SET time_zone = '+08:00'"},
        )
        cleanup = f"DROP DATABASE `{name}`"
    else:
        assert isinstance(service, PostgreSQLTestService)
        admin = create_async_engine(service.url())
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{name}"'))
        instance = create_async_engine(
            service.url(),
            pool_size=4,
            max_overflow=0,
            connect_args={
                "server_settings": {"search_path": name, "timezone": "Asia/Shanghai"}
            },
        )
        cleanup = f'DROP SCHEMA "{name}" CASCADE'
    try:
        yield instance
    finally:
        await instance.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(cleanup))
        await admin.dispose()


_STORE_FAMILIES = [
    "memory",
    "sqlite",
    pytest.param("mysql", marks=pytest.mark.docker_integration),
    pytest.param("postgresql", marks=pytest.mark.docker_integration),
]


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
    async with _sql_engine(request.param, request, tmp_path) as engine:
        sql_store = SqlAlchemyAutomationStore(engine, notifications=notifications)
        await sql_store.setup()
        control_database_clock(engine, clock, monkeypatch)
        try:
            yield sql_store, clock
        finally:
            await sql_store.close()
