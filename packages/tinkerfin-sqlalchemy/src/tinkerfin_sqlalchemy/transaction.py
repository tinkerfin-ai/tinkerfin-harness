"""Single-attempt transactions that restore or discard every borrowed connection."""

from __future__ import annotations

import asyncio
import hashlib
from types import TracebackType
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import PoolProxiedConnection, StaticPool

from ._settlement import restore_caller_cancellation, select_failure, settle
from .capabilities import database_capabilities, engine_dialect, sqlite_lock_error
from .errors import SqlTransactionError


class SqlTransaction:
    """Borrow one connection for an atomic write or a consistent read snapshot.

    A successful write scope commits automatically; read scopes and failed writes
    roll back. The Engine stays caller-owned. SQLite uses explicit BEGIN IMMEDIATE
    for writes and BEGIN for reads. MySQL and PostgreSQL use explicit transactions
    even when the host Engine uses AUTOCOMMIT. Connection settings are restored
    before pool return; restoration or advisory-lock release failure discards the
    physical connection.

    The scope never retries domain operations. A storage implementation may call
    commit() itself to retain the same transaction after SQLite COMMIT BUSY, and
    inspect the outcome before applying its own retry policy. Use the yielded
    connection for statements; transaction control and connection closure belong to
    this scope. Engines must provide exclusive checkouts; StaticPool is rejected
    because one borrower can otherwise roll back another borrower's work.
    Cancellation during body I/O propagates after cleanup. Connection acquisition,
    commit, rollback, and pool return settle before repeated caller cancellation
    propagates. Driver connection and statement timeouts remain controlled by the
    Engine's owner.

    Args:
        engine: Borrowed asynchronous SQLite, MySQL, or PostgreSQL Engine.
        read_only: Select one repeatable snapshot instead of a write transaction.
        isolation_level: Optional write isolation, for batches that read one snapshot
            before writing. Read-only scopes require REPEATABLE READ; schema setup
            requires READ COMMITTED. SQLite uses its explicit transaction guarantees.
        sqlite_busy_timeout_ms: Temporary SQLite lock wait in milliseconds, or None
            to retain the host setting. The scope never retries a busy result.
        mysql_lock_wait_timeout_seconds: Temporary MySQL InnoDB row-lock wait.
        schema_lock: Stable database-local name for mutually exclusive schema setup.
            The lock and DDL must use this scope's connection.
        schema_lock_timeout_seconds: Maximum database wait for the schema lock;
            zero rejects an occupied lock immediately.

    Raises:
        TypeError: An Engine or timing argument has the wrong type.
        ValueError: A timing value, dialect, or read-only option is invalid.
        SqlTransactionError: A scope is reused or its database guarantees fail.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        read_only: bool = False,
        isolation_level: Literal["READ COMMITTED", "REPEATABLE READ"] | None = None,
        sqlite_busy_timeout_ms: int | None = None,
        mysql_lock_wait_timeout_seconds: int | None = None,
        schema_lock: str | None = None,
        schema_lock_timeout_seconds: int = 30,
    ) -> None:
        """Validate one transaction declaration without opening a connection."""

        self._dialect = engine_dialect(engine)
        if isinstance(engine.sync_engine.pool, StaticPool):
            raise TypeError(
                "SQL transactions require exclusive connection checkouts; "
                "use AsyncAdaptedQueuePool for SQLite in-memory engines"
            )
        if schema_lock_timeout_seconds is None:
            raise TypeError("schema_lock_timeout_seconds must be an integer")
        for name, value, minimum in (
            ("sqlite_busy_timeout_ms", sqlite_busy_timeout_ms, 0),
            ("mysql_lock_wait_timeout_seconds", mysql_lock_wait_timeout_seconds, 1),
            ("schema_lock_timeout_seconds", schema_lock_timeout_seconds, 0),
        ):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError(f"{name} must be an integer")
                if value < minimum:
                    raise ValueError(f"{name} must be at least {minimum}")
        if not isinstance(read_only, bool):
            raise TypeError("read_only must be a boolean")
        if isolation_level not in {None, "READ COMMITTED", "REPEATABLE READ"}:
            raise ValueError("Unsupported transaction isolation level")
        if read_only and isolation_level == "READ COMMITTED":
            raise ValueError("Read-only scopes require a repeatable snapshot")
        if schema_lock is not None:
            if not isinstance(schema_lock, str):
                raise TypeError("schema_lock must be a string or None")
            if not schema_lock or schema_lock != schema_lock.strip():
                raise ValueError("schema_lock must be a non-empty canonical name")
            schema_lock.encode("utf-8")
            if read_only:
                raise ValueError("schema setup requires a write transaction")
            if isolation_level == "REPEATABLE READ":
                raise ValueError("schema setup requires READ COMMITTED isolation")
        self._engine = engine
        self._read_only = read_only
        self._isolation_level = isolation_level or (
            "REPEATABLE READ" if read_only else "READ COMMITTED"
        )
        self._busy_timeout = sqlite_busy_timeout_ms
        self._mysql_timeout = mysql_lock_wait_timeout_seconds
        self._schema_lock = schema_lock
        self._schema_timeout = schema_lock_timeout_seconds
        self._connection: AsyncConnection | None = None
        self._pooled: PoolProxiedConnection | None = None
        self._old_sqlite_timeout: int | None = None
        self._old_sqlite_query_only: int | None = None
        self._old_mysql_timeout: int | None = None
        self._mysql_schema_lock: str | None = None
        self._used = False
        self._began = False
        self._discard = False
        self._committed = False
        self._rolled_back = False
        self._commit_uncertain = False
        self._cleanup_failed = False
        self._commit_failure: BaseException | None = None
        self._cancellations = 0

    @property
    def committed(self) -> bool:
        """Whether the database acknowledged this attempt's COMMIT."""

        return self._committed

    @property
    def rolled_back(self) -> bool:
        """Whether this attempt ended with an acknowledged ROLLBACK."""

        return self._rolled_back

    @property
    def commit_uncertain(self) -> bool:
        """Whether an issued COMMIT failed without proving that it stayed uncommitted."""

        return self._commit_uncertain

    @property
    def cleanup_failed(self) -> bool:
        """Whether rollback, settings restoration, or connection return failed.

        A storage implementation must preserve this failure instead of retrying
        the domain operation, even when physical connection disposal succeeded.

        Returns:
            True if any connection cleanup step failed.
        """

        return self._cleanup_failed

    async def __aenter__(self) -> AsyncConnection:
        """Open and initialize one owned connection checkout before exposing it.

        Returns:
            The borrowed SQLAlchemy connection inside its active transaction.

        Raises:
            SqlTransactionError: This declaration was already entered.
            BaseException: Acquisition or initialization fails after cleanup.
        """

        if self._used:
            raise SqlTransactionError("A SQL transaction declaration is single-use")
        self._used = True
        caller = asyncio.current_task()
        self._cancellations = 0 if caller is None else caller.cancelling()
        connection = self._engine.connect()
        self._connection = connection
        try:
            await settle(connection.start())
            self._pooled = await connection.get_raw_connection()
            database_capabilities(connection)
            await self._begin(connection)
        except BaseException as error:
            # Failed initialization may leave next-transaction settings or a session
            # advisory lock active even when the driver reports no open transaction.
            self._discard = True
            primary = restore_caller_cancellation(error, self._cancellations)
            await self._finish(primary)
            if primary is not error:
                raise primary
            raise
        return connection

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Commit successful work and settle all connection ownership before returning."""

        primary = (
            None
            if error is None
            else restore_caller_cancellation(error, self._cancellations)
        )
        try:
            if primary is None and not self._read_only and not self._rolled_back:
                await self.commit()
        except BaseException as failure:  # noqa: BLE001 - pool return still owns every outcome
            primary = failure
        await self._finish(primary)
        if primary is not None and primary is not error:
            raise primary

    def _require_connection(self) -> AsyncConnection:
        connection = self._connection
        if connection is None or connection.sync_connection is None:
            raise SqlTransactionError("The SQL transaction is not active")
        return connection

    async def commit(self) -> None:
        """Commit once, retaining a SQLite BUSY transaction for a domain-owned retry.

        Raises:
            SqlTransactionError: The scope is inactive, read-only, or rolled back.
            BaseException: COMMIT fails or caller cancellation follows its settlement.
        """

        connection = self._require_connection()
        if self._read_only or self._rolled_back:
            raise SqlTransactionError("This SQL transaction cannot commit")
        if self._committed:
            return
        if self._commit_uncertain:
            assert self._commit_failure is not None
            raise self._commit_failure

        async def commit_once() -> None:
            try:
                if self._dialect == "sqlite":
                    await connection.exec_driver_sql("COMMIT")
                else:
                    await connection.commit()
            except BaseException as error:
                self._commit_failure = error
                self._commit_uncertain = not (
                    self._dialect == "sqlite"
                    and sqlite_lock_error(error)
                    and not connection.invalidated
                )
                raise
            self._committed = True
            # Explicit SQL also works under host AUTOCOMMIT. Clear SQLAlchemy's
            # autobegin bookkeeping after the server acknowledged completion.
            await connection.rollback()

        await settle(commit_once())

    async def rollback(self) -> None:
        """Acknowledge rollback without returning the connection or selecting a retry."""

        connection = self._require_connection()
        if self._committed or self._rolled_back:
            return

        async def rollback_once() -> None:
            if self._began and not connection.invalidated:
                if self._dialect == "sqlite" or (
                    self._dialect == "mysql"
                    and self._read_only
                    and connection.dialect.skip_autocommit_rollback
                ):
                    # A host may disable driver rollback under AUTOCOMMIT, but the
                    # explicit read transaction still has to end before pool return.
                    await connection.exec_driver_sql("ROLLBACK")
                else:
                    await connection.rollback()
                self._rolled_back = True
            await connection.rollback()

        try:
            await settle(rollback_once())
        except BaseException:
            self._cleanup_failed = True
            self._discard = True
            raise

    async def _begin(self, connection: AsyncConnection) -> None:
        if self._dialect == "sqlite":
            if self._read_only:
                self._old_sqlite_query_only = await self._integer_setting(
                    connection, "PRAGMA query_only"
                )
                await connection.exec_driver_sql("PRAGMA query_only = 1")
                await connection.rollback()
            wait = self._busy_timeout
            if self._schema_lock is not None and wait is None:
                wait = self._schema_timeout * 1000
            if wait is not None:
                self._old_sqlite_timeout = await self._integer_setting(
                    connection, "PRAGMA busy_timeout"
                )
                await connection.exec_driver_sql(f"PRAGMA busy_timeout = {wait}")
                await connection.rollback()
            await connection.exec_driver_sql(
                "BEGIN" if self._read_only else "BEGIN IMMEDIATE"
            )
            self._began = True
            return

        if self._dialect == "mysql":
            if self._mysql_timeout is not None:
                self._old_mysql_timeout = await self._integer_setting(
                    connection, "SELECT @@SESSION.innodb_lock_wait_timeout"
                )
                await connection.exec_driver_sql(
                    f"SET SESSION innodb_lock_wait_timeout = {self._mysql_timeout}"
                )
                await connection.rollback()
            if self._schema_lock is not None:
                database = self._engine.url.database or ""
                digest = hashlib.sha256(
                    f"{database}\0{self._schema_lock}".encode()
                ).hexdigest()[:48]
                lock_name = f"tinkerfin:{digest}"
                acquired = await connection.scalar(
                    text("SELECT GET_LOCK(:name, :timeout)"),
                    {"name": lock_name, "timeout": self._schema_timeout},
                )
                if acquired != 1:
                    raise SqlTransactionError(
                        "The SQL schema lock could not be acquired"
                    )
                self._mysql_schema_lock = lock_name
                await connection.rollback()
            if self._read_only:
                await connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                await connection.exec_driver_sql("START TRANSACTION READ ONLY")
            else:
                await connection.execution_options(
                    isolation_level=self._isolation_level
                )
                await connection.begin()
        else:
            # asyncpg starts its driver transaction before preparing the first SQL
            # statement (SQLAlchemy 2.0.52 AsyncAdapt_asyncpg_cursor). Configure that
            # transaction rather than issuing a nested raw BEGIN. Pool return resets
            # the connection's isolation to the host Engine's configured default.
            await connection.execution_options(isolation_level=self._isolation_level)
            await connection.begin()
            if self._read_only:
                await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        self._began = True

        if self._dialect == "postgresql" and self._schema_lock is not None:
            lock_key = int.from_bytes(
                hashlib.sha256(self._schema_lock.encode("utf-8")).digest()[:8],
                "big",
                signed=True,
            )
            if self._schema_timeout == 0:
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key}
                )
                if not acquired:
                    raise SqlTransactionError(
                        "The SQL schema lock could not be acquired"
                    )
            else:
                await connection.execute(
                    text("SELECT set_config('lock_timeout', :timeout, true)"),
                    {"timeout": f"{self._schema_timeout * 1000}ms"},
                )
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
                )

    @staticmethod
    async def _integer_setting(connection: AsyncConnection, query: str) -> int:
        value = (await connection.exec_driver_sql(query)).scalar_one()
        if not isinstance(value, int) or value < 0:
            raise SqlTransactionError(
                "The database returned an invalid connection setting"
            )
        await connection.rollback()
        return value

    async def _invalidate(
        self, connection: AsyncConnection, error: BaseException | None
    ) -> None:
        pooled = self._pooled
        failures: list[BaseException] = []
        try:
            if (
                connection.dialect.driver == "asyncmy"
                and pooled is not None
                and pooled.is_valid
            ):
                # asyncmy marks an interrupted socket unusable. Pool proxy
                # invalidation terminates it outside that failed greenlet.
                pooled.invalidate(error)
            else:
                # aiosqlite needs awaited graceful close to release SQLite locks.
                await connection.invalidate(error)
        except BaseException as failure:  # noqa: BLE001 - verify physical disposal even when host events fail
            failures.append(failure)
        if pooled is not None and pooled.is_valid:
            try:
                await self._retire_connection(connection, pooled)
            except BaseException as failure:  # noqa: BLE001 - retain every failure from the disposal boundary
                failures.append(failure)
        try:
            await connection.rollback()
        except BaseException as failure:  # noqa: BLE001 - clear transaction bookkeeping after driver disposal
            failures.append(failure)
        if failures:
            selected = failures[0]
            for failure in failures[1:]:
                selected = select_failure(selected, failure)
            raise selected

    async def _retire_connection(
        self, connection: AsyncConnection, pooled: PoolProxiedConnection
    ) -> None:
        """Detach an unsafe live checkout before closing its actual driver connection.

        SQLAlchemy 2.0.52 _ConnectionRecord.invalidate dispatches host listeners
        before physical close. A listener failure must not return the unsafe socket
        to the pool. Public detach removes pool ownership before its own event, so
        driver disposal can finish even if that event fails as well. The dialect's
        close runs inside run_sync to await native async driver cleanup.
        """

        dbapi = pooled.dbapi_connection
        assert dbapi is not None
        failures: list[BaseException] = []
        try:
            await connection.run_sync(lambda _sync: pooled.detach())
        except BaseException as failure:  # noqa: BLE001 - detach events run after pool ownership is released
            failures.append(failure)
        if not pooled.is_detached:
            raise SqlTransactionError(
                "An unsafe SQL connection could not be detached from its pool",
                cause=failures[0] if failures else None,
            )
        try:
            await connection.run_sync(lambda sync: sync.dialect.do_close(dbapi))
        except BaseException as failure:  # noqa: BLE001 - a failed graceful close still requires termination
            failures.append(failure)
            try:
                await connection.run_sync(lambda sync: sync.dialect.do_terminate(dbapi))
            except BaseException as termination_error:  # noqa: BLE001 - retain the failed final termination
                failures.append(termination_error)
        finally:
            # A detached proxy has no pool record: invalidate clears its references
            # without invoking the failed pool event or closing the driver again.
            pooled.invalidate()
        if failures:
            selected = failures[0]
            for failure in failures[1:]:
                selected = select_failure(selected, failure)
            raise selected

    async def _finish(self, primary: BaseException | None) -> None:
        connection = self._connection
        if connection is None:
            return

        async def cleanup() -> None:
            errors: list[BaseException] = []
            try:
                if connection.sync_connection is None:
                    return
                discard = (
                    self._discard
                    or self._commit_uncertain
                    or isinstance(primary, asyncio.CancelledError)
                )
                if not discard and not connection.invalidated:
                    try:
                        await self.rollback()
                        if self._old_sqlite_timeout is not None:
                            await connection.exec_driver_sql(
                                f"PRAGMA busy_timeout = {self._old_sqlite_timeout}"
                            )
                        if self._old_sqlite_query_only is not None:
                            await connection.exec_driver_sql(
                                f"PRAGMA query_only = {self._old_sqlite_query_only}"
                            )
                        if self._old_mysql_timeout is not None:
                            await connection.exec_driver_sql(
                                f"SET SESSION innodb_lock_wait_timeout = {self._old_mysql_timeout}"
                            )
                        if self._mysql_schema_lock is not None:
                            released = await connection.scalar(
                                text("SELECT RELEASE_LOCK(:name)"),
                                {"name": self._mysql_schema_lock},
                            )
                            if released != 1:
                                raise SqlTransactionError(
                                    "The SQL schema lock could not be released"
                                )
                        await connection.rollback()
                    except BaseException as error:  # noqa: BLE001 - failed settings cannot enter the pool
                        errors.append(error)
                        discard = True
                if discard or connection.invalidated:
                    try:
                        await self._invalidate(connection, primary)
                    except BaseException as error:  # noqa: BLE001 - close still has to return the checkout
                        errors.append(error)
            finally:
                try:
                    if connection.sync_connection is not None:
                        await connection.close()
                except BaseException as error:  # noqa: BLE001 - preserve all cleanup evidence
                    errors.append(error)
                    # A failed AsyncConnection.close() is not evidence of pool
                    # return. Invalidate the physical connection, then close its
                    # already captured invalid proxy to return this checkout. That
                    # final proxy close performs no driver I/O after invalidation.
                    if not connection.closed:
                        try:
                            await self._invalidate(connection, error)
                        except BaseException as invalidate_error:  # noqa: BLE001 - retain cleanup failures together
                            errors.append(invalidate_error)
                        finally:
                            pooled = self._pooled
                            if pooled is not None and not pooled.is_valid:
                                try:
                                    pooled.close()
                                except BaseException as return_error:  # noqa: BLE001 - report a failed checkout return
                                    errors.append(return_error)
            if errors:
                self._cleanup_failed = True
                selected = errors[0]
                for error in errors[1:]:
                    selected = select_failure(selected, error)
                raise selected

        try:
            await settle(cleanup())
        except BaseException as cleanup_error:
            if primary is None:
                raise
            selected = select_failure(primary, cleanup_error)
            if selected is not primary:
                raise selected
        finally:
            self._connection = None
