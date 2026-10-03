"""Keep lease decisions current after real database row-lock waits."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import DateTime, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from tinkerfin_sandbox import (
    OpenSandboxOwnerClaim,
    OpenSandboxStateOwnershipError,
    SQLAlchemyOpenSandboxState,
)

_CASES = [
    "acquire_owner",
    "renew_owner",
    "bind_owner",
    "unbind_owner",
    "renew_warm",
    "publish_warm",
    "discard_warm",
    "renew_cleanup",
    "consume_warm",
    "availability",
    "register_holder",
]


async def _wait_for_lock(
    engine: AsyncEngine, identity: int, pending: asyncio.Task[object]
) -> None:
    async with asyncio.timeout(5), engine.connect() as connection:
        while True:
            if engine.dialect.name == "postgresql":
                await connection.execute(text("SELECT pg_stat_clear_snapshot()"))
                waiting = await connection.scalar(
                    text(
                        "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :identity"
                    ),
                    {"identity": identity},
                )
            else:
                waiting = await connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.innodb_trx "
                        "WHERE trx_mysql_thread_id = :identity AND trx_state = 'LOCK WAIT'"
                    ),
                    {"identity": identity},
                )
            if waiting:
                return
            if pending.done():
                await pending
                raise AssertionError(
                    "Operation finished before its blocked row became available"
                )
            await connection.rollback()


async def _check_clock(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    now = [datetime(2030, 1, 1)]
    monkeypatch.setattr(
        SQLAlchemyOpenSandboxState, "_now", staticmethod(lambda: now[0])
    )
    state = SQLAlchemyOpenSandboxState(engine=engine, namespace="clock", lease_ttl=60)
    await state.start(warm_pool_size=1)
    owner = await state.acquire_owner("owner")
    table = "tinkerfin_opensandbox_owners"
    operation: Awaitable[object]
    if case == "acquire_owner":
        await state.release_owner(owner)
        operation = state.acquire_owner("owner")
    elif case == "renew_owner":
        operation = state.renew_owner(owner)
    elif case == "bind_owner":
        operation = state.bind_owner(owner, "new", purpose="commands")
    elif case == "unbind_owner":
        await state.bind_owner(owner, "existing", purpose="commands")
        operation = state.unbind_owner(owner)
    elif case in {"renew_warm", "publish_warm", "discard_warm"}:
        table = "tinkerfin_opensandbox_warm_slots"
        warm = await state.claim_warm_slot()
        assert warm is not None
        if case == "renew_warm":
            operation = state.renew_warm(warm)
        elif case == "publish_warm":
            operation = state.publish_warm(warm, "new")
        else:
            await state.publish_warm(warm, "ready")
            ready = await state.claim_ready_warm_slot(exclude_slots=())
            assert ready is not None
            operation = state.discard_ready_warm_slot(ready)
    elif case == "renew_cleanup":
        table = "tinkerfin_opensandbox_cleanup"
        await state.enqueue_cleanup("orphan")
        cleanup = await state.claim_cleanup()
        assert cleanup is not None
        operation = state.renew_cleanup(cleanup)
    elif case == "consume_warm":
        warm = await state.claim_warm_slot()
        assert warm is not None
        await state.publish_warm(warm, "ready")
        operation = state.consume_warm(owner)
    else:
        await state.bind_owner(owner, "existing", purpose="commands")
        running = await state.register_holder(owner, "holder")
        operation = (
            state.change_availability(owner, running, phase="draining")
            if case == "availability"
            else state.register_holder(owner, "second-holder")
        )
    blocker = await engine.connect()
    await blocker.begin()
    await blocker.execute(text(f"SELECT * FROM {table} FOR UPDATE"))
    entered = asyncio.Event()
    identities: list[int] = []
    execute = AsyncConnection.execute

    async def observe(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if (
            connection.engine is engine
            and not entered.is_set()
            and str(statement).startswith(
                (f"SELECT {table}.", f"UPDATE {table}", f"DELETE FROM {table}")
            )
        ):
            command = (
                "SELECT pg_backend_pid()"
                if engine.dialect.name == "postgresql"
                else "SELECT CONNECTION_ID()"
            )
            value = (await execute(connection, text(command))).scalar_one()
            assert isinstance(value, int)
            identities.append(value)
            entered.set()
        return await execute(connection, statement, *args, **kwargs)

    async def invoke() -> object:
        return await operation

    pending: asyncio.Task[object] | None = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "execute", observe)
            pending = asyncio.create_task(invoke())
            async with asyncio.timeout(5):
                await entered.wait()
            await _wait_for_lock(engine, identities[0], pending)
            now[0] += timedelta(seconds=61)
            await blocker.rollback()
            if case == "acquire_owner":
                current = await pending
                assert isinstance(current, OpenSandboxOwnerClaim)
                await state.bind_owner(current, "resumed", purpose="commands")
                async with engine.connect() as connection:
                    expiry = await connection.scalar(
                        text(
                            "SELECT MIN(lease_expires_at) FROM tinkerfin_opensandbox_owners WHERE namespace = 'clock'"
                        )
                    )
                assert isinstance(expiry, datetime) and expiry > now[0]
            elif case.startswith("renew_"):
                assert await pending is False
            else:
                with pytest.raises(OpenSandboxStateOwnershipError):
                    await pending
    finally:
        await blocker.rollback()
        await blocker.close()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await state.aclose()


@pytest.mark.parametrize("case", _CASES)
async def test_server_lease_fencing_after_row_lock_wait(
    sandbox_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    if sandbox_sql_engine.dialect.name == "sqlite":
        pytest.skip(
            "SQLite acquires the database writer lock before the claim operation"
        )
    await _check_clock(sandbox_sql_engine, monkeypatch, case)


@pytest.mark.docker_integration
@pytest.mark.parametrize("case", _CASES)
async def test_mysql57_lease_fencing_after_row_lock_wait(
    mysql57_sandbox_url: str, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    engine = create_async_engine(mysql57_sandbox_url, pool_size=4, max_overflow=0)
    try:
        await _check_clock(engine, monkeypatch, case)
    finally:
        await engine.dispose()


async def _check_subsecond_leases(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2030, 1, 1, 0, 0, 0, 100000)
    monkeypatch.setattr(SQLAlchemyOpenSandboxState, "_now", staticmethod(lambda: now))
    state = SQLAlchemyOpenSandboxState(engine=engine, lease_ttl=0.1)
    try:
        await state.start(warm_pool_size=1)
        owner = await state.acquire_owner("owner")
        warm = await state.claim_warm_slot()
        assert warm is not None
        await state.enqueue_cleanup("orphan")
        cleanup = await state.claim_cleanup()
        assert cleanup is not None
        async with engine.connect() as connection:
            for table in (
                "tinkerfin_opensandbox_owners",
                "tinkerfin_opensandbox_warm_slots",
                "tinkerfin_opensandbox_cleanup",
                "tinkerfin_opensandbox_workers",
            ):
                expiry = await connection.scalar(
                    text(f"SELECT lease_expires_at FROM {table}").columns(
                        lease_expires_at=DateTime(timezone=False)
                    )
                )
                assert expiry == now + timedelta(seconds=0.1)
        assert await state.renew_cleanup(cleanup)
        assert await state.renew_owner(owner)
        await state.bind_owner(owner, "bound", purpose="commands")
        await state.publish_warm(warm, "ready")
    finally:
        await state.aclose()


async def test_sql_state_preserves_subsecond_leases(
    sandbox_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _check_subsecond_leases(sandbox_sql_engine, monkeypatch)


@pytest.mark.docker_integration
async def test_mysql57_state_preserves_subsecond_leases(
    mysql57_sandbox_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(mysql57_sandbox_url)
    try:
        await _check_subsecond_leases(engine, monkeypatch)
    finally:
        await engine.dispose()
