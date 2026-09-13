"""Async SQLAlchemy implementation of the Automation Store contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy import (
    ColumnElement,
    DateTime,
    and_,
    case,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SqlTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tinkerfin_sqlalchemy import (
    SqlTransaction,
    database_capabilities,
    engine_dialect,
    mysql_error_code,
    postgresql_error_code,
    sqlite_lock_error,
)

from . import _rules
from ._codec import (
    decode_execution,
    decode_operation_result,
    decode_task,
    encode_execution,
    encode_operation_result,
    encode_task,
)
from ._query_cursor import decode_cursor, encode_cursor, query_scope
from ._tasks import Cancellation, TaskOutcome, capture, join_owned_task
from .errors import (
    AutomationError,
    AutomationStoreError,
    AutomationStoreProtocolError,
    AutomationStoreTimeout,
    ClaimLostError,
    ExecutionNotFoundError,
    RequestConflictError,
    TaskConflictError,
    TaskNotFoundError,
)
from .models import (
    AttentionResolution,
    AutomationExecution,
    AutomationTask,
    ExecutionPage,
    ExecutionStatus,
    TaskPage,
    TaskStatus,
    _validate_persisted_text,
)
from .queries import ExecutionFilter, TaskFilter
from .sql_schema import (
    operations,
    prepare_schema,
    runs,
    scopes,
    tasks,
    work_items,
)
from .store import (
    MaterializationResult,
    ScheduledExecution,
    StartAuthorization,
    WorkItemClaim,
    WorkKind,
)

_ResultT = TypeVar("_ResultT")
_PENDING = "pending"
_CLAIMED = "claimed"
_COMPLETED = "completed"
_SCHEMA_LOCK_TIMEOUT_SECONDS = 30


def _validate_scope(namespace: object, owner_id: object) -> None:
    """Validate direct Store ownership inputs before database access."""

    _validate_persisted_text(namespace, name="namespace", maximum=128)
    _validate_persisted_text(owner_id, name="owner_id", maximum=191)


def _database_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database datetime values must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _identity_digest(execution: AutomationExecution) -> bytes:
    encoded = json.dumps(
        [execution.identity.thread_id, execution.identity.run_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def _task_values(task: AutomationTask) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "namespace": task.namespace,
        "owner_id": task.owner_id,
        "name": task.name,
        "search_name": task.name.casefold(),
        "status": task.status.value,
        "revision": task.revision,
        "next_run_at": _database_datetime(task.next_run_at),
        "payload": encode_task(task),
        "created_at": _database_datetime(task.created_at),
        "updated_at": _database_datetime(task.updated_at),
    }


def _run_values(
    execution: AutomationExecution,
    *,
    occurrence_key: str | None = None,
    admitted: bool | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "execution_id": execution.execution_id,
        "namespace": execution.namespace,
        "owner_id": execution.owner_id,
        "task_id": execution.task_id,
        "task_name": execution.task_name,
        "search_name": execution.task_name.casefold() if execution.task_name else None,
        "identity_digest": _identity_digest(execution),
        "thread_id": execution.identity.thread_id,
        "run_id": execution.identity.run_id,
        "status": execution.status.value,
        "attempt": execution.attempt,
        "retry_of": execution.retry_of,
        "scheduled_for": _database_datetime(execution.scheduled_for),
        "queued_at": _database_datetime(execution.queued_at),
        "queue_deadline": _database_datetime(execution.queue_deadline),
        "execution_started_at": _database_datetime(execution.execution_started_at),
        "execution_deadline": _database_datetime(execution.execution_deadline),
        "finished_at": _database_datetime(execution.finished_at),
        "start_authorized_at": _database_datetime(execution.start_authorized_at),
        "start_token": execution.start_token,
        "payload": encode_execution(execution),
        "created_at": _database_datetime(execution.created_at),
        "updated_at": _database_datetime(execution.updated_at),
    }
    if occurrence_key is not None:
        values["occurrence_key"] = occurrence_key
    if admitted is not None:
        values["admitted"] = admitted
    return values


class SqlAlchemyAutomationStore:
    """Persist tasks and execution ownership on SQLite, MySQL, or PostgreSQL.

    The Engine belongs to the caller. Each operation borrows a connection for one
    atomic write or consistent read. Close stops new work and waits for accepted
    work, including when its caller is cancelled; it never disposes the Engine.
    Cancelling a write before COMMIT rolls it back after its current statement
    settles. A lost COMMIT acknowledgement is not replayed. Command IDs and stored
    occurrence identities provide the existing result when a caller repeats input.
    Schema setup uses a 30-second lock wait. The Engine owner configures other
    connection, statement, and lock-wait timeouts.

    Args:
        engine: Borrowed AsyncEngine with exclusive connection checkouts. Install
            its asynchronous database driver separately from the SQL extra.

    Raises:
        TypeError: The Engine does not provide asynchronous exclusive checkouts.
        ValueError: The database dialect is unsupported.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Validate the borrowed Engine without opening a connection."""

        SqlTransaction(engine)
        self._engine = engine
        self._dialect = engine_dialect(engine)
        self._setup_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._active: set[asyncio.Task[object]] = set()
        self._closed = False

    async def setup(self) -> None:
        """Create an empty schema or validate all existing Automation tables.

        Concurrent callers share initialization. Cancellation waits for accepted
        schema work before propagating. A later call may retry a failed setup.

        Raises:
            AutomationStoreProtocolError: Existing tables differ from the schema.
            AutomationStoreTimeout: A database statement or lock wait times out.
            AutomationStoreError: The database cannot prepare the schema.
        """

        self._ensure_open()
        task = self._setup_task
        if (
            task is not None
            and task.done()
            and isinstance(task.result(), BaseException)
        ):
            task = None
        if task is None:
            task = asyncio.create_task(
                capture(self._setup_once()), name="tinkerfin-automation-sql-setup"
            )
            self._setup_task = task
        await join_owned_task(task)

    async def _setup_once(self) -> None:
        try:
            async with SqlTransaction(
                self._engine,
                schema_lock="tinkerfin-automation-schema",
                schema_lock_timeout_seconds=_SCHEMA_LOCK_TIMEOUT_SECONDS,
                sqlite_busy_timeout_ms=_SCHEMA_LOCK_TIMEOUT_SECONDS * 1000,
            ) as connection:
                await connection.run_sync(prepare_schema)
        except AutomationError:
            raise
        except Exception as error:
            raise self._store_error("setup", error) from error

    async def _run_operation(
        self,
        operation: Callable[[Cancellation], Awaitable[_ResultT]],
    ) -> _ResultT:
        self._ensure_open()
        cancellation = Cancellation()
        task = asyncio.create_task(
            capture(operation(cancellation)), name="tinkerfin-automation-sql-operation"
        )
        self._active.add(task)
        try:
            return await join_owned_task(task, cancellation=cancellation)
        except (AutomationError, TypeError, ValueError):
            raise
        except Exception as error:
            raise self._store_error("operation", error) from error
        finally:
            self._active.discard(task)

    @asynccontextmanager
    async def _transaction(
        self, cancellation: Cancellation, *, read_only: bool = False
    ) -> AsyncGenerator[AsyncConnection]:
        transaction = SqlTransaction(self._engine, read_only=read_only)
        try:
            async with transaction as connection:
                if cancellation.error is not None:
                    raise cancellation.error
                yield connection
                if cancellation.error is not None:
                    raise cancellation.error
        except IntegrityError as error:
            # Only a clean, uncommitted uniqueness conflict may read an existing
            # command result. Recovery must not hide a connection cleanup failure.
            if transaction.cleanup_failed or transaction.commit_uncertain:
                raise self._store_error("transaction", error) from error
            raise

    async def current_time(self) -> datetime:
        """Return the database authority's UTC time."""

        self._ensure_open()

        async def operation(cancellation: Cancellation) -> datetime:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    return await self._now(connection)
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("current_time", error) from error

        return await self._run_operation(operation)

    async def create_task(
        self,
        task: AutomationTask,
        *,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationTask:
        """Create one task and its task admission scope atomically."""

        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationTask:
            try:
                async with self._transaction(cancellation) as connection:
                    previous = await self._operation_result(
                        connection,
                        task.namespace,
                        task.owner_id,
                        request_id,
                        input_digest,
                    )
                    if previous is not None:
                        return self._expect_result(previous, AutomationTask)
                    await connection.execute(insert(tasks).values(**_task_values(task)))
                    now = await self._now(connection)
                    await self._ensure_scope(
                        connection,
                        task.namespace,
                        "task",
                        task.task_id,
                        task.limits.max_concurrent_runs,
                        now,
                    )
                    await self._save_operation(
                        connection,
                        task.namespace,
                        task.owner_id,
                        request_id,
                        input_digest,
                        task,
                        now,
                    )
                    return task
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except IntegrityError as error:
                return await self._recover_idempotent_result(
                    task.namespace,
                    task.owner_id,
                    request_id,
                    input_digest,
                    AutomationTask,
                    error,
                    conflict=TaskConflictError(
                        "Task identity already exists",
                        context={"task_id": task.task_id},
                        cause=error,
                    ),
                )
            except SQLAlchemyError as error:
                raise self._store_error("create_task", error) from error

        return await self._run_operation(operation)

    async def update_task(
        self,
        task: AutomationTask,
        *,
        expected_revision: int,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationTask:
        """Replace one task after locking and checking its revision."""

        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationTask:
            try:
                async with self._transaction(cancellation) as connection:
                    previous = await self._operation_result(
                        connection,
                        task.namespace,
                        task.owner_id,
                        request_id,
                        input_digest,
                    )
                    if previous is not None:
                        return self._expect_result(previous, AutomationTask)
                    current = await self._locked_task(
                        connection, task.namespace, task.owner_id, task.task_id
                    )
                    if current.revision != expected_revision:
                        raise TaskConflictError(
                            "Task revision changed",
                            context={
                                "task_id": task.task_id,
                                "expected_revision": expected_revision,
                                "actual_revision": current.revision,
                            },
                        )
                    if task.revision != expected_revision + 1:
                        raise TaskConflictError(
                            "Replacement task must advance revision exactly once",
                            context={"task_id": task.task_id},
                        )
                    await connection.execute(
                        update(tasks)
                        .where(tasks.c.task_id == task.task_id)
                        .values(**_task_values(task))
                    )
                    now = await self._now(connection)
                    await connection.execute(
                        update(scopes)
                        .where(
                            scopes.c.namespace == task.namespace,
                            scopes.c.kind == "task",
                            scopes.c.scope_key == task.task_id,
                            scopes.c.allocated <= task.limits.max_concurrent_runs,
                        )
                        .values(
                            capacity=task.limits.max_concurrent_runs,
                            updated_at=_database_datetime(now),
                        )
                    )
                    await self._save_operation(
                        connection,
                        task.namespace,
                        task.owner_id,
                        request_id,
                        input_digest,
                        task,
                        now,
                    )
                    return task
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except IntegrityError as error:
                return await self._recover_idempotent_result(
                    task.namespace,
                    task.owner_id,
                    request_id,
                    input_digest,
                    AutomationTask,
                    error,
                    conflict=TaskConflictError(
                        "Task update conflicted with stored state",
                        context={"task_id": task.task_id},
                        cause=error,
                    ),
                )
            except SQLAlchemyError as error:
                raise self._store_error("update_task", error) from error

        return await self._run_operation(operation)

    async def delete_task(
        self,
        namespace: str,
        owner_id: str,
        task_id: str,
        *,
        expected_revision: int,
        request_id: str | None,
        input_digest: str,
    ) -> None:
        """Delete a definition and cancel only queued executions in one transaction."""

        _validate_scope(namespace, owner_id)
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> None:
            try:
                async with self._transaction(cancellation) as connection:
                    previous = await self._operation_result(
                        connection,
                        namespace,
                        owner_id,
                        request_id,
                        input_digest,
                    )
                    if previous is not None:
                        self._expect_result(previous, str)
                        return
                    task = await self._locked_task(
                        connection, namespace, owner_id, task_id
                    )
                    if task.revision != expected_revision:
                        raise TaskConflictError(
                            "Task revision changed",
                            context={
                                "task_id": task_id,
                                "expected_revision": expected_revision,
                                "actual_revision": task.revision,
                            },
                        )
                    now = await self._now(connection)
                    queued_rows = (
                        await connection.execute(
                            select(runs.c.payload, runs.c.admitted)
                            .where(
                                runs.c.namespace == namespace,
                                runs.c.owner_id == owner_id,
                                runs.c.task_id == task_id,
                                runs.c.status == ExecutionStatus.QUEUED.value,
                            )
                            .with_for_update()
                        )
                    ).mappings()
                    for row in queued_rows:
                        execution = decode_execution(self._text(row["payload"]))
                        cancelled = replace(
                            execution,
                            status=ExecutionStatus.CANCELLED,
                            finished_at=now,
                            updated_at=now,
                        )
                        await self._update_execution(
                            connection,
                            cancelled,
                            admitted=bool(row["admitted"]),
                        )
                        await self._complete_work(
                            connection, cancelled.execution_id, now
                        )
                        await self._release_scopes(connection, cancelled, now)
                    await connection.execute(
                        delete(tasks).where(tasks.c.task_id == task_id)
                    )
                    await self._save_operation(
                        connection,
                        namespace,
                        owner_id,
                        request_id,
                        input_digest,
                        task_id,
                        now,
                    )
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except IntegrityError as error:
                await self._recover_idempotent_result(
                    namespace,
                    owner_id,
                    request_id,
                    input_digest,
                    str,
                    error,
                    conflict=TaskConflictError(
                        "Task deletion conflicted with stored state",
                        context={"task_id": task_id},
                        cause=error,
                    ),
                )
            except SQLAlchemyError as error:
                raise self._store_error("delete_task", error) from error

        return await self._run_operation(operation)

    async def get_task(
        self, namespace: str, owner_id: str, task_id: str
    ) -> AutomationTask:
        """Read one task inside an explicit ownership scope."""

        _validate_scope(namespace, owner_id)
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationTask:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    row = (
                        await connection.execute(
                            select(tasks.c.payload).where(
                                tasks.c.namespace == namespace,
                                tasks.c.owner_id == owner_id,
                                tasks.c.task_id == task_id,
                            )
                        )
                    ).first()
                    if row is None:
                        raise TaskNotFoundError(
                            "Task was not found", context={"task_id": task_id}
                        )
                    return decode_task(self._text(row.payload))
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("get_task", error) from error

        return await self._run_operation(operation)

    async def list_tasks(
        self,
        namespace: str,
        owner_id: str,
        *,
        limit: int,
        cursor: str | None,
        filters: TaskFilter | None = None,
    ) -> TaskPage:
        """Read matching tasks with a query-bound position that survives deletions."""
        _validate_scope(namespace, owner_id)
        self._validate_page_limit(limit)
        self._ensure_open()
        selected = filters or TaskFilter()
        scope = query_scope(namespace, owner_id, selected)
        position = decode_cursor(cursor, scope)

        async def operation(cancellation: Cancellation) -> TaskPage:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    statement = select(tasks.c.payload).where(
                        tasks.c.namespace == namespace,
                        tasks.c.owner_id == owner_id,
                        *self._filter_tasks(selected),
                    )
                    if position is not None:
                        at, identity = position
                        statement = statement.where(
                            or_(
                                tasks.c.created_at > _database_datetime(at),
                                and_(
                                    tasks.c.created_at == _database_datetime(at),
                                    tasks.c.task_id > identity,
                                ),
                            )
                        )
                    rows = (
                        await connection.execute(
                            statement.order_by(
                                tasks.c.created_at, tasks.c.task_id
                            ).limit(limit + 1)
                        )
                    ).all()
                    decoded = [decode_task(self._text(row.payload)) for row in rows]
                    return TaskPage(
                        items=tuple(decoded[:limit]),
                        next_cursor=encode_cursor(
                            scope,
                            decoded[limit - 1].created_at,
                            decoded[limit - 1].task_id,
                        )
                        if len(decoded) > limit
                        else None,
                    )
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("list_tasks", error) from error

        return await self._run_operation(operation)

    async def summarize_tasks(
        self, namespace: str, owner_id: str, *, filters: TaskFilter | None = None
    ) -> dict[TaskStatus, int]:
        """Aggregate matching tasks in SQL without loading record payloads."""
        _validate_scope(namespace, owner_id)
        self._ensure_open()
        selected = filters or TaskFilter()

        async def operation(cancellation: Cancellation) -> dict[TaskStatus, int]:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    statement = (
                        select(tasks.c.status, func.count())
                        .where(
                            tasks.c.namespace == namespace,
                            tasks.c.owner_id == owner_id,
                            *self._filter_tasks(selected),
                        )
                        .group_by(tasks.c.status)
                    )
                    rows = (await connection.execute(statement)).all()
                    counts = {status: 0 for status in TaskStatus}
                    for row in rows:
                        counts[TaskStatus(row[0])] = row[1]
                    return counts
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("summarize_tasks", error) from error

        return await self._run_operation(operation)

    def _filter_tasks(self, filters: TaskFilter) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if filters.name_contains:
            column = tasks.c.search_name
            if self._dialect == "mysql":
                column = column.collate("utf8mb4_bin")
            conditions.append(
                column.contains(filters.name_contains.casefold(), autoescape=True)
            )
        if filters.statuses:
            conditions.append(
                tasks.c.status.in_([status.value for status in filters.statuses])
            )
        return conditions

    async def summarize_executions(
        self, namespace: str, owner_id: str, *, filters: ExecutionFilter | None = None
    ) -> dict[ExecutionStatus, int]:
        """Aggregate matching executions in SQL without loading record payloads."""
        _validate_scope(namespace, owner_id)
        self._ensure_open()
        selected = filters or ExecutionFilter()

        async def operation(cancellation: Cancellation) -> dict[ExecutionStatus, int]:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    statement = (
                        select(runs.c.status, func.count())
                        .where(
                            runs.c.namespace == namespace,
                            runs.c.owner_id == owner_id,
                            *self._filter_executions(selected),
                        )
                        .group_by(runs.c.status)
                    )
                    rows = (await connection.execute(statement)).all()
                    counts = {status: 0 for status in ExecutionStatus}
                    for row in rows:
                        counts[ExecutionStatus(row[0])] = row[1]
                    return counts
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("summarize_executions", error) from error

        return await self._run_operation(operation)

    def _filter_executions(self, filters: ExecutionFilter) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if filters.name_contains:
            column = runs.c.search_name
            if self._dialect == "mysql":
                column = column.collate("utf8mb4_bin")
            conditions.append(
                column.contains(filters.name_contains.casefold(), autoescape=True)
            )
        if filters.statuses:
            conditions.append(
                runs.c.status.in_([status.value for status in filters.statuses])
            )
        if filters.queued_from is not None:
            conditions.append(
                runs.c.queued_at >= _database_datetime(filters.queued_from)
            )
        if filters.queued_until is not None:
            conditions.append(
                runs.c.queued_at < _database_datetime(filters.queued_until)
            )
        return conditions

    async def get_scheduled_task(self, namespace: str, task_id: str) -> AutomationTask:
        """Read one task for a trusted worker inside its configured namespace."""

        _validate_persisted_text(namespace, name="namespace", maximum=128)
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationTask:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    row = (
                        await connection.execute(
                            select(tasks.c.payload).where(
                                tasks.c.namespace == namespace,
                                tasks.c.task_id == task_id,
                            )
                        )
                    ).first()
                    if row is None:
                        raise TaskNotFoundError(
                            "Task was not found", context={"task_id": task_id}
                        )
                    return decode_task(self._text(row.payload))
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("get_scheduled_task", error) from error

        return await self._run_operation(operation)

    async def list_scheduled_tasks(
        self,
        namespace: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> TaskPage:
        """List enabled tasks across owners for a trusted scheduler worker."""

        _validate_persisted_text(namespace, name="namespace", maximum=128)
        self._validate_page_limit(limit)
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> TaskPage:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    statement = select(
                        tasks.c.payload, tasks.c.created_at, tasks.c.task_id
                    ).where(
                        tasks.c.namespace == namespace,
                        tasks.c.status == TaskStatus.ENABLED.value,
                    )
                    if cursor is not None:
                        cursor_row = (
                            await connection.execute(
                                statement.with_only_columns(
                                    tasks.c.created_at, tasks.c.task_id
                                ).where(tasks.c.task_id == cursor)
                            )
                        ).first()
                        if cursor_row is None:
                            return TaskPage(())
                        statement = statement.where(
                            or_(
                                tasks.c.created_at > cursor_row.created_at,
                                and_(
                                    tasks.c.created_at == cursor_row.created_at,
                                    tasks.c.task_id > cursor_row.task_id,
                                ),
                            )
                        )
                    rows = (
                        await connection.execute(
                            statement.order_by(
                                tasks.c.created_at, tasks.c.task_id
                            ).limit(limit + 1)
                        )
                    ).all()
                    decoded = [decode_task(self._text(row.payload)) for row in rows]
                    return TaskPage(
                        items=tuple(decoded[:limit]),
                        next_cursor=(
                            decoded[limit - 1].task_id if len(decoded) > limit else None
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("list_scheduled_tasks", error) from error

        return await self._run_operation(operation)

    async def materialize_task(
        self,
        task: AutomationTask,
        *,
        expected_next_run_at: datetime,
        executions: tuple[ScheduledExecution, ...],
    ) -> MaterializationResult:
        """Persist due executions and advance one task wakeup atomically."""

        self._ensure_open()

        async def operation(cancellation: Cancellation) -> MaterializationResult:
            try:
                async with self._transaction(cancellation) as connection:
                    current = await self._locked_task(
                        connection, task.namespace, task.owner_id, task.task_id
                    )
                    if not _rules.materialization_is_current(
                        current, task, expected_next_run_at
                    ):
                        return MaterializationResult(task=current, executions=())
                    await self._ensure_scope(
                        connection,
                        task.namespace,
                        "task",
                        task.task_id,
                        task.limits.max_concurrent_runs,
                        await self._now(connection),
                    )
                    existing: set[str] = set()
                    for item in executions:
                        found = await connection.scalar(
                            select(runs.c.execution_id).where(
                                runs.c.namespace == task.namespace,
                                runs.c.occurrence_key == item.occurrence_key,
                            )
                        )
                        if found is not None:
                            existing.add(item.occurrence_key)
                    pending = _rules.pending_occurrences(executions, existing)
                    queued = await connection.scalar(
                        select(func.count())
                        .select_from(runs)
                        .where(
                            runs.c.namespace == task.namespace,
                            runs.c.task_id == task.task_id,
                            runs.c.status == ExecutionStatus.QUEUED.value,
                        )
                    )
                    _rules.require_queue_capacity(
                        queued=int(queued or 0),
                        additional=len(pending),
                        maximum=task.limits.max_queued_runs,
                        scope_id=task.task_id,
                    )
                    await connection.execute(
                        update(tasks)
                        .where(tasks.c.task_id == task.task_id)
                        .values(**_task_values(task))
                    )
                    now = await self._now(connection)
                    created: list[AutomationExecution] = []
                    for item in pending:
                        execution = item.execution
                        await connection.execute(
                            insert(runs).values(
                                **_run_values(
                                    execution,
                                    occurrence_key=item.occurrence_key,
                                    admitted=False,
                                )
                            )
                        )
                        await connection.execute(
                            insert(work_items).values(
                                work_item_id=str(uuid4()),
                                namespace=execution.namespace,
                                execution_id=execution.execution_id,
                                kind=WorkKind.EXECUTE.value,
                                status=_PENDING,
                                available_at=_database_datetime(execution.queued_at),
                                worker_id=None,
                                claim_token=None,
                                fence=0,
                                lease_until=None,
                                created_at=_database_datetime(now),
                                updated_at=_database_datetime(now),
                            )
                        )
                        created.append(execution)
                    return MaterializationResult(task=task, executions=tuple(created))
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except IntegrityError as error:
                raise RequestConflictError(
                    "Scheduled execution conflicted with stored state",
                    context={"task_id": task.task_id},
                    cause=error,
                ) from error
            except SQLAlchemyError as error:
                raise self._store_error("materialize_task", error) from error

        return await self._run_operation(operation)

    async def enqueue_execution(
        self,
        execution: AutomationExecution,
        *,
        occurrence_key: str,
        request_id: str | None,
        input_digest: str,
        expected_task_revision: int | None = None,
    ) -> AutomationExecution:
        """Create one execution, occurrence identity, and work item atomically."""

        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationExecution:
            try:
                async with self._transaction(cancellation) as connection:
                    existing = await self._enqueued_result(
                        connection, execution, occurrence_key, request_id, input_digest
                    )
                    if existing is not None:
                        return existing
                    if execution.status is not ExecutionStatus.QUEUED:
                        raise ValueError("new execution status must be queued")
                    if execution.retry_of is None and execution.task_id is not None:
                        task = await self._locked_task(
                            connection,
                            execution.namespace,
                            execution.owner_id,
                            execution.task_id,
                        )
                        _rules.require_task_revision(task, expected_task_revision)
                    elif expected_task_revision is not None:
                        raise ValueError(
                            "expected_task_revision requires new task-backed work"
                        )
                    scope_kind, scope_key = _rules.execution_scope(execution)
                    await self._ensure_scope(
                        connection,
                        execution.namespace,
                        scope_kind,
                        scope_key,
                        execution.limits.max_concurrent_runs,
                        await self._now(connection),
                    )
                    # A concurrent command may have committed while this caller
                    # waited for queue admission, even when the queue is now full.
                    existing = await self._enqueued_result(
                        connection, execution, occurrence_key, request_id, input_digest
                    )
                    if existing is not None:
                        return existing
                    queue_scope = [
                        runs.c.namespace == execution.namespace,
                        runs.c.status == ExecutionStatus.QUEUED.value,
                    ]
                    if execution.task_id is None:
                        queue_scope.extend(
                            (
                                runs.c.owner_id == execution.owner_id,
                                runs.c.task_id.is_(None),
                            )
                        )
                    else:
                        queue_scope.append(runs.c.task_id == execution.task_id)
                    queued = await connection.scalar(
                        select(func.count()).select_from(runs).where(*queue_scope)
                    )
                    _rules.require_queue_capacity(
                        queued=int(queued or 0),
                        additional=1,
                        maximum=execution.limits.max_queued_runs,
                        scope_id=execution.task_id or execution.owner_id,
                    )
                    values = _run_values(
                        execution, occurrence_key=occurrence_key, admitted=False
                    )
                    await connection.execute(insert(runs).values(**values))
                    now = await self._now(connection)
                    await connection.execute(
                        insert(work_items).values(
                            work_item_id=str(uuid4()),
                            namespace=execution.namespace,
                            execution_id=execution.execution_id,
                            kind=WorkKind.EXECUTE.value,
                            status=_PENDING,
                            available_at=_database_datetime(execution.queued_at),
                            worker_id=None,
                            claim_token=None,
                            fence=0,
                            lease_until=None,
                            created_at=_database_datetime(now),
                            updated_at=_database_datetime(now),
                        )
                    )
                    await self._save_operation(
                        connection,
                        execution.namespace,
                        execution.owner_id,
                        request_id,
                        input_digest,
                        execution,
                        now,
                    )
                    return execution
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except IntegrityError as error:
                recovered = await self._recover_occurrence_or_operation(
                    execution, occurrence_key, request_id, input_digest
                )
                if recovered is not None:
                    return recovered
                raise RequestConflictError(
                    "Execution identity conflicts with stored state",
                    context={"execution_id": execution.execution_id},
                    cause=error,
                ) from error
            except SQLAlchemyError as error:
                raise self._store_error("enqueue_execution", error) from error

        return await self._run_operation(operation)

    async def get_execution(
        self, namespace: str, owner_id: str, execution_id: str
    ) -> AutomationExecution:
        """Read one execution inside an explicit ownership scope."""

        _validate_scope(namespace, owner_id)
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationExecution:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    return await self._execution(
                        connection, namespace, owner_id, execution_id
                    )
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("get_execution", error) from error

        return await self._run_operation(operation)

    async def get_scheduled_execution(
        self, namespace: str, execution_id: str
    ) -> AutomationExecution:
        """Read one execution for a trusted worker inside its namespace."""

        _validate_persisted_text(namespace, name="namespace", maximum=128)
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationExecution:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    row = (
                        await connection.execute(
                            select(runs.c.payload).where(
                                runs.c.namespace == namespace,
                                runs.c.execution_id == execution_id,
                            )
                        )
                    ).first()
                    if row is None:
                        raise ExecutionNotFoundError(
                            "Execution was not found",
                            context={"execution_id": execution_id},
                        )
                    return decode_execution(self._text(row.payload))
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("get_scheduled_execution", error) from error

        return await self._run_operation(operation)

    async def list_executions(
        self,
        namespace: str,
        owner_id: str,
        *,
        task_id: str | None,
        limit: int,
        cursor: str | None,
        filters: ExecutionFilter | None = None,
    ) -> ExecutionPage:
        """Read matching executions with a query-bound position that survives deletions."""
        _validate_scope(namespace, owner_id)
        self._validate_page_limit(limit)
        self._ensure_open()
        selected = filters or ExecutionFilter()
        scope = query_scope(namespace, owner_id, selected, task_id)
        position = decode_cursor(cursor, scope)

        async def operation(cancellation: Cancellation) -> ExecutionPage:
            try:
                async with self._transaction(
                    cancellation, read_only=True
                ) as connection:
                    statement = select(runs.c.payload).where(
                        runs.c.namespace == namespace,
                        runs.c.owner_id == owner_id,
                        *self._filter_executions(selected),
                    )
                    if task_id is not None:
                        statement = statement.where(runs.c.task_id == task_id)
                    if position is not None:
                        at, identity = position
                        statement = statement.where(
                            or_(
                                runs.c.queued_at < _database_datetime(at),
                                and_(
                                    runs.c.queued_at == _database_datetime(at),
                                    runs.c.execution_id < identity,
                                ),
                            )
                        )
                    rows = (
                        await connection.execute(
                            statement.order_by(
                                runs.c.queued_at.desc(), runs.c.execution_id.desc()
                            ).limit(limit + 1)
                        )
                    ).all()
                    decoded = [
                        decode_execution(self._text(row.payload)) for row in rows
                    ]
                    return ExecutionPage(
                        items=tuple(decoded[:limit]),
                        next_cursor=encode_cursor(
                            scope,
                            decoded[limit - 1].queued_at,
                            decoded[limit - 1].execution_id,
                        )
                        if len(decoded) > limit
                        else None,
                    )
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("list_executions", error) from error

        return await self._run_operation(operation)

    async def claim_work(
        self,
        namespace: str,
        worker_id: str,
        *,
        limit: int,
        lease_duration: timedelta,
        global_concurrency: int,
    ) -> tuple[WorkItemClaim, ...]:
        """Claim due work using database time, row locks, tokens, and fences."""

        _validate_persisted_text(namespace, name="namespace", maximum=128)
        _validate_persisted_text(worker_id, name="worker_id", maximum=191)
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if global_concurrency < 1:
            raise ValueError("global_concurrency must be at least 1")
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> tuple[WorkItemClaim, ...]:
            try:
                async with self._transaction(cancellation) as connection:
                    now = await self._now(connection)
                    await self._recover_expired_claims(connection, namespace, now)
                    rows = (
                        await connection.execute(
                            select(work_items)
                            .where(
                                work_items.c.namespace == namespace,
                                work_items.c.status == _PENDING,
                                work_items.c.available_at <= _database_datetime(now),
                            )
                            .order_by(
                                work_items.c.available_at,
                                work_items.c.created_at,
                                work_items.c.work_item_id,
                            )
                            .limit(max(limit * 4, limit))
                            .with_for_update(
                                skip_locked=database_capabilities(
                                    connection
                                ).skip_locked
                            )
                        )
                    ).mappings()
                    selected: list[tuple[RowMapping, AutomationExecution]] = []
                    for row in rows:
                        if len(selected) >= limit:
                            break
                        execution, admitted = await self._locked_execution_by_id(
                            connection, self._text(row["execution_id"])
                        )
                        now = await self._now(connection)
                        kind = WorkKind(self._text(row["kind"]))
                        transition = _rules.claim_candidate(execution, kind, now)
                        if transition is not None:
                            await self._apply_transition(
                                connection,
                                transition,
                                admitted=admitted,
                                now=now,
                                row=row,
                            )
                            continue
                        if kind is WorkKind.EXECUTE and not await self._reserve_scopes(
                            connection,
                            execution,
                            global_concurrency,
                            now,
                            already_admitted=admitted,
                        ):
                            continue
                        selected.append((row, execution))
                    # An earlier candidate must not lose its lease while a later
                    # candidate waits for run or capacity ownership in this batch.
                    now = await self._now(connection)
                    claims: list[WorkItemClaim] = []
                    for row, execution in selected:
                        kind = WorkKind(self._text(row["kind"]))
                        if kind is WorkKind.EXECUTE and execution.queue_deadline <= now:
                            await self._expire_queued(
                                connection, row, execution, admitted=True, now=now
                            )
                            continue
                        token = secrets.token_hex(16)
                        fence = int(row["fence"]) + 1
                        lease_until = now + lease_duration
                        await connection.execute(
                            update(work_items)
                            .where(work_items.c.work_item_id == row["work_item_id"])
                            .values(
                                status=_CLAIMED,
                                worker_id=worker_id,
                                claim_token=token,
                                fence=fence,
                                lease_until=_database_datetime(lease_until),
                                updated_at=_database_datetime(now),
                            )
                        )
                        claims.append(
                            WorkItemClaim(
                                work_item_id=self._text(row["work_item_id"]),
                                namespace=namespace,
                                execution_id=execution.execution_id,
                                kind=kind,
                                claim_token=token,
                                fence=fence,
                                lease_until=lease_until,
                            )
                        )
                    return tuple(claims)
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("claim_work", error) from error

        return await self._run_operation(operation)

    async def renew_claim(
        self, claim: WorkItemClaim, *, lease_duration: timedelta
    ) -> WorkItemClaim:
        """Extend a valid fenced claim using database time."""

        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> WorkItemClaim:
            try:
                async with self._transaction(cancellation) as connection:
                    _, now = await self._valid_claim(connection, claim)
                    lease_until = now + lease_duration
                    await connection.execute(
                        update(work_items)
                        .where(work_items.c.work_item_id == claim.work_item_id)
                        .values(
                            lease_until=_database_datetime(lease_until),
                            updated_at=_database_datetime(now),
                        )
                    )
                    return replace(claim, lease_until=lease_until)
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("renew_claim", error) from error

        return await self._run_operation(operation)

    async def authorize_start(
        self,
        claim: WorkItemClaim,
        *,
        execution_timeout: timedelta,
    ) -> StartAuthorization:
        """Persist and return the one-time target start authorization."""

        if execution_timeout <= timedelta(0):
            raise ValueError("execution_timeout must be positive")
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> StartAuthorization:
            try:
                async with self._transaction(cancellation) as connection:
                    row, now = await self._valid_claim(connection, claim)
                    execution, admitted = await self._locked_execution_by_id(
                        connection, claim.execution_id
                    )
                    now = await self._claim_time(connection, claim, row)
                    authorization = _rules.authorize_start(
                        execution,
                        kind=WorkKind(self._text(row["kind"])),
                        admitted=admitted,
                        now=now,
                        execution_timeout=execution_timeout,
                        start_token=secrets.token_hex(24),
                    )
                    await self._update_execution(
                        connection, authorization.execution, admitted=True
                    )
                    return authorization
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("authorize_start", error) from error

        return await self._run_operation(operation)

    async def mark_interrupted(
        self,
        claim: WorkItemClaim,
        *,
        interrupt_ids: tuple[str, ...],
    ) -> AutomationExecution:
        """Persist interrupt identity and one deadline work item without resuming."""

        if not interrupt_ids:
            raise ValueError("interrupt_ids must not be empty")
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationExecution:
            try:
                async with self._transaction(cancellation) as connection:
                    row, now = await self._valid_claim(connection, claim)
                    execution, admitted = await self._locked_execution_by_id(
                        connection, claim.execution_id
                    )
                    now = await self._claim_time(connection, claim, row)
                    transition = _rules.interrupt_execution(
                        execution, interrupt_ids=interrupt_ids, now=now
                    )
                    return await self._apply_transition(
                        connection, transition, admitted=admitted, now=now, row=row
                    )
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("mark_interrupted", error) from error

        return await self._run_operation(operation)

    async def finish_execution(
        self,
        claim: WorkItemClaim,
        *,
        status: ExecutionStatus,
        result: JsonValue | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> AutomationExecution:
        """Settle claimed work and release capacity only at a safe terminal state."""

        if not status.is_terminal and status is not ExecutionStatus.NEEDS_ATTENTION:
            raise ValueError("finish status must be terminal or needs_attention")
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationExecution:
            try:
                async with self._transaction(cancellation) as connection:
                    row, now = await self._valid_claim(connection, claim)
                    execution, admitted = await self._locked_execution_by_id(
                        connection, claim.execution_id
                    )
                    now = await self._claim_time(connection, claim, row)
                    transition = _rules.finish_execution(
                        execution,
                        status=status,
                        result=result,
                        failure_code=failure_code,
                        failure_message=failure_message,
                        now=now,
                    )
                    return await self._apply_transition(
                        connection, transition, admitted=admitted, now=now, row=row
                    )
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except SQLAlchemyError as error:
                raise self._store_error("finish_execution", error) from error

        return await self._run_operation(operation)

    async def cancel_execution(
        self,
        namespace: str,
        owner_id: str,
        execution_id: str,
        *,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationExecution:
        """Cancel queued/interrupted work or persist running cancellation intent."""

        _validate_scope(namespace, owner_id)
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationExecution:
            try:
                async with self._transaction(cancellation) as connection:
                    previous = await self._operation_result(
                        connection,
                        namespace,
                        owner_id,
                        request_id,
                        input_digest,
                    )
                    if previous is not None:
                        return self._expect_result(previous, AutomationExecution)
                    execution, admitted = await self._locked_execution_by_id(
                        connection, execution_id, namespace=namespace, owner_id=owner_id
                    )
                    now = await self._now(connection)
                    transition = _rules.cancel_execution(execution, now)
                    updated_execution = await self._apply_transition(
                        connection, transition, admitted=admitted, now=now
                    )
                    await self._save_operation(
                        connection,
                        namespace,
                        owner_id,
                        request_id,
                        input_digest,
                        updated_execution,
                        now,
                    )
                    return updated_execution
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except IntegrityError as error:
                return await self._recover_idempotent_result(
                    namespace,
                    owner_id,
                    request_id,
                    input_digest,
                    AutomationExecution,
                    error,
                    conflict=RequestConflictError(
                        "Cancellation conflicted with stored state", cause=error
                    ),
                )
            except SQLAlchemyError as error:
                raise self._store_error("cancel_execution", error) from error

        return await self._run_operation(operation)

    async def resolve_execution(
        self,
        namespace: str,
        owner_id: str,
        execution_id: str,
        *,
        resolution: AttentionResolution,
        request_id: str,
        input_digest: str,
        reason: str,
    ) -> AutomationExecution:
        """Apply one audited terminal resolution to uncertain external work."""

        _validate_scope(namespace, owner_id)
        _validate_persisted_text(reason, name="reason")
        self._ensure_open()

        async def operation(cancellation: Cancellation) -> AutomationExecution:
            try:
                async with self._transaction(cancellation) as connection:
                    previous = await self._operation_result(
                        connection,
                        namespace,
                        owner_id,
                        request_id,
                        input_digest,
                    )
                    if previous is not None:
                        return self._expect_result(previous, AutomationExecution)
                    execution, admitted = await self._locked_execution_by_id(
                        connection, execution_id, namespace=namespace, owner_id=owner_id
                    )
                    now = await self._now(connection)
                    transition = _rules.resolve_execution(
                        execution, resolution=resolution, reason=reason, now=now
                    )
                    resolved = await self._apply_transition(
                        connection, transition, admitted=admitted, now=now
                    )
                    await self._save_operation(
                        connection,
                        namespace,
                        owner_id,
                        request_id,
                        input_digest,
                        resolved,
                        now,
                    )
                    return resolved
            except asyncio.CancelledError:
                raise
            except AutomationError:
                raise
            except IntegrityError as error:
                return await self._recover_idempotent_result(
                    namespace,
                    owner_id,
                    request_id,
                    input_digest,
                    AutomationExecution,
                    error,
                    conflict=RequestConflictError(
                        "Resolution conflicted with stored state", cause=error
                    ),
                )
            except SQLAlchemyError as error:
                raise self._store_error("resolve_execution", error) from error

        return await self._run_operation(operation)

    async def close(self) -> None:
        """Stop accepting work and wait for database operations to release connections.

        Concurrent callers share close. Cancelling a caller waits for the accepted
        setup and operations, then propagates cancellation. The borrowed Engine
        remains usable by its owner. Operation failures go to their own callers.
        """

        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                capture(self._close_once()), name="tinkerfin-automation-sql-close"
            )
        await join_owned_task(self._close_task)

    async def _close_once(self) -> None:
        pending: list[asyncio.Task[object]] = list(self._active)
        if self._setup_task is not None:
            pending.append(self._setup_task)
        for task in pending:
            # Captured outcomes are delivered by each operation's owning caller.
            # Close owns the wait, not a second delivery of the same failure.
            await asyncio.shield(task)

    async def _now(self, connection: AsyncConnection) -> datetime:
        if self._dialect == "mysql":
            expression = func.utc_timestamp(6, type_=DateTime())
        elif self._dialect == "postgresql":
            expression = func.timezone("UTC", func.clock_timestamp(), type_=DateTime())
        else:
            expression = func.strftime("%Y-%m-%d %H:%M:%f", "now", type_=DateTime())
        value = await connection.scalar(select(expression))
        if not isinstance(value, datetime) or value.tzinfo is not None:
            raise AutomationStoreProtocolError(
                "Database returned an invalid UTC timestamp"
            )
        return value.replace(tzinfo=UTC)

    async def _operation_result(
        self,
        connection: AsyncConnection,
        namespace: str,
        owner_id: str,
        request_id: str | None,
        input_digest: str,
        *,
        lock: bool = True,
    ) -> AutomationTask | AutomationExecution | str | None:
        if request_id is None:
            return None
        statement = select(
            operations.c.input_digest,
            operations.c.result_kind,
            operations.c.result_payload,
        ).where(
            operations.c.namespace == namespace,
            operations.c.owner_id == owner_id,
            operations.c.request_id == request_id,
        )
        if lock:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).first()
        if row is None:
            return None
        if row.input_digest != input_digest:
            raise RequestConflictError(
                "Request ID was reused with different input",
                context={"request_id": request_id},
            )
        try:
            return decode_operation_result(
                self._text(row.result_kind), self._text(row.result_payload)
            )
        except (TypeError, ValueError) as error:
            raise AutomationStoreProtocolError(
                "Stored operation result is invalid",
                cause=error,
            ) from error

    async def _save_operation(
        self,
        connection: AsyncConnection,
        namespace: str,
        owner_id: str,
        request_id: str | None,
        input_digest: str,
        result: AutomationTask | AutomationExecution | str,
        now: datetime,
    ) -> None:
        if request_id is None:
            return
        result_kind, result_payload = encode_operation_result(result)
        await connection.execute(
            insert(operations).values(
                namespace=namespace,
                owner_id=owner_id,
                request_id=request_id,
                input_digest=input_digest,
                result_kind=result_kind,
                result_payload=result_payload,
                created_at=_database_datetime(now),
            )
        )

    async def _recover_idempotent_result(
        self,
        namespace: str,
        owner_id: str,
        request_id: str | None,
        input_digest: str,
        expected: type[_ResultT],
        cause: IntegrityError,
        *,
        conflict: AutomationError,
    ) -> _ResultT:
        if request_id is not None:
            async with SqlTransaction(self._engine, read_only=True) as connection:
                previous = await self._operation_result(
                    connection,
                    namespace,
                    owner_id,
                    request_id,
                    input_digest,
                    lock=False,
                )
                if previous is not None:
                    return self._expect_result(previous, expected)
        raise conflict from cause

    async def _recover_occurrence_or_operation(
        self,
        execution: AutomationExecution,
        occurrence_key: str,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationExecution | None:
        async with SqlTransaction(self._engine, read_only=True) as connection:
            return await self._enqueued_result(
                connection,
                execution,
                occurrence_key,
                request_id,
                input_digest,
                lock_command=False,
            )

    async def _enqueued_result(
        self,
        connection: AsyncConnection,
        execution: AutomationExecution,
        occurrence_key: str,
        request_id: str | None,
        input_digest: str,
        *,
        lock_command: bool = True,
    ) -> AutomationExecution | None:
        previous = await self._operation_result(
            connection,
            execution.namespace,
            execution.owner_id,
            request_id,
            input_digest,
            lock=lock_command,
        )
        if previous is not None:
            return self._expect_result(previous, AutomationExecution)
        row = (
            await connection.execute(
                select(runs.c.payload).where(
                    runs.c.namespace == execution.namespace,
                    runs.c.occurrence_key == occurrence_key,
                )
            )
        ).first()
        return None if row is None else decode_execution(self._text(row.payload))

    async def _locked_task(
        self,
        connection: AsyncConnection,
        namespace: str,
        owner_id: str,
        task_id: str,
    ) -> AutomationTask:
        row = (
            await connection.execute(
                select(tasks.c.payload)
                .where(
                    tasks.c.namespace == namespace,
                    tasks.c.owner_id == owner_id,
                    tasks.c.task_id == task_id,
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            raise TaskNotFoundError("Task was not found", context={"task_id": task_id})
        return decode_task(self._text(row.payload))

    async def _execution(
        self,
        connection: AsyncConnection,
        namespace: str,
        owner_id: str,
        execution_id: str,
    ) -> AutomationExecution:
        row = (
            await connection.execute(
                select(runs.c.payload).where(
                    runs.c.namespace == namespace,
                    runs.c.owner_id == owner_id,
                    runs.c.execution_id == execution_id,
                )
            )
        ).first()
        if row is None:
            raise ExecutionNotFoundError(
                "Execution was not found", context={"execution_id": execution_id}
            )
        return decode_execution(self._text(row.payload))

    async def _locked_execution_by_id(
        self,
        connection: AsyncConnection,
        execution_id: str,
        *,
        namespace: str | None = None,
        owner_id: str | None = None,
    ) -> tuple[AutomationExecution, bool]:
        conditions = [runs.c.execution_id == execution_id]
        if namespace is not None:
            conditions.append(runs.c.namespace == namespace)
        if owner_id is not None:
            conditions.append(runs.c.owner_id == owner_id)
        row = (
            await connection.execute(
                select(runs.c.payload, runs.c.admitted)
                .where(*conditions)
                .with_for_update()
            )
        ).first()
        if row is None:
            raise ExecutionNotFoundError(
                "Execution was not found", context={"execution_id": execution_id}
            )
        return decode_execution(self._text(row.payload)), bool(row.admitted)

    async def _valid_claim(
        self, connection: AsyncConnection, claim: WorkItemClaim
    ) -> tuple[RowMapping, datetime]:
        row = (
            (
                await connection.execute(
                    select(work_items)
                    .where(work_items.c.work_item_id == claim.work_item_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        now = await self._claim_time(connection, claim, row)
        assert row is not None
        return row, now

    async def _claim_time(
        self, connection: AsyncConnection, claim: WorkItemClaim, row: RowMapping | None
    ) -> datetime:
        now = await self._now(connection)
        if (
            row is None
            or row["namespace"] != claim.namespace
            or row["execution_id"] != claim.execution_id
            or row["status"] != _CLAIMED
            or row["claim_token"] != claim.claim_token
            or int(row["fence"]) != claim.fence
            or row["lease_until"] is None
            or _utc_datetime(row["lease_until"]) <= now
        ):
            raise ClaimLostError(
                "Work item claim is no longer valid",
                context={"work_item_id": claim.work_item_id},
            )
        return now

    async def _apply_transition(
        self,
        connection: AsyncConnection,
        transition: _rules.ExecutionTransition,
        *,
        admitted: bool,
        now: datetime,
        row: RowMapping | None = None,
    ) -> AutomationExecution:
        execution = transition.execution
        await self._update_execution(connection, execution, admitted=admitted)
        if transition.complete_work == "all":
            await self._complete_work(connection, execution.execution_id, now)
        elif transition.complete_work == "current":
            assert row is not None
            await self._complete_work_item(connection, row, now)
        if transition.release_capacity:
            await self._release_scopes(connection, execution, now)
        if transition.deadline_work_at is not None:
            existing = (
                await connection.execute(
                    select(work_items.c.work_item_id).where(
                        work_items.c.namespace == execution.namespace,
                        work_items.c.execution_id == execution.execution_id,
                        work_items.c.kind == WorkKind.EXPIRE_INTERRUPT.value,
                    )
                )
            ).first()
            values = {
                "status": _PENDING,
                "available_at": _database_datetime(transition.deadline_work_at),
                "worker_id": None,
                "claim_token": None,
                "lease_until": None,
                "updated_at": _database_datetime(now),
            }
            if existing is None:
                await connection.execute(
                    insert(work_items).values(
                        work_item_id=str(uuid4()),
                        namespace=execution.namespace,
                        execution_id=execution.execution_id,
                        kind=WorkKind.EXPIRE_INTERRUPT.value,
                        fence=0,
                        created_at=_database_datetime(now),
                        **values,
                    )
                )
            else:
                await connection.execute(
                    update(work_items)
                    .where(work_items.c.work_item_id == existing.work_item_id)
                    .values(**values)
                )
        return execution

    async def _expire_queued(
        self,
        connection: AsyncConnection,
        row: RowMapping,
        execution: AutomationExecution,
        *,
        admitted: bool,
        now: datetime,
    ) -> None:
        await self._apply_transition(
            connection,
            _rules.expire_queued(execution, now),
            admitted=admitted,
            now=now,
            row=row,
        )

    async def _recover_expired_claims(
        self, connection: AsyncConnection, namespace: str, now: datetime
    ) -> None:
        rows = (
            await connection.execute(
                select(work_items)
                .where(
                    work_items.c.namespace == namespace,
                    work_items.c.status == _CLAIMED,
                    work_items.c.lease_until <= _database_datetime(now),
                )
                .with_for_update(
                    skip_locked=database_capabilities(connection).skip_locked
                )
            )
        ).mappings()
        for row in rows:
            execution, admitted = await self._locked_execution_by_id(
                connection, self._text(row["execution_id"])
            )
            transition = _rules.recover_expired_claim(
                execution, WorkKind(self._text(row["kind"])), now
            )
            if transition.complete_work == "current":
                await self._update_execution(
                    connection, transition.execution, admitted=admitted
                )
                status = _COMPLETED
            else:
                status = _PENDING
            await connection.execute(
                update(work_items)
                .where(work_items.c.work_item_id == row["work_item_id"])
                .values(
                    status=status,
                    worker_id=None,
                    claim_token=None,
                    lease_until=None,
                    updated_at=_database_datetime(now),
                )
            )

    async def _reserve_scopes(
        self,
        connection: AsyncConnection,
        execution: AutomationExecution,
        global_concurrency: int,
        now: datetime,
        *,
        already_admitted: bool,
    ) -> bool:
        if already_admitted:
            return True
        global_reserved = await self._allocate_scope(
            connection,
            execution.namespace,
            "global",
            "global",
            global_concurrency,
            now,
        )
        if not global_reserved:
            return False
        scope_kind, scope_key = _rules.execution_scope(execution)
        execution_reserved = await self._allocate_scope(
            connection,
            execution.namespace,
            scope_kind,
            scope_key,
            execution.limits.max_concurrent_runs,
            now,
        )
        if not execution_reserved:
            await self._decrement_scope(
                connection, execution.namespace, "global", "global", now
            )
            return False
        await connection.execute(
            update(runs)
            .where(runs.c.execution_id == execution.execution_id)
            .values(admitted=True, updated_at=_database_datetime(now))
        )
        return True

    async def _allocate_scope(
        self,
        connection: AsyncConnection,
        namespace: str,
        kind: str,
        scope_key: str,
        capacity: int,
        now: datetime,
    ) -> bool:
        await self._ensure_scope(connection, namespace, kind, scope_key, capacity, now)
        result = await connection.execute(
            update(scopes)
            .where(
                scopes.c.namespace == namespace,
                scopes.c.kind == kind,
                scopes.c.scope_key == scope_key,
                scopes.c.allocated < capacity,
            )
            .values(
                allocated=scopes.c.allocated + 1,
                capacity=case(
                    (scopes.c.capacity < capacity, capacity),
                    else_=scopes.c.capacity,
                ),
                updated_at=_database_datetime(now),
            )
        )
        return result.rowcount == 1

    async def _ensure_scope(
        self,
        connection: AsyncConnection,
        namespace: str,
        kind: str,
        scope_key: str,
        capacity: int,
        now: datetime,
    ) -> None:
        values = {
            "namespace": namespace,
            "kind": kind,
            "scope_key": scope_key,
            "allocated": 0,
            "capacity": capacity,
            "updated_at": _database_datetime(now),
        }
        # A no-op conflict update owns the existing scope row for the enclosing
        # transaction. Queue counts and capacity changes share this same boundary.
        if self._dialect == "mysql":
            statement = mysql_insert(scopes).values(**values)
            await connection.execute(
                statement.on_duplicate_key_update(scope_key=scopes.c.scope_key)
            )
        elif self._dialect == "postgresql":
            await connection.execute(
                postgresql_insert(scopes)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[
                        scopes.c.namespace,
                        scopes.c.kind,
                        scopes.c.scope_key,
                    ],
                    set_={"scope_key": scopes.c.scope_key},
                )
            )
        else:
            # BEGIN IMMEDIATE already owns SQLite's write admission.
            await connection.execute(
                sqlite_insert(scopes).values(**values).on_conflict_do_nothing()
            )

    async def _release_scopes(
        self,
        connection: AsyncConnection,
        execution: AutomationExecution,
        now: datetime,
    ) -> None:
        admitted = await connection.scalar(
            select(runs.c.admitted)
            .where(runs.c.execution_id == execution.execution_id)
            .with_for_update()
        )
        if not admitted:
            return
        await self._decrement_scope(
            connection, execution.namespace, "global", "global", now
        )
        scope_kind, scope_key = _rules.execution_scope(execution)
        await self._decrement_scope(
            connection, execution.namespace, scope_kind, scope_key, now
        )
        await connection.execute(
            update(runs)
            .where(runs.c.execution_id == execution.execution_id)
            .values(admitted=False, updated_at=_database_datetime(now))
        )

    async def _decrement_scope(
        self,
        connection: AsyncConnection,
        namespace: str,
        kind: str,
        scope_key: str,
        now: datetime,
    ) -> None:
        result = await connection.execute(
            update(scopes)
            .where(
                scopes.c.namespace == namespace,
                scopes.c.kind == kind,
                scopes.c.scope_key == scope_key,
                scopes.c.allocated > 0,
            )
            .values(
                allocated=scopes.c.allocated - 1,
                updated_at=_database_datetime(now),
            )
        )
        if result.rowcount != 1:
            raise AutomationStoreProtocolError(
                "Admission scope could not release allocated capacity",
                context={"scope_kind": kind},
            )

    async def _update_execution(
        self,
        connection: AsyncConnection,
        execution: AutomationExecution,
        *,
        admitted: bool,
    ) -> None:
        values = _run_values(execution, admitted=admitted)
        values.pop("execution_id")
        values.pop("namespace")
        values.pop("owner_id")
        values.pop("task_id")
        values.pop("identity_digest")
        values.pop("thread_id")
        values.pop("run_id")
        values.pop("created_at")
        result = await connection.execute(
            update(runs)
            .where(runs.c.execution_id == execution.execution_id)
            .values(**values)
        )
        if result.rowcount != 1:
            raise AutomationStoreProtocolError(
                "Execution update did not affect one row"
            )

    async def _complete_work(
        self, connection: AsyncConnection, execution_id: str, now: datetime
    ) -> None:
        await connection.execute(
            update(work_items)
            .where(
                work_items.c.execution_id == execution_id,
                work_items.c.status != _COMPLETED,
            )
            .values(
                status=_COMPLETED,
                worker_id=None,
                claim_token=None,
                lease_until=None,
                updated_at=_database_datetime(now),
            )
        )

    async def _complete_work_item(
        self, connection: AsyncConnection, row: RowMapping, now: datetime
    ) -> None:
        await connection.execute(
            update(work_items)
            .where(work_items.c.work_item_id == row["work_item_id"])
            .values(
                status=_COMPLETED,
                worker_id=None,
                claim_token=None,
                lease_until=None,
                updated_at=_database_datetime(now),
            )
        )

    @staticmethod
    def _expect_result(result: object, expected: type[_ResultT]) -> _ResultT:
        if not isinstance(result, expected):
            raise AutomationStoreProtocolError(
                "Stored operation result has an invalid type"
            )
        return result

    @staticmethod
    def _text(value: object) -> str:
        if not isinstance(value, str):
            raise AutomationStoreProtocolError("Database text value has invalid type")
        return value

    @staticmethod
    def _validate_page_limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

    def _ensure_open(self) -> None:
        if self._closed:
            raise AutomationStoreError("SQL Automation Store is closed")

    @staticmethod
    def _store_error(operation: str, cause: Exception) -> AutomationStoreError:
        error_type = (
            AutomationStoreTimeout
            if isinstance(cause, SqlTimeoutError)
            or (
                isinstance(cause, SQLAlchemyError)
                and (
                    sqlite_lock_error(cause)
                    or mysql_error_code(cause) == 1205
                    or postgresql_error_code(cause) in {"55P03", "57014"}
                )
            )
            else AutomationStoreError
        )
        return error_type(
            "Automation database operation failed",
            diagnostic_context={"operation": operation},
            cause=cause,
        )


__all__ = ["SqlAlchemyAutomationStore"]
