"""Classify accepted SQL messages before releasing their caller-owned resources."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import replace
from typing import Any, TypeVar

import pytest
from backend_harness import MessagingBackendHarness
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from tests.support.sql_faults import after_sql_commit

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    MessagingBackendUnavailable,
    SqlAlchemyBackend,
)
from tinkerfin_messaging.backend_contract import MessagingTransition

_T = TypeVar("_T")


class ProcessControl(BaseException):
    """A caller-visible control outcome from accepted database work."""


async def capture(operation: Awaitable[_T]) -> _T | BaseException:
    try:
        return await operation
    except BaseException as error:  # noqa: BLE001 - inspect the exact owning-call outcome
        return error


def contains(root: BaseException, target: BaseException) -> bool:
    pending, seen = [root], set()
    while pending:
        error = pending.pop()
        if error is target:
            return True
        if id(error) in seen:
            continue
        seen.add(id(error))
        pending.extend(
            item for item in (error.__cause__, error.__context__) if item is not None
        )
        cause = vars(error).get("cause")
        if isinstance(cause, BaseException):
            pending.append(cause)
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
    return False


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize(
    "failure_type", [None, OSError, TypeError, ValueError, ProcessControl]
)
async def test_sql_message_write_settles_before_cancel_or_failure(
    messaging_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
    failure_type: type[BaseException] | None,
) -> None:
    engine = messaging_sql_engine
    backend = MessagingBackendHarness(SqlAlchemyBackend(engine))
    identity = RunIdentity(namespace="customer", thread_id="thread", run_id="run")
    owner = await backend.prepare(
        channel="events",
        identity=identity,
        codec="bytes",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    cause = RuntimeError("original database failure")
    failure = failure_type("database statement outcome") if failure_type else None
    if failure is not None:
        failure.__cause__ = cause
    execute = AsyncConnection.execute

    async def observed(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        result = await execute(connection, statement, *args, **kwargs)
        if connection.engine is engine and str(statement).startswith(
            "INSERT INTO tinkerfin_messaging_messages"
        ):
            entered.set()
            await release.wait()
            if failure is not None:
                raise failure
        return result

    monkeypatch.setattr(AsyncConnection, "execute", observed)
    task = asyncio.create_task(
        capture(
            backend.append(
                owner.handle, message_id="m", codec="bytes", payload=b"payload"
            )
        )
    )
    try:
        await entered.wait()
        if cancel:
            task.cancel("caller stopped")
            queued = asyncio.Event()
            asyncio.get_running_loop().call_soon(queued.set)
            await queued.wait()
            task.cancel("caller stopped again")
        assert not task.done()
        release.set()
        outcome = await task
        if isinstance(failure, ProcessControl):
            assert outcome is failure
        elif cancel:
            assert isinstance(outcome, asyncio.CancelledError)
        elif failure is not None:
            assert isinstance(outcome, MessagingBackendUnavailable)
        else:
            assert not isinstance(outcome, BaseException)
        if failure is not None:
            assert isinstance(outcome, BaseException)
            assert contains(outcome, failure) and contains(outcome, cause)
        page = await backend.read(channel="events", identity=identity)
        assert len(page) == (0 if cancel or failure is not None else 1)
        async with engine.connect() as connection:
            assert await connection.exec_driver_sql("SELECT 1") is not None
    finally:
        release.set()
        await task


async def test_sql_message_unknown_commit_is_not_replayed(
    messaging_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = messaging_sql_engine
    backend = MessagingBackendHarness(SqlAlchemyBackend(engine))
    identity = RunIdentity(namespace="customer", thread_id="thread", run_id="run")
    owner = await backend.prepare(
        channel="events",
        identity=identity,
        codec="bytes",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    execute = AsyncConnection.execute
    inserts = acknowledgements = 0

    async def observed(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal inserts
        result = await execute(connection, statement, *args, **kwargs)
        if connection.engine is engine and str(statement).startswith(
            "INSERT INTO tinkerfin_messaging_messages"
        ):
            inserts += 1
        return result

    failure = OSError("lost commit acknowledgement")

    async def lost_acknowledgement() -> None:
        nonlocal acknowledgements
        if inserts:
            acknowledgements += 1
            raise failure

    monkeypatch.setattr(AsyncConnection, "execute", observed)
    with after_sql_commit(engine, lost_acknowledgement):
        with pytest.raises(MessagingBackendUnavailable) as captured:
            await backend.append(
                owner.handle, message_id="m", codec="bytes", payload=b"payload"
            )
    assert contains(captured.value, failure)
    assert inserts == acknowledgements == 1
    page = await backend.read(channel="events", identity=identity)
    assert len(page) == 1
    repeated = await backend.append(
        owner.handle, message_id="m", codec="bytes", payload=b"payload"
    )
    assert repeated == page[0]
    assert inserts == 1


async def test_sql_parameter_failure_retains_independent_rollback_failure(
    messaging_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = messaging_sql_engine
    backend = SqlAlchemyBackend(engine)
    identity = RunIdentity(namespace="customer", thread_id="thread", run_id="run")
    prepared = await backend.commit_messaging_transition(
        MessagingTransition(
            kind="prepare_run",
            transition_id="prepare",
            channel="events",
            identity=identity,
            settings=backend.messaging_settings,
            codec_id="bytes",
            after_sequence=0,
        )
    )
    assert prepared.run_reference is not None
    failure = OSError("independent rollback acknowledgement failure")
    cause = RuntimeError("original rollback cause")
    failure.__cause__ = cause
    injected = False

    async def fail_once() -> None:
        nonlocal injected
        if not injected:
            injected = True
            raise failure

    if engine.dialect.name == "sqlite":
        execute = AsyncConnection.exec_driver_sql

        async def rollback_sql(
            connection: AsyncConnection, statement: str, *args: Any, **kwargs: Any
        ) -> Any:
            result = await execute(connection, statement, *args, **kwargs)
            if connection.engine is engine and statement == "ROLLBACK":
                await fail_once()
            return result

        monkeypatch.setattr(AsyncConnection, "exec_driver_sql", rollback_sql)
    else:
        rollback = AsyncConnection.rollback

        async def rollback_method(connection: AsyncConnection) -> None:
            await rollback(connection)
            if connection.engine is engine:
                await fail_once()

        monkeypatch.setattr(AsyncConnection, "rollback", rollback_method)

    with pytest.raises(ValueError) as captured:
        await backend.commit_messaging_transition(
            MessagingTransition(
                kind="renew_producer_ownership",
                transition_id="renew",
                channel="events",
                identity=identity,
                settings=backend.messaging_settings,
                run_reference=replace(prepared.run_reference, channel="different"),
            )
        )
    assert injected
    assert contains(captured.value, failure)
    assert contains(captured.value, cause)
