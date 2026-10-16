"""Own bounded remote-operation settlement independently of cancelled callers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar

from ..errors import OpenSandboxBusyError

_SETTLEMENT_TIMEOUT_SECONDS = 5.0
_MAX_PENDING_SETTLEMENTS = 32
_MAX_RETAINED_RESOURCES = 65536
_current_operations: ContextVar[RemoteOperations | None] = ContextVar(
    "tinkerfin_sandbox_remote_operations", default=None
)


class RemoteOperations:
    """Retain remote work until termination is confirmed or remains unresolved.

    The handle separately counts active calls. This object retains the obligations
    left by calls that have already returned. Unresolved outcomes remain attached
    to the remote instance; reconnecting or waiting does not erase that evidence.
    All access belongs to the native asynchronous operation's event loop.
    """

    def __init__(self) -> None:
        self._pending: set[asyncio.Task[None]] = set()
        self._unresolved = False
        self._resources: set[str] = set()

    @contextmanager
    def activate(self) -> Generator[None]:
        """Attribute remote work in this call to its owning handle connection."""
        token = _current_operations.set(self)
        try:
            yield
        finally:
            _current_operations.reset(token)

    @property
    def is_idle(self) -> bool:
        """Return whether no pending or unconfirmed remote work remains."""
        return not self._pending and not self._unresolved and not self._resources

    def retain_resource(self, identity: str) -> None:
        """Keep a known remote resource non-idle until its exact termination is confirmed.

        The bounded set matches the native session ledger's per-instance capacity.
        Unlike an unknown command outcome, an identified resource can be settled
        by a later operation that proves termination of that same identity.
        """
        if identity in self._resources:
            return
        if len(self._resources) >= _MAX_RETAINED_RESOURCES:
            raise OpenSandboxBusyError("Sandbox has too many unsettled resources")
        self._resources.add(identity)

    def confirm_resource_stopped(self, identity: str) -> None:
        """Discharge only this identity, preserving all unrelated uncertainty."""
        self._resources.discard(identity)

    def mark_unresolved(self) -> None:
        """Preserve an outcome that cannot establish remote termination."""
        self._unresolved = True

    def start_settlement(self, settle: Callable[[], Awaitable[None]]) -> None:
        """Own one bounded termination check without delaying its original caller.

        Capacity exhaustion preserves uncertainty instead of launching unbounded
        background work. The callable is invoked only after capacity is reserved.
        """
        if len(self._pending) >= _MAX_PENDING_SETTLEMENTS:
            self.mark_unresolved()
            return
        task = asyncio.create_task(
            self._settle(settle), name="tinkerfin-sandbox-remote-settlement"
        )
        self._pending.add(task)
        task.add_done_callback(self._settled)

    async def _settle(self, settle: Callable[[], Awaitable[None]]) -> None:
        try:
            async with asyncio.timeout(_SETTLEMENT_TIMEOUT_SECONDS):
                await settle()
        except asyncio.CancelledError:
            self.mark_unresolved()
            raise
        except Exception:  # noqa: BLE001 - failure retains remote uncertainty
            self.mark_unresolved()

    def _settled(self, task: asyncio.Task[None]) -> None:
        self._pending.discard(task)
        if not task.cancelled():
            task.exception()

    async def wait(self) -> None:
        """Await owned checks without cancelling them when this caller cancels.

        Each check has its own deadline. Completion releases task resources but
        never clears unresolved outcomes or declares the remote instance idle.
        """
        while self._pending:
            pending = tuple(self._pending)
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
            # Gathering tasks that already completed may return without yielding.
            # Consume that snapshot explicitly rather than spinning until their
            # scheduled done callbacks get an opportunity to run.
            for task in pending:
                if task.done():
                    self._settled(task)


def current_remote_operations(default: RemoteOperations) -> RemoteOperations:
    """Use the active handle owner, or the standalone backend's own tracker."""
    current = _current_operations.get()
    return default if current is None else current
