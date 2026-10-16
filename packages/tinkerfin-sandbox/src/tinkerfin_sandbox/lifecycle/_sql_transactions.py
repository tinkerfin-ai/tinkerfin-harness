"""Sandbox State transaction decisions over shared SQL connection guarantees."""

from __future__ import annotations

__all__ = [
    "_apply_claim_lock",
    "_is_retryable_claim_conflict",
    "_read_rows",
    "_run_claim_transaction",
]

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, TypeVar

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import Select

from tinkerfin_sqlalchemy import (
    DatabaseCapabilities,
    SqlTransaction,
    database_capabilities,
    mysql_error_code,
    postgresql_error_code,
    sqlite_lock_error,
)

from ..errors import (
    OpenSandboxStateCommitUncertainError,
    OpenSandboxStateConfigurationError,
    OpenSandboxStateError,
    OpenSandboxStateTimeoutError,
    UnexpectedOpenSandboxStateError,
)
from ._sql_schema import _initialize_schema, _warm_slots, _workers
from ._sql_tasks import (
    Cancellation,
    TaskOutcome,
    capture,
    join_owned_task,
    run_owned_operation,
)

if TYPE_CHECKING:
    from .sqlalchemy import SQLAlchemyOpenSandboxState

_ResultT = TypeVar("_ResultT")
_SelectRowT = TypeVar("_SelectRowT", bound=tuple[object, ...])
_RETRY_MAX_DELAY_SECONDS = 0.5


class _RetryableSQLiteWriteError(Exception):
    """Carry a proven uncommitted lock conflict after successful connection cleanup."""

    def __init__(self, error: DBAPIError) -> None:
        super().__init__("SQLite write lock conflict")
        self.error = error


def _apply_claim_lock(
    statement: Select[_SelectRowT], *, capabilities: DatabaseCapabilities
) -> Select[_SelectRowT]:
    if not capabilities.row_locks:
        return statement
    return statement.with_for_update(skip_locked=capabilities.skip_locked)


def _is_retryable_claim_conflict(
    self: SQLAlchemyOpenSandboxState, error: DBAPIError
) -> bool:
    # Only acquisition and idempotent cleanup insertion retry server conflicts.
    # Ordinary mutations and uncertain COMMIT never enter these domain loops.
    if self._dialect == "mysql":
        return mysql_error_code(error) in {1062, 1205, 1213}
    if self._dialect == "postgresql":
        return postgresql_error_code(error) in {"23505", "40001", "40P01"}
    return False


def _transaction(
    self: SQLAlchemyOpenSandboxState, *, initialize: bool
) -> SqlTransaction:
    capabilities = self._capabilities
    busy_seconds = min(self._poll_interval, self._sqlite_retry_timeout, 0.01)
    return SqlTransaction(
        self._engine,
        sqlite_busy_timeout_ms=math.ceil(busy_seconds * 1000),
        mysql_lock_wait_timeout_seconds=(
            1
            if self._dialect == "mysql"
            and (capabilities is None or not capabilities.skip_locked)
            else None
        ),
        schema_lock="opensandbox-state" if initialize else None,
    )


