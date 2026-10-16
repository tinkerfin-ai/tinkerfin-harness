"""Deterministic faults at acknowledged SQL commands on a selected test Engine."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@contextmanager
def after_sql_command(
    engine: AsyncEngine,
    statement: str,
    observe_result: Callable[[], Awaitable[None]],
) -> Iterator[None]:
    """Hold or fail one command after the driver completed it but before return.

    Other Engines and statements remain usable, so a peer can advance the same
    database while a caller is still missing its original commit acknowledgement.
    SQLAlchemy's execution options and result are passed through unchanged.
    """

    execute = AsyncConnection.exec_driver_sql

    async def execute_observed(
        connection: AsyncConnection,
        command: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = await execute(connection, command, *args, **kwargs)
        if connection.engine is engine and command == statement:
            await observe_result()
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AsyncConnection, "exec_driver_sql", execute_observed)
        yield


@contextmanager
def after_sql_commit(
    engine: AsyncEngine, observe_result: Callable[[], Awaitable[None]]
) -> Iterator[None]:
    """Inject an acknowledgement outcome without replacing transaction ownership."""

    if engine.dialect.name == "sqlite":
        with after_sql_command(engine, "COMMIT", observe_result):
            yield
        return
    commit = AsyncConnection.commit

    async def commit_observed(connection: AsyncConnection) -> None:
        await commit(connection)
        if connection.engine is engine:
            await observe_result()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AsyncConnection, "commit", commit_observed)
        yield
