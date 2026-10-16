"""Optional SQLAlchemy Core implementation of OpenSandbox allocation state."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from typing import TypeVar, cast
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
)

from tinkerfin_sqlalchemy import (
    DatabaseCapabilities,
    SqlDialect,
    SqlTransaction,
    engine_dialect,
)

from ..errors import (
    OpenSandboxError,
    OpenSandboxStateError,
    OpenSandboxStateTimeoutError,
    OpenSandboxStateUnavailableError,
    UnexpectedOpenSandboxStateError,
)
from ..models import OpenSandboxPurpose
from . import _sql_availability, _sql_schema, _sql_state_ops, _sql_transactions
from ._sql_schema import _cleanup as _cleanup
from ._sql_schema import _warm_slots as _warm_slots
from ._sql_schema import _workers
from ._sql_tasks import TaskOutcome, capture, join_owned_task, select_failure
from .availability import (
    OpenSandboxAvailability,
    OpenSandboxAvailabilityPhase,
    OpenSandboxHolderUpdate,
)
from .state import (
    OpenSandboxBinding,
    OpenSandboxCleanupClaim,
    OpenSandboxOwnerClaim,
    OpenSandboxReadyWarmClaim,
    OpenSandboxState,
    OpenSandboxWarmClaim,
)

_ResultT = TypeVar("_ResultT")
_AsyncFunctionT = TypeVar(
    "_AsyncFunctionT",
    bound=Callable[..., Awaitable[object]],
)


def _state_operation(
    operation: str,
) -> Callable[[_AsyncFunctionT], _AsyncFunctionT]:
    """Translate SQLAlchemy and driver failures at each public State boundary."""

    def decorate(
        function: _AsyncFunctionT,
    ) -> _AsyncFunctionT:
        @wraps(function)
        async def wrapped(*args: object, **kwargs: object) -> object:
            state = cast(SQLAlchemyOpenSandboxState, args[0])
            diagnostic_context = {
                "implementation": "sqlalchemy",
                "dialect": state._dialect,
                "operation": operation,
            }
            try:
                return await function(*args, **kwargs)
            except OpenSandboxError as error:
                error._enrich_diagnostic_context(diagnostic_context)
                raise
            except SQLAlchemyTimeoutError as error:
                translated = OpenSandboxStateTimeoutError(
                    f"OpenSandbox State {operation} timed out",
                    diagnostic_context=diagnostic_context,
                    cause=error,
                )
                raise translated from error
            except DBAPIError as error:
                error_type = (
                    OpenSandboxStateUnavailableError
                    if error.connection_invalidated
                    else UnexpectedOpenSandboxStateError
                )
                translated = error_type(
                    f"OpenSandbox State {operation} failed",
                    diagnostic_context=diagnostic_context,
                    cause=error,
                )
                raise translated from error
            except (SQLAlchemyError, OSError) as error:
                translated = OpenSandboxStateUnavailableError(
                    f"OpenSandbox State {operation} is unavailable",
                    diagnostic_context=diagnostic_context,
                    cause=error,
                )
                raise translated from error
            except (TypeError, ValueError):
                raise
            except Exception as error:
                translated = UnexpectedOpenSandboxStateError(
                    f"OpenSandbox State {operation} failed",
                    diagnostic_context=diagnostic_context,
                    cause=error,
                )
                raise translated from error

        return cast(_AsyncFunctionT, wrapped)

    return decorate


@dataclass(frozen=True, slots=True)
class SQLAlchemyOpenSandboxStateSchema:
    """Describe one complete deployable OpenSandbox State database schema.

    Attributes:
        dialect: SQL dialect accepted by the deployment script.
        table_names: Complete table set in deterministic creation order.
        ddl: Full empty-database DDL including indexes.
    """

    dialect: SqlDialect
    table_names: tuple[str, ...]
    ddl: str


def get_sqlalchemy_opensandbox_state_schema(
    *,
    dialect: SqlDialect,
) -> SQLAlchemyOpenSandboxStateSchema:
    """Build the complete OpenSandbox State schema without database I/O.

    Args:
        dialect: Deployment SQL dialect. MySQL output also supports MySQL 5.7.

    Returns:
        An immutable descriptor containing deterministic full-database DDL.

    Raises:
        ValueError: The requested dialect is unsupported.
    """

    return _sql_schema.get_sqlalchemy_opensandbox_state_schema(
        dialect=dialect,
    )


class SQLAlchemyOpenSandboxState(OpenSandboxState):
    """Persist Sandbox bindings, claims, availability, and cleanup through SQLAlchemy.

    The State borrows an asynchronous SQLite, MySQL, or PostgreSQL Engine. It owns
    worker registration and renewal, and never disposes or reconfigures the Engine.
    Start the State through OpenSandboxManager; closing the manager settles these
    State resources while persistent bindings remain available to other workers.

    Args:
        engine: Borrowed asynchronous SQLAlchemy Engine with exclusive checkouts.
        namespace: Deployment domain shared by cooperating Sandbox managers.
        lease_ttl: Worker and claim expiry duration in seconds.
        poll_interval: Delay between contested claim attempts in seconds.
        sqlite_retry_timeout: Maximum SQLite lock retry time in seconds. A known
            busy COMMIT retries in the same transaction; an unknown outcome never
            replays a mutation. Driver timeouts are controlled by the Engine owner.

    Raises:
        TypeError: The Engine or timing arguments have the wrong type.
        ValueError: The namespace, timing values, or SQL dialect are unsupported.
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        namespace: str = "",
        lease_ttl: float = 15.0,
        poll_interval: float = 0.05,
        sqlite_retry_timeout: float = 5.0,
    ) -> None:
        """Validate a borrowed Engine and State policy without database I/O."""

        self._dialect = engine_dialect(engine)
        SqlTransaction(engine)
        if not isinstance(namespace, str):
            raise TypeError("namespace must be a string")
        if len(namespace) > 64:
            raise ValueError("namespace must contain at most 64 characters")
        if isinstance(lease_ttl, bool) or not isinstance(lease_ttl, int | float):
            raise TypeError("lease_ttl must be a number")
        if isinstance(poll_interval, bool) or not isinstance(
            poll_interval, int | float
        ):
            raise TypeError("poll_interval must be a number")
        resolved_lease_ttl = float(lease_ttl)
        resolved_poll_interval = float(poll_interval)
        if not math.isfinite(resolved_lease_ttl) or resolved_lease_ttl <= 0:
            raise ValueError("lease_ttl must be a finite positive number")
        if not math.isfinite(resolved_poll_interval) or resolved_poll_interval <= 0:
            raise ValueError("poll_interval must be a finite positive number")
        if isinstance(sqlite_retry_timeout, bool) or not isinstance(
            sqlite_retry_timeout, int | float
        ):
            raise TypeError("sqlite_retry_timeout must be a number")
        resolved_sqlite_retry_timeout = float(sqlite_retry_timeout)
        if (
            not math.isfinite(resolved_sqlite_retry_timeout)
            or resolved_sqlite_retry_timeout < 0
        ):
            raise ValueError("sqlite_retry_timeout must be finite and non-negative")
        self._namespace = namespace
        self._lease_ttl = resolved_lease_ttl
        self._poll_interval = resolved_poll_interval
        self._sqlite_retry_timeout = resolved_sqlite_retry_timeout
        self._worker_id = uuid4().hex
        self._worker_lease_ttl = resolved_lease_ttl
        self._worker_renew_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._worker_failure: BaseException | None = None
        self._engine = engine
        self._capabilities: DatabaseCapabilities | None = None
        self._start_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._worker_may_exist = False
        self._started = False
        self._warm_pool_size: int | None = None
        self._closed = False

    @property
    def persistent(self) -> bool:
        """SQL bindings and shared resources survive manager shutdown."""
        return True

    @property
    def lease_renew_interval(self) -> float:
        """Renew active claims three times within each lease period."""
        return self._lease_ttl / 3

    def _ensure_open(self) -> None:
        if not self._started:
            raise OpenSandboxStateError("OpenSandbox state has not been started")
        if self._closed:
            raise OpenSandboxStateError("OpenSandbox state is closed")
        if self._worker_failure is not None:
            if not isinstance(self._worker_failure, Exception):
                raise self._worker_failure
            raise OpenSandboxStateError(
                "OpenSandbox worker registration is no longer valid"
            ) from self._worker_failure

    def _require_capabilities(self) -> DatabaseCapabilities:
        """Return capabilities established before schema initialization."""
        capabilities = self._capabilities
        if capabilities is None:
            raise OpenSandboxStateError(
                "OpenSandbox database capabilities have not been initialized"
            )
        return capabilities

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    def _is_retryable_claim_conflict(self, error: DBAPIError) -> bool:
        return _sql_transactions._is_retryable_claim_conflict(self, error)

    async def _run_write_transaction(
        self,
        operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
    ) -> _ResultT:
        """Retry only rolled-back SQLite lock conflicts within one deadline."""

        return await _sql_transactions._run_write_transaction(
            self,
            operation,
        )

    async def _run_claim_transaction(
        self,
        operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
    ) -> _ResultT | None:
        """Run one claim operation and preserve MySQL 5.7 timeout semantics."""

        return await _sql_transactions._run_claim_transaction(
            self,
            operation,
        )

    @_state_operation("start")
    async def start(self, *, warm_pool_size: int) -> None:
        """Create the schema through one instance-owned startup attempt.

        Concurrent callers using the same capacity share the shielded attempt. Caller
        cancellation does not cancel database work that may already own a worker row.
        A capacity conflict rolls back before worker registration and can be retried
        after the conflicting workers close. Failures with uncertain commit state
        remain attached to this instance so a retry cannot duplicate ownership.

        Args:
            warm_pool_size: Database-global warm-slot capacity for this namespace.

        Raises:
            OpenSandboxStateConfigurationError: This instance or another active worker
                uses a different capacity.
            OpenSandboxStateError: The State is closed or cannot initialize safely.
            ValueError: The capacity is negative.
        """

        return await _sql_transactions.start(
            self,
            warm_pool_size=warm_pool_size,
        )

    @_state_operation("acquire_owner")
    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        """Wait until this worker atomically owns the next owner generation."""

        return await _sql_state_ops.acquire_owner(
            self,
            owner_key,
        )

    @_state_operation("bind_owner")
    async def bind_owner(
        self,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
        *,
        purpose: OpenSandboxPurpose,
    ) -> OpenSandboxBinding:
        """Commit a binding with claim token, generation, and lease fencing."""

        return await _sql_state_ops.bind_owner(
            self,
            claim,
            sandbox_id,
            purpose=purpose,
        )

    @_state_operation("renew_owner")
    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool:
        """Extend a current, unexpired owner lease."""

        return await _sql_state_ops.renew_owner(
            self,
            claim,
        )

    @_state_operation("unbind_owner")
    async def unbind_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Remove a binding only while the owner claim remains current."""

        return await _sql_state_ops.unbind_owner(
            self,
            claim,
        )

    @_state_operation("read_binding")
    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        """Read the latest committed owner binding without claiming it."""

        return await _sql_state_ops.read_binding(
            self,
            owner_key,
        )

    @_state_operation("release_owner")
    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Release only the exact owner claim supplied by the caller."""

        return await _sql_state_ops.release_owner(
            self,
            claim,
        )

    @_state_operation("register_holder")
    async def register_holder(
        self, claim: OpenSandboxOwnerClaim, holder_id: str
    ) -> OpenSandboxAvailability:
        """Register before publishing a handle under the current running intent."""
        return await _sql_availability.register_holder(self, claim, holder_id)

    @_state_operation("read_availability")
    async def read_availability(self, owner_key: str) -> OpenSandboxAvailability | None:
        """Read current intent without waiting for an active owner claim."""
        return await _sql_availability.read_availability(self, owner_key)

    @_state_operation("get_holder_updates")
    async def get_holder_updates(
        self, holder_id: str
    ) -> tuple[OpenSandboxHolderUpdate, ...]:
        """Read all registrations and availability intents for one manager."""
        return await _sql_availability.get_holder_updates(self, holder_id)

    @_state_operation("change_availability")
    async def change_availability(
        self,
        claim: OpenSandboxOwnerClaim,
        expected: OpenSandboxAvailability,
        *,
        phase: OpenSandboxAvailabilityPhase,
        refresh_connection: bool = False,
    ) -> OpenSandboxAvailability:
        """Advance the exact expected intent under the current owner fence."""
        return await _sql_availability.change_availability(
            self, claim, expected, phase=phase, refresh_connection=refresh_connection
        )

    @_state_operation("acknowledge_idle")
    async def acknowledge_idle(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> bool:
        """Acknowledge a current drain after admission closes and operations settle."""
        return await _sql_availability.acknowledge_idle(self, holder_id, availability)

    @_state_operation("holders_are_idle")
    async def holders_are_idle(
        self, claim: OpenSandboxOwnerClaim, availability: OpenSandboxAvailability
    ) -> bool:
        """Require explicit current-drain acknowledgements from every holder."""
        return await _sql_availability.holders_are_idle(self, claim, availability)

    @_state_operation("unregister_holder")
    async def unregister_holder(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> None:
        """Release an exact binding registration after proving local idle."""
        return await _sql_availability.unregister_holder(self, holder_id, availability)

    @_state_operation("claim_warm_slot")
    async def claim_warm_slot(self) -> OpenSandboxWarmClaim | None:
        """Claim one empty or abandoned global warm-pool slot."""

        return await _sql_state_ops.claim_warm_slot(
            self,
        )

    @_state_operation("claim_ready_warm_slot")
    async def claim_ready_warm_slot(
        self,
        *,
        exclude_slots: Sequence[int],
    ) -> OpenSandboxReadyWarmClaim | None:
        """Fence one published slot for remote health and expiry reconciliation."""

        return await _sql_state_ops.claim_ready_warm_slot(
            self,
            exclude_slots=exclude_slots,
        )

    @_state_operation("discard_ready_warm_slot")
    async def discard_ready_warm_slot(
        self,
        claim: OpenSandboxReadyWarmClaim,
    ) -> None:
        """Clear one unusable published slot and retain durable cleanup."""

        await _sql_state_ops.discard_ready_warm_slot(self, claim)

    @_state_operation("warm_pool_ready")
    async def warm_pool_ready(self) -> bool:
        """Return whether every configured slot retains a published Sandbox."""

        return await _sql_state_ops.warm_pool_ready(self)

    @_state_operation("publish_warm")
    async def publish_warm(
        self,
        claim: OpenSandboxWarmClaim,
        sandbox_id: str,
    ) -> None:
        """Publish a remote Sandbox only through the current warm claim."""

        return await _sql_state_ops.publish_warm(
            self,
            claim,
            sandbox_id,
        )

    @_state_operation("renew_warm")
    async def renew_warm(self, claim: OpenSandboxWarmClaim) -> bool:
        """Extend a current, unexpired warm-slot lease."""

        return await _sql_state_ops.renew_warm(
            self,
            claim,
        )

    @_state_operation("release_warm")
    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        """Release only the exact uncommitted warm claim."""

        return await _sql_state_ops.release_warm(
            self,
            claim,
        )

    @_state_operation("consume_warm")
    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        """Atomically consume and authoritatively bind one ready global slot."""

        return await _sql_state_ops.consume_warm(
            self,
            claim,
        )

    async def _enqueue_cleanup_in_transaction(
        self,
        connection: AsyncConnection,
        sandbox_id: str,
        *,
        now: datetime,
    ) -> None:
        return await _sql_state_ops._enqueue_cleanup_in_transaction(
            self,
            connection,
            sandbox_id,
            now=now,
        )

    @_state_operation("enqueue_cleanup")
    async def enqueue_cleanup(self, sandbox_id: str) -> None:
        """Persist an idempotent remote-destruction retry target."""

        return await _sql_state_ops.enqueue_cleanup(
            self,
            sandbox_id,
        )

    @_state_operation("claim_cleanup")
    async def claim_cleanup(self) -> OpenSandboxCleanupClaim | None:
        """Claim one pending or abandoned cleanup target."""

        return await _sql_state_ops.claim_cleanup(
            self,
        )

    @_state_operation("renew_cleanup")
    async def renew_cleanup(self, claim: OpenSandboxCleanupClaim) -> bool:
        """Extend a current, unexpired cleanup lease."""

        return await _sql_state_ops.renew_cleanup(
            self,
            claim,
        )

    @_state_operation("complete_cleanup")
    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Delete a cleanup row only after its claimant confirms destruction."""

        return await _sql_state_ops.complete_cleanup(
            self,
            claim,
        )

    @_state_operation("release_cleanup")
    async def release_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Release one failed cleanup claim for a future retry."""

        return await _sql_state_ops.release_cleanup(
            self,
            claim,
        )

    @_state_operation("shutdown_sandbox_ids")
    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        """Keep durable bindings, warm slots, and cleanup work on shutdown."""

        return await _sql_state_ops.shutdown_sandbox_ids(
            self,
        )

    @_state_operation("close")
    async def aclose(self) -> None:
        """Settle accepted startup and release State resources once.

        Concurrent callers share close. Cancellation waits for worker renewal to
        stop and unregisters a worker whose registration may have committed. The
        borrowed Engine remains usable; durable Sandbox bindings are retained.

        Raises:
            OpenSandboxStateError: Worker unregistration fails after renewal stops.
            BaseException: Cancellation or process control follows resource cleanup.
        """

        close_task = self._close_task
        if close_task is None:
            self._closed = True
            close_task = asyncio.create_task(
                capture(self._aclose_once()),
                name=f"tinkerfin-opensandbox-close:{self._worker_id}",
            )
            self._close_task = close_task
        await join_owned_task(close_task)

    async def _aclose_once(self) -> None:
        failure: BaseException | None = None
        start_task = self._start_task
        if start_task is not None:
            try:
                await join_owned_task(start_task)
            except Exception:  # noqa: BLE001 - startup failure already belongs to its caller
                # The startup caller receives ordinary failure; registration still
                # has to be removed if its COMMIT acknowledgement was uncertain.
                pass
            except BaseException as error:  # noqa: BLE001 - complete ownership cleanup before delivery
                failure = error
        renew_task = self._worker_renew_task
        if renew_task is not None:
            cancel_renewal = asyncio.get_running_loop().call_soon(renew_task.cancel)
            try:
                await join_owned_task(renew_task)
            except BaseException as error:  # noqa: BLE001 - unregister even when renewal failed
                if not isinstance(error, asyncio.CancelledError):
                    failure = (
                        error if failure is None else select_failure(failure, error)
                    )
            finally:
                cancel_renewal.cancel()
            self._worker_renew_task = None
        worker_failure = self._worker_failure
        if worker_failure is not None and not isinstance(worker_failure, Exception):
            failure = (
                worker_failure
                if failure is None
                else select_failure(failure, worker_failure)
            )
        if self._worker_may_exist:

            async def unregister(connection: AsyncConnection) -> None:
                await connection.execute(
                    delete(_workers).where(
                        _workers.c.namespace == self._namespace,
                        _workers.c.worker_id == self._worker_id,
                    )
                )

            try:
                await self._run_write_transaction(unregister)
            except BaseException as error:  # noqa: BLE001 - the caller must observe an unconfirmed close
                failure = error if failure is None else select_failure(failure, error)
        if failure is not None:
            raise failure