async def _commit(
    self: SQLAlchemyOpenSandboxState,
    transaction: SqlTransaction,
    cancellation: Cancellation,
) -> None:
    """Retry SQLite COMMIT BUSY in place; an unknown acknowledgement is terminal."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + self._sqlite_retry_timeout
    delay = min(self._poll_interval, _RETRY_MAX_DELAY_SECONDS)
    while True:
        if cancellation.error is not None:
            await transaction.rollback()
            return
        try:
            await transaction.commit()
            return
        except DBAPIError as error:
            if transaction.committed or transaction.commit_uncertain:
                raise
            if self._dialect != "sqlite" or not sqlite_lock_error(error):
                raise
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise OpenSandboxStateTimeoutError(
                    "SQLite COMMIT lock retry budget exhausted", cause=error
                ) from error
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, _RETRY_MAX_DELAY_SECONDS)


async def _write_transaction_once(
    self: SQLAlchemyOpenSandboxState,
    operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
    *,
    initialize: bool = False,
) -> _ResultT:
    cancellation = Cancellation()

    async def execute() -> _ResultT:
        transaction = _transaction(self, initialize=initialize)
        try:
            async with transaction as connection:
                if cancellation.error is not None:
                    await transaction.rollback()
                    raise cancellation.error
                result = await operation(connection)
                await _commit(self, transaction, cancellation)
            if cancellation.error is not None:
                raise cancellation.error
            return result
        except Exception as error:
            if transaction.commit_uncertain:
                raise OpenSandboxStateCommitUncertainError(
                    "OpenSandbox write COMMIT outcome is uncertain; the transaction was not retried",
                    cause=error,
                ) from error
            if transaction.committed or transaction.cleanup_failed:
                # A subsequent domain conflict loop must not hide failed cleanup.
                if isinstance(error, DBAPIError):
                    raise UnexpectedOpenSandboxStateError(
                        "OpenSandbox State transaction settlement failed", cause=error
                    ) from error
                raise
            if (
                isinstance(error, DBAPIError)
                and self._dialect == "sqlite"
                and sqlite_lock_error(error)
            ):
                raise _RetryableSQLiteWriteError(error) from error
            raise
        finally:
            if (
                initialize
                and transaction.rolled_back
                and not transaction.commit_uncertain
            ):
                self._worker_may_exist = False

    return await run_owned_operation(
        execute(), task_name="tinkerfin-sandbox-state-write", cancellation=cancellation
    )


async def _read_rows(
    self: SQLAlchemyOpenSandboxState, statement: Select[_SelectRowT]
) -> Sequence[RowMapping]:
    """Read one consistent snapshot and return its connection before delivering cancellation."""

    async def read() -> Sequence[RowMapping]:
        async with SqlTransaction(self._engine, read_only=True) as connection:
            return (await connection.execute(statement)).mappings().all()

    return await run_owned_operation(read(), task_name="tinkerfin-sandbox-state-read")


async def _run_write_transaction(
    self: SQLAlchemyOpenSandboxState,
    operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
    *,
    initialize: bool = False,
) -> _ResultT:
    """Retry proven SQLite write conflicts within one bounded domain budget."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + self._sqlite_retry_timeout
    delay = min(self._poll_interval, _RETRY_MAX_DELAY_SECONDS)
    while True:
        try:
            return await _write_transaction_once(self, operation, initialize=initialize)
        except _RetryableSQLiteWriteError as retry:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise OpenSandboxStateTimeoutError(
                    "SQLite write lock retry budget exhausted", cause=retry.error
                ) from retry.error
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, _RETRY_MAX_DELAY_SECONDS)


async def _run_claim_transaction(
    self: SQLAlchemyOpenSandboxState,
    operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
) -> _ResultT | None:
    try:
        return await _run_write_transaction(self, operation)
    except DBAPIError as error:
        capabilities = self._require_capabilities()
        if (
            capabilities.dialect == "mysql"
            and not capabilities.skip_locked
            and mysql_error_code(error) == 1205
        ):
            return None
        raise


async def start(self: SQLAlchemyOpenSandboxState, *, warm_pool_size: int) -> None:
    """Share one accepted startup; only a later call can retry a configuration conflict."""

    if isinstance(warm_pool_size, bool) or not isinstance(warm_pool_size, int):
        raise TypeError("warm_pool_size must be an integer")
    if warm_pool_size < 0:
        raise ValueError("warm_pool_size must not be negative")
    if self._closed:
        raise OpenSandboxStateError("OpenSandbox state is closed")
    start_task = self._start_task
    if start_task is not None and _is_retryable_start_failure(start_task):
        self._start_task = None
        self._warm_pool_size = None
        start_task = None
    if start_task is not None:
        if self._warm_pool_size != warm_pool_size:
            raise OpenSandboxStateConfigurationError(
                "OpenSandbox State is already started with a different warm_pool_size"
            )
    else:
        self._warm_pool_size = warm_pool_size
        start_task = asyncio.create_task(
            capture(_start_once(self, warm_pool_size=warm_pool_size)),
            name=f"tinkerfin-opensandbox-start:{self._worker_id}",
        )
        self._start_task = start_task
    await join_owned_task(start_task)


