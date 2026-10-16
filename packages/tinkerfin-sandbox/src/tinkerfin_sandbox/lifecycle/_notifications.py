"""Own bounded notification delivery and suppress repeated outage observations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextvars import Context, ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from ..errors import (
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxInitializationError,
    OpenSandboxObserverReentryError,
    OpenSandboxStateError,
)
from .notifications import (
    OpenSandboxLifecycleEvent,
    OpenSandboxLifecycleObserver,
    OpenSandboxNotificationOptions,
)
from .notifications import (
    OpenSandboxLifecycleEventType as EventType,
)
from .notifications import (
    OpenSandboxLifecycleReason as Reason,
)

logger = logging.getLogger("tinkerfin.sandbox.notifications")
__all__ = ["_LifecycleNotifications"]
_observer_owners: ContextVar[tuple[object, ...]] = ContextVar(
    "tinkerfin_sandbox_observer_owners", default=()
)
_Failure = Literal[
    "queue_full", "callback_failed", "callback_timeout", "callback_cancelled"
]
_MAX_DIAGNOSTIC_COUNT = 2**31 - 1
_DIAGNOSTIC_INTERVAL = 60.0


def failure_reason(error: Exception) -> Reason:
    """Read declared categories only; provider text is never a lifecycle reason."""
    if isinstance(error, OpenSandboxInitializationError):
        return Reason.INITIALIZATION_FAILED
    if isinstance(error, OpenSandboxBackendProtocolError):
        return Reason.PROTOCOL_ERROR
    if isinstance(error, OpenSandboxBackendTimeoutError | TimeoutError):
        return Reason.TIMEOUT
    if isinstance(error, OpenSandboxStateError):
        return Reason.STATE_FAILURE
    if isinstance(error, OpenSandboxBackendError):
        reason = error.context.get("reason")
        if isinstance(reason, str) and reason in {
            "not_found",
            "unreachable",
            "unhealthy",
            "timeout",
            "authentication",
            "permission",
            "provider_rejected",
        }:
            return Reason(reason)
    return Reason.UNEXPECTED_FAILURE


class _ObserverDelivery:
    """Drain one borrowed observer independently of Sandbox operation tasks."""

    def __init__(
        self,
        owner: object,
        observer: OpenSandboxLifecycleObserver,
        options: OpenSandboxNotificationOptions,
        index: int,
    ) -> None:
        self._owner = owner
        self._observer = observer
        self._options = options
        self._index = index
        self._pending: asyncio.Queue[OpenSandboxLifecycleEvent] = asyncio.Queue(
            maxsize=options.max_pending_events
        )
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._diagnostics: dict[_Failure, int] = {}
        self._next_diagnostic_at = 0.0

    def publish(self, event: OpenSandboxLifecycleEvent) -> None:
        if self._closing:
            return
        try:
            self._pending.put_nowait(event)
        except asyncio.QueueFull:
            self._record_failure("queue_full")
        self._wakeup.set()
        if self._task is None:
            # Creation is lazy: a manager without events has no delivery tasks.
            # Clearing the inherited observer context prevents unrelated nested
            # managers from acquiring a false reentry restriction.
            self._task = asyncio.create_task(
                self._run(),
                name=f"tinkerfin-sandbox-notifications:{self._index}",
                context=Context(),
            )

    def _record_failure(self, failure: _Failure) -> None:
        self._diagnostics[failure] = min(
            self._diagnostics.get(failure, 0) + 1, _MAX_DIAGNOSTIC_COUNT
        )

    def _report_failures(self) -> None:
        if not self._diagnostics:
            return
        now = asyncio.get_running_loop().time()
        if now < self._next_diagnostic_at:
            return
        self._next_diagnostic_at = now + _DIAGNOSTIC_INTERVAL
        counts = self._diagnostics
        self._diagnostics = {}
        # Only this worker logs delivery failures. It holds no resource/State lock,
        # and fixed categories plus saturated counters bound volume and field size.
        # Host logging handlers must not change the outcome of notification delivery.
        try:
            logger.warning(
                "Sandbox lifecycle notification delivery was incomplete",
                extra={
                    "tinkerfin_observer_index": self._index,
                    "tinkerfin_queue_full": counts.get("queue_full", 0),
                    "tinkerfin_callback_failed": counts.get("callback_failed", 0),
                    "tinkerfin_callback_timeout": counts.get("callback_timeout", 0),
                    "tinkerfin_callback_cancelled": counts.get("callback_cancelled", 0),
                },
            )
        except Exception:  # noqa: BLE001 - diagnostics cannot affect resource outcomes
            pass

    async def _deliver(self, event: OpenSandboxLifecycleEvent) -> None:
        async def invoke() -> None:
            token = _observer_owners.set((self._owner,))
            try:
                await self._observer.on_sandbox_event(event)
            finally:
                _observer_owners.reset(token)

        callback = asyncio.create_task(invoke(), name="tinkerfin-sandbox-observer")
        try:
            async with asyncio.timeout(self._options.timeout):
                await callback
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # An observer may raise CancelledError without cancellation of this
            # owned worker. Isolate that callback outcome and keep its queue alive.
            self._record_failure("callback_cancelled")
        except TimeoutError:
            self._record_failure("callback_timeout")
        except Exception:  # noqa: BLE001 - observer failures never alter operations
            self._record_failure("callback_failed")

    async def _run(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            self._report_failures()
            while not self._pending.empty():
                event = self._pending.get_nowait()
                try:
                    await self._deliver(event)
                finally:
                    self._pending.task_done()
                self._report_failures()
            if self._closing:
                return

    def finish(self) -> asyncio.Task[None] | None:
        self._closing = True
        self._wakeup.set()
        return self._task


@dataclass(eq=False, slots=True)
class _HealthCheck:
    """Invalidate an in-flight snapshot when a newer lifecycle fact is published."""

    current: bool = True


@dataclass(slots=True)
class _Outage:
    sandbox_id: str
    reason: Reason
    workspace_may_have_changed: bool = False
    recovering: bool = False
    failure: tuple[Reason, bool] | None = None


class _LifecycleNotifications:
    """Keep outage deduplication local to this manager and own its delivery tasks.

    Outage entries exist only until availability or explicit destruction. Health
    tickets exist only while checks are active; publication invalidates them without
    retaining historical owner tombstones. Queue acceptance never awaits callbacks.
    """

    def __init__(
        self,
        observers: Sequence[OpenSandboxLifecycleObserver],
        options: OpenSandboxNotificationOptions,
    ) -> None:
        self._deliveries = [
            _ObserverDelivery(self, observer, options, index)
            for index, observer in enumerate(observers)
        ]
        self._outages: dict[str, _Outage] = {}
        self._checks: dict[str, set[_HealthCheck]] = {}
        self._warm_degraded = False

    def check_reentry(self) -> None:
        if self in _observer_owners.get():
            raise OpenSandboxObserverReentryError(
                "A lifecycle observer cannot operate or close its own Sandbox manager"
            )

    def _emit(
        self,
        kind: EventType,
        owner_key: str | None,
        reason: Reason,
        *,
        sandbox_id: str | None = None,
        previous_sandbox_id: str | None = None,
        workspace_may_have_changed: bool = False,
    ) -> None:
        if not self._deliveries:
            return
        from ._identity import SandboxResourceIdentity

        resource = (
            None if owner_key is None else SandboxResourceIdentity.from_key(owner_key)
        )
        diagnostic: dict[str, str] = {}
        if sandbox_id is not None:
            diagnostic["sandbox_id"] = sandbox_id
        if previous_sandbox_id is not None:
            diagnostic["previous_sandbox_id"] = previous_sandbox_id
        event = OpenSandboxLifecycleEvent(
            event_id=uuid4().hex,
            type=kind,
            owner_key=None if resource is None else resource.key,
            namespace=None if resource is None else resource.namespace,
            occurred_at=datetime.now(UTC),
            reason=reason,
            workspace_may_have_changed=workspace_may_have_changed,
            diagnostic_context=diagnostic,
        )
        for delivery in self._deliveries:
            delivery.publish(event)

    def begin_check(self, owner_key: str) -> _HealthCheck:
        check = _HealthCheck()
        if self._deliveries:
            self._checks.setdefault(owner_key, set()).add(check)
        return check

    def end_check(self, owner_key: str, check: _HealthCheck) -> None:
        checks = self._checks.get(owner_key)
        if checks is not None:
            checks.discard(check)
            if not checks:
                self._checks.pop(owner_key, None)

    def invalidate_checks(self, owner_key: str) -> None:
        for check in self._checks.pop(owner_key, ()):
            check.current = False

    def checked(
        self,
        owner_key: str,
        sandbox_id: str,
        check: _HealthCheck,
        reason: Reason | None,
    ) -> None:
        if not check.current:
            return
        if reason is None:
            self.available(owner_key, sandbox_id)
        else:
            self.unavailable(owner_key, sandbox_id, reason)

    def unavailable(
        self,
        owner_key: str,
        sandbox_id: str,
        reason: Reason,
        *,
        workspace_may_have_changed: bool = False,
    ) -> None:
        if not self._deliveries:
            return
        self.invalidate_checks(owner_key)
        workspace_may_have_changed |= reason in {
            Reason.NOT_FOUND,
            Reason.INITIALIZATION_FAILED,
        }
        outage = self._outages.get(owner_key)
        if outage is not None and outage.sandbox_id == sandbox_id:
            previous = (outage.reason, outage.workspace_may_have_changed)
            outage.reason = reason
            outage.workspace_may_have_changed |= workspace_may_have_changed
            if previous == (outage.reason, outage.workspace_may_have_changed):
                return
        else:
            outage = _Outage(sandbox_id, reason, workspace_may_have_changed)
            self._outages[owner_key] = outage
        self._emit(
            EventType.UNAVAILABLE,
            owner_key,
            reason,
            sandbox_id=sandbox_id,
            workspace_may_have_changed=outage.workspace_may_have_changed,
        )

    def recovering(self, owner_key: str, sandbox_id: str, reason: Reason) -> None:
        if not self._deliveries:
            return
        outage = self._outages.get(owner_key)
        if outage is None or outage.sandbox_id != sandbox_id:
            outage = _Outage(sandbox_id, reason)
            self._outages[owner_key] = outage
        if reason is Reason.EXPLICIT_RECREATE:
            outage.reason = reason
        # Reconnection may run caller initializers even when identity is retained.
        # Carry possible effects through failure or recovery; neither rolls them back.
        outage.workspace_may_have_changed = True
        if not outage.recovering:
            outage.recovering = True
            self._emit(
                EventType.RECOVERING,
                owner_key,
                reason,
                sandbox_id=sandbox_id,
                workspace_may_have_changed=True,
            )

    def recovery_started(self, owner_key: str, sandbox_id: str) -> None:
        outage = self._outages.get(owner_key)
        if outage is not None and outage.sandbox_id == sandbox_id:
            self.recovering(owner_key, sandbox_id, outage.reason)

    def failed(self, owner_key: str, sandbox_id: str, reason: Reason) -> None:
        if not self._deliveries:
            return
        outage = self._outages.get(owner_key)
        if outage is None or outage.sandbox_id != sandbox_id:
            outage = _Outage(sandbox_id, reason)
            self._outages[owner_key] = outage
        outage.workspace_may_have_changed |= reason in {
            Reason.NOT_FOUND,
            Reason.INITIALIZATION_FAILED,
        }
        failure = (reason, outage.workspace_may_have_changed)
        if outage.failure != failure:
            outage.failure = failure
            self._emit(
                EventType.RECOVERY_FAILED,
                owner_key,
                reason,
                sandbox_id=sandbox_id,
                workspace_may_have_changed=outage.workspace_may_have_changed,
            )

    def available(self, owner_key: str, sandbox_id: str) -> None:
        self.invalidate_checks(owner_key)
        outage = self._outages.pop(owner_key, None)
        if outage is not None and outage.sandbox_id == sandbox_id:
            self._emit(
                EventType.RECOVERED,
                owner_key,
                Reason.CONNECTION_RESTORED,
                sandbox_id=sandbox_id,
                workspace_may_have_changed=outage.workspace_may_have_changed,
            )

    def connected(
        self, owner_key: str, sandbox_id: str, previous_sandbox_id: str | None
    ) -> None:
        # A State commit may outlive a cancelled caller, and another worker may
        # replace the authoritative binding. Announce the identity change only
        # once this manager has also published its verified local handle.
        outage = self._outages.get(owner_key)
        previous_id = previous_sandbox_id or (outage.sandbox_id if outage else None)
        if previous_id is not None and previous_id != sandbox_id:
            self.published(
                owner_key, sandbox_id, previous_id, reason=Reason.BINDING_CHANGED
            )
        else:
            if outage is not None:
                outage.workspace_may_have_changed = True
            self.available(owner_key, sandbox_id)

    def published(
        self,
        owner_key: str,
        sandbox_id: str,
        previous_sandbox_id: str | None,
        *,
        reason: Reason = Reason.EXPLICIT_RECREATE,
    ) -> None:
        self.invalidate_checks(owner_key)
        outage = self._outages.pop(owner_key, None)
        if previous_sandbox_id is not None and previous_sandbox_id != sandbox_id:
            self._emit(
                EventType.REPLACED,
                owner_key,
                reason if outage is None else outage.reason,
                sandbox_id=sandbox_id,
                previous_sandbox_id=previous_sandbox_id,
                workspace_may_have_changed=True,
            )

    def paused(self, owner_key: str, sandbox_id: str) -> None:
        """Observe an intentional pause without reporting a connectivity outage."""
        self.invalidate_checks(owner_key)
        self._outages.pop(owner_key, None)
        self._emit(
            EventType.PAUSED, owner_key, Reason.EXPLICIT_PAUSE, sandbox_id=sandbox_id
        )

    def resumed(self, owner_key: str, sandbox_id: str) -> None:
        """Observe successful explicit resume after connection initialization."""
        self.invalidate_checks(owner_key)
        self._outages.pop(owner_key, None)
        self._emit(
            EventType.RESUMED,
            owner_key,
            Reason.EXPLICIT_RESUME,
            sandbox_id=sandbox_id,
            workspace_may_have_changed=True,
        )

    def workspace_reset(self, owner_key: str, sandbox_id: str) -> None:
        self._emit(
            EventType.WORKSPACE_RESET,
            owner_key,
            Reason.EXPLICIT_RESET,
            sandbox_id=sandbox_id,
            workspace_may_have_changed=True,
        )

    def destroyed(self, owner_key: str, sandbox_id: str | None) -> None:
        self.invalidate_checks(owner_key)
        self._outages.pop(owner_key, None)
        self._emit(
            EventType.DESTROYED,
            owner_key,
            Reason.EXPLICIT_DESTROY,
            sandbox_id=sandbox_id,
            workspace_may_have_changed=True,
        )

    def warm_capacity(self, *, ready: bool) -> None:
        if self._warm_degraded == (not ready):
            return
        self._warm_degraded = not ready
        self._emit(
            EventType.WARM_CAPACITY_RESTORED
            if ready
            else EventType.WARM_CAPACITY_DEGRADED,
            None,
            Reason.WARM_CAPACITY_AVAILABLE
            if ready
            else Reason.WARM_CAPACITY_UNAVAILABLE,
        )

    async def aclose(self) -> None:
        tasks = [
            task
            for delivery in self._deliveries
            if (task := delivery.finish()) is not None
        ]
        if tasks:
            await asyncio.gather(*tasks)
        self._outages.clear()
        self._checks.clear()
