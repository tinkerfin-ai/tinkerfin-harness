"""Async ORM sessions whose savepoints belong to a real root transaction."""

from __future__ import annotations

from types import TracebackType
from typing import Self, TypeVar

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.ext.asyncio.session import _AsyncSessionContextManager
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.pool import StaticPool

from ._settlement import select_failure, settle
from .capabilities import engine_dialect
from .errors import SqlTransactionError

_SessionT = TypeVar("_SessionT", bound="SqlSession")


class _SessionContext(_AsyncSessionContextManager[_SessionT]):
    """Join factory transaction settlement and session release as one operation."""

    async def __aenter__(self) -> _SessionT:
        # SQLAlchemy 2.0.52 allows after_transaction_create to perform I/O.
        # Factory ownership starts before that hook, even when no body is entered.
        try:
            return await super().__aenter__()
        except BaseException as error:
            try:
                await settle(
                    self.async_session.__aexit__(
                        type(error), error, error.__traceback__
                    )
                )
            except BaseException as cleanup:  # noqa: BLE001 - preserve cancellation and both failures
                raise select_failure(error, cleanup)
            raise

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # SQLAlchemy 2.0.52 async_sessionmaker.begin selects the session's
        # _maker_context_manager. Its default exit shields a detached task and
        # skips session closure if transaction exit fails. Keep that native entry
        # and transaction behavior, but retain ownership through every exit.
        async def finish() -> None:
            failure = error
            try:
                await self.trans.__aexit__(kind, error, traceback)
            except BaseException as transaction_error:  # noqa: BLE001 - retain failure through session cleanup
                failure = (
                    transaction_error
                    if error is None
                    else select_failure(error, transaction_error)
                )
                raise failure
            finally:
                await self.async_session.__aexit__(
                    None if failure is None else type(failure),
                    failure,
                    None if failure is None else failure.__traceback__,
                )

        await settle(finish())


class _TransactionalSession(Session):
    """Scope transaction initialization to sessions that selected this capability."""


def _begin_before_savepoint(connection: Connection, _name: str | None) -> None:
    # SQLAlchemy 2.0.52 sqlite.base documents that a SAVEPOINT created before
    # SQLite BEGIN commits independently of a later root rollback. A Session's
    # logical transaction alone does not establish that database boundary.
    # The aiosqlite driver's public property is a local state read, not I/O.
    driver = connection.connection.driver_connection
    active: object = getattr(driver, "in_transaction", None)
    if not isinstance(active, bool):
        raise SqlTransactionError("SQLite driver does not expose transaction state")
    if not active:
        connection.exec_driver_sql("BEGIN")


def _bind_savepoints(
    _session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    if (
        not transaction.nested
        and connection.dialect.name == "sqlite"
        and not event.contains(connection, "savepoint", _begin_before_savepoint)
    ):
        # Connection-level listeners expire with this checkout. Plain reads keep
        # the Engine's isolation policy; they do not acquire an extra SQLite read
        # lock that could block another service's projection or admission.
        event.listen(connection, "savepoint", _begin_before_savepoint)


event.listen(_TransactionalSession, "after_begin", _bind_savepoints)


class SqlSession(AsyncSession):
    """Keep ORM savepoint writes within the enclosing Session transaction.

    Args:
        bind: Borrowed asynchronous Engine with exclusive connection checkouts.
        autoflush: Whether queries flush pending ORM changes, as in AsyncSession.
        expire_on_commit: Whether committed ORM values expire, as in AsyncSession.

    SQLite starts a database transaction before its first savepoint when needed.
    Ordinary reads retain the Engine's isolation policy. MySQL and PostgreSQL
    retain their native Session transaction behavior. The native
    execute, flush and savepoint interfaces and SQLAlchemy exceptions remain
    available. Explicit commit, rollback and close settle accepted I/O before
    propagating caller cancellation. Context exit closes uncommitted work; it
    does not implicitly commit. A sessionmaker.begin context commits on normal
    exit and joins transaction settlement and session close before cancellation
    propagates. Failed or cancelled entry also closes the session. The Engine
    remains caller-owned.

    Raises:
        TypeError: bind is not an async Engine or uses shared StaticPool checkouts.
        ValueError: The Engine's SQL dialect is unsupported.
        SqlTransactionError: A SQLite driver cannot establish transaction guarantees.
    """

    def __init__(
        self,
        bind: AsyncEngine,
        *,
        autoflush: bool = True,
        expire_on_commit: bool = True,
    ) -> None:
        """Validate borrowed storage without opening a connection."""
        engine_dialect(bind)
        if isinstance(bind.sync_engine.pool, StaticPool):
            raise TypeError("SQL sessions require exclusive connection checkouts")
        super().__init__(
            bind,
            autoflush=autoflush,
            expire_on_commit=expire_on_commit,
            sync_session_class=_TransactionalSession,
        )

    def _maker_context_manager(self) -> _SessionContext[Self]:
        return _SessionContext(self)

    async def commit(self) -> None:
        """Settle the accepted commit before propagating cancellation or failure."""
        await settle(super().commit())

    async def rollback(self) -> None:
        """Roll back the complete root transaction, including released savepoints."""
        await settle(super().rollback())

    async def close(self) -> None:
        """Return owned checkouts after all rollback and cleanup work has settled."""
        await settle(super().close())

    async def __aexit__(
        self,
        _kind: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close uncommitted work while retaining body and cleanup failures."""
        try:
            await self.close()
        except BaseException as cleanup:
            if error is not None:
                raise select_failure(error, cleanup)
            raise