def _is_retryable_start_failure(start_task: asyncio.Task[TaskOutcome[None]]) -> bool:
    return start_task.done() and isinstance(
        start_task.result(), OpenSandboxStateConfigurationError
    )


async def _start_once(self: SQLAlchemyOpenSandboxState, *, warm_pool_size: int) -> None:
    async def initialize(connection: AsyncConnection) -> None:
        self._capabilities = database_capabilities(connection)
        await connection.run_sync(_initialize_schema)
        now = self._now()

        await connection.execute(
            delete(_workers).where(
                _workers.c.namespace == self._namespace,
                _workers.c.lease_expires_at <= now,
            )
        )
        active_capacities = set(
            (
                await connection.execute(
                    select(_workers.c.warm_pool_size).where(
                        _workers.c.namespace == self._namespace,
                        _workers.c.lease_expires_at > now,
                    )
                )
            ).scalars()
        )
        if active_capacities and active_capacities != {warm_pool_size}:
            raise OpenSandboxStateConfigurationError(
                "Active OpenSandbox workers use a different warm_pool_size"
            )

        existing_slots = {
            int(row.slot): row.sandbox_id
            for row in (
                await connection.execute(
                    select(
                        _warm_slots.c.slot,
                        _warm_slots.c.sandbox_id,
                    ).where(_warm_slots.c.namespace == self._namespace)
                )
            )
        }
        if not active_capacities:
            # No live worker can still own a warm transition. Releasing every stale
            # claim lets the next manager validate published IDs or refill empty slots
            # before it reports readiness.
            await connection.execute(
                update(_warm_slots)
                .where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.slot < warm_pool_size,
                )
                .values(
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            for slot, sandbox_id in existing_slots.items():
                if slot < warm_pool_size:
                    continue
                if sandbox_id is not None:
                    await self._enqueue_cleanup_in_transaction(
                        connection,
                        str(sandbox_id),
                        now=now,
                    )
            await connection.execute(
                delete(_warm_slots).where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.slot >= warm_pool_size,
                )
            )
        for slot in range(warm_pool_size):
            if slot in existing_slots:
                continue
            await connection.execute(
                insert(_warm_slots).values(
                    namespace=self._namespace,
                    slot=slot,
                    sandbox_id=None,
                    generation=0,
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
        now = self._now()
        self._worker_may_exist = True
        await connection.execute(
            insert(_workers).values(
                namespace=self._namespace,
                worker_id=self._worker_id,
                warm_pool_size=warm_pool_size,
                lease_expires_at=now + timedelta(seconds=self._worker_lease_ttl),
                updated_at=now,
            )
        )

    await _run_write_transaction(self, initialize, initialize=True)
    self._started = True
    self._worker_renew_task = asyncio.create_task(
        capture(_renew_worker_loop(self)),
        name=f"tinkerfin-opensandbox-worker:{self._worker_id}",
    )


async def _renew_worker_loop(self: SQLAlchemyOpenSandboxState) -> None:
    """Keep registration live; retain any unexpected stop for the State's caller."""

    try:
        while True:
            await asyncio.sleep(self._worker_lease_ttl / 3)

            async def renew(connection: AsyncConnection) -> None:
                statement = select(_workers.c.worker_id).where(
                    _workers.c.namespace == self._namespace,
                    _workers.c.worker_id == self._worker_id,
                )
                if self._require_capabilities().row_locks:
                    statement = statement.with_for_update()
                if await connection.scalar(statement) is None:
                    raise OpenSandboxStateError(
                        "OpenSandbox worker registration was lost"
                    )
                now = self._now()
                result = await connection.execute(
                    update(_workers)
                    .where(
                        _workers.c.namespace == self._namespace,
                        _workers.c.worker_id == self._worker_id,
                    )
                    .values(
                        lease_expires_at=now
                        + timedelta(seconds=self._worker_lease_ttl),
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise OpenSandboxStateError(
                        "OpenSandbox worker registration was lost"
                    )

            await _run_write_transaction(self, renew)
    except asyncio.CancelledError as error:
        if not self._closed:
            self._worker_failure = error
    except BaseException as error:  # noqa: BLE001 - deliver process control through a State call
        self._worker_failure = error
