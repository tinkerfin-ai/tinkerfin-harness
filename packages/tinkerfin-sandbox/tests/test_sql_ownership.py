"""Preserve public State ownership and failures when callers cancel SQL work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Literal, TypeGuard

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from tests.support.sql_faults import after_sql_commit

from tinkerfin_sandbox import SQLAlchemyOpenSandboxState
from tinkerfin_sqlalchemy import SqlTransaction


class _Control(BaseException):
    """Represent process control raised inside an owned SQL operation."""


def _is_group(error: BaseException) -> TypeGuard[BaseExceptionGroup[BaseException]]:
    return isinstance(error, BaseExceptionGroup)


def _contains(error: BaseException, target: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is target:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend(
            item
            for item in (current.__cause__, current.__context__)
            if item is not None
        )
        if _is_group(current):
            pending.extend(current.exceptions)
        cause = vars(current).get("cause")
        if isinstance(cause, BaseException):
            pending.append(cause)
    return False


async def _outcome(operation: Awaitable[None]) -> BaseException | None:
    try:
        await operation
    except BaseException as error:  # noqa: BLE001 - tests keep control errors inside their calling task
        return error
    return None


@pytest.mark.parametrize("operation", ["start", "close"])
@pytest.mark.parametrize("control", [False, True])
async def test_sql_shared_lifecycle_retains_cancelled_waiter_and_original_failure(
    sandbox_sql_engine: AsyncEngine,
    operation: Literal["start", "close"],
    control: bool,
) -> None:
    state = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine)
    if operation == "close":
        await state.start(warm_pool_size=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()
    failure = (
        _Control("database stopped") if control else RuntimeError("database failed")
    )
    original_cause = OSError("original connection failure")
    failure.__cause__ = original_cause
    commits = 0

    async def gate() -> None:
        nonlocal commits
        commits += 1
        entered.set()
        await release.wait()
        raise failure

    async def invoke(*, second: bool = False) -> BaseException | None:
        if second:
            second_entered.set()
        return await _outcome(
            state.start(warm_pool_size=1) if operation == "start" else state.aclose()
        )

    with after_sql_commit(sandbox_sql_engine, gate):
        first = asyncio.create_task(invoke())
        second: asyncio.Task[BaseException | None] | None = None
        try:
            await entered.wait()
            second = asyncio.create_task(invoke(second=True))
            await second_entered.wait()
            first.cancel("cancelled waiter")
            first.cancel("repeat cancellation")
            release.set()
            first_error, second_error = await asyncio.gather(first, second)
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            if second is not None:
                await asyncio.gather(second, return_exceptions=True)
    assert commits == 1
    assert first_error is not None and second_error is not None
    if control:
        assert first_error is failure and second_error is failure
    else:
        assert isinstance(first_error, asyncio.CancelledError)
    for error in (first_error, second_error):
        assert _contains(error, failure) and _contains(error, original_cause)
    if operation == "start":
        close_error = await _outcome(state.aclose())
        assert close_error is failure if control else close_error is None
    async with sandbox_sql_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_opensandbox_workers")
            )
            == 0
        )


async def test_shared_sql_cleanup_failure_is_visible_to_the_domain_retry_decision(
    sandbox_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction = SqlTransaction(sandbox_sql_engine, read_only=True)
    close = AsyncConnection.close
    original = RuntimeError("connection close failed")

    async def fail_close(connection: AsyncConnection) -> None:
        await close(connection)
        if connection.engine is sandbox_sql_engine:
            raise original

    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "close", fail_close)
        with pytest.raises(RuntimeError) as captured:
            async with transaction as connection:
                assert await connection.scalar(text("SELECT 1")) == 1
    assert captured.value is original
    assert transaction.rolled_back and transaction.cleanup_failed
    assert not transaction.committed and not transaction.commit_uncertain


async def test_shared_sql_explicit_rollback_failure_prevents_domain_retry(
    sandbox_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction = SqlTransaction(sandbox_sql_engine, read_only=True)
    original = RuntimeError("explicit rollback failed")
    rollback = AsyncConnection.rollback
    execute = AsyncConnection.exec_driver_sql
    armed = False
    failed = False

    def fail_once(connection: AsyncConnection) -> None:
        nonlocal failed
        if connection.engine is sandbox_sql_engine and armed and not failed:
            failed = True
            raise original

    async def rollback_observed(connection: AsyncConnection) -> None:
        fail_once(connection)
        await rollback(connection)

    async def execute_observed(connection: AsyncConnection, statement: str):
        if statement == "ROLLBACK":
            fail_once(connection)
        return await execute(connection, statement)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "rollback", rollback_observed)
        patch.setattr(AsyncConnection, "exec_driver_sql", execute_observed)
        with pytest.raises(RuntimeError) as captured:
            async with transaction as connection:
                assert await connection.scalar(text("SELECT 1")) == 1
                armed = True
                await transaction.rollback()
    assert captured.value is original
    assert failed and transaction.cleanup_failed
    assert not transaction.committed and not transaction.rolled_back
