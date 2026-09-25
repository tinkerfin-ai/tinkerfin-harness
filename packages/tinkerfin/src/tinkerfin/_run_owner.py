"""Keep coordination and native execution alive under explicit Task ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from contextlib import AbstractAsyncContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from tinkerfin_contracts import RunIdentity

from ._failure_evidence import (
    has_non_cancellation_failure,
    retain_failure,
    select_failure,
)
from ._tasks import join_task
from .coordination import RunCoordinator
from .errors import RunCoordinationOwnershipLostError, TinkerFinLifecycleError

ResultT = TypeVar("ResultT")
PreparedT = TypeVar("PreparedT")
_current_owner: ContextVar[RunOwner | None] = ContextVar(
    "tinkerfin_native_run_owner", default=None
)


@dataclass(frozen=True, slots=True)
class _Outcome(Generic[ResultT]):
    """Carry a value or failure back from a Task without leaking process control."""

    value: ResultT | None = None
    error: BaseException | None = None

    def unwrap(self) -> ResultT:
        if self.error is not None:
            raise self.error
        return cast(ResultT, self.value)

    def retain_failure(self, primary: BaseException) -> None:
        failure = self.error
        if failure is None or failure is primary:
            return
        if not isinstance(failure, Exception | asyncio.CancelledError) or (
            isinstance(primary, Exception)
            and isinstance(failure, asyncio.CancelledError)
        ):
            retain_failure(failure, primary, label="Run operation also failed")
            raise failure
        retain_failure(primary, failure, label="Owned Run operation also failed")


def in_native_owner() -> bool:
    """Identify direct execution in the protected native Task, excluding its children."""

    owner = _current_owner.get()
    return owner is not None and owner.is_current()


@contextmanager
def native_iterator_cleanup() -> Generator[None, None, None]:
    """Finish an iterator's cleanup in its own Task before honoring a new stop.

    A naturally exhausted iterator can close before whole-Run settlement starts.
    The local guard also permits Plan subflows to close and then continue normally.
    A pending stop is delivered after cleanup, before subsequent Graph work.

    Yields:
        A scope that defers controller cancellation while the iterator closes.

    Raises:
        TinkerFinLifecycleError: The caller does not own native execution.
        BaseException: Iterator cleanup fails or the Run is stopped during cleanup.
    """

    owner = _current_owner.get()
    if owner is None or not owner.is_current():
        raise TinkerFinLifecycleError("iterator cleanup requires its native Run owner")
    owner._closing_iterators += 1
    try:
        yield
    except BaseException as error:  # noqa: BLE001 - preserve cleanup and controller failures together
        raise owner.failure_for(error)
    finally:
        owner._closing_iterators -= 1
    if owner._stopping and not owner._settling and not owner._closing_iterators:
        raise owner.failure_for(
            asyncio.CancelledError("Run closed during source cleanup")
        )


class RunOwner:
    """Own a bounded native command loop and its coordinator's complete scope.

    The coordinator's supervisor enters and exits its context in the same Task.
    Native preparation, pulls, and cleanup run in one child Task inheriting that
    context. Lease loss, caller close, and observer failure merge into one work
    cancellation. Subsequent requests wait, so resource cleanup cannot receive a
    second cancellation from a different controller.
    """

    def __init__(
        self, identity: RunIdentity, coordinator: RunCoordinator | None
    ) -> None:
        self._identity = identity
        self._coordinator = coordinator
        self._commands: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue(
            maxsize=1
        )
        self._native: asyncio.Task[None] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._release = asyncio.Event()
        self._released: asyncio.Future[_Outcome[None]] = (
            asyncio.get_running_loop().create_future()
        )
        self._scope_error: BaseException | None = None
        self._admission_error: BaseException | None = None
        self._failure: BaseException | None = None
        self._failure_delivered = False
        self._caller_cancellation: asyncio.CancelledError | None = None
        self._cleanup_error: BaseException | None = None
        self._close_native: Callable[[BaseException | None], Awaitable[None]] | None = (
            None
        )
        self._pending = False
        self._fail_pending: Callable[[BaseException], None] | None = None
        self._stopping = False
        self._settling = False
        self._closing_iterators = 0
        self._watchers: list[asyncio.Task[None]] = []

    def is_current(self) -> bool:
        """Check whether the caller owns native preparation, pulls, and cleanup."""

        return asyncio.current_task() is self._native

    @property
    def error(self) -> Exception | None:
        """Retain a known controller failure even when caller cancellation wins."""

        return self._failure if isinstance(self._failure, Exception) else None

    def require_admission(self) -> None:
        """Reject preparation if the coordinator did not admit this Run."""

        if self._admission_error is not None:
            raise self._admission_error

    def failure_for(self, error: BaseException) -> BaseException:
        """Distinguish a controller stopping work from caller cancellation."""

        primary: BaseException | None = self._failure
        if self._caller_cancellation is not None:
            primary = (
                self._caller_cancellation
                if primary is None
                else select_failure(self._caller_cancellation, primary)
            )
        if primary is None:
            return error
        if primary is error:
            return error
        if isinstance(error, asyncio.CancelledError):
            # A controller's cancellation stops work, but cleanup may have attached
            # failures to that exact cancellation before it reaches this boundary.
            retain_failure(primary, error, label="Run work was also cancelled")
            return primary
        return select_failure(primary, error)

    def _record_caller_cancellation(self, error: BaseException) -> None:
        task = asyncio.current_task()
        if (
            isinstance(error, asyncio.CancelledError)
            and task is not None
            and task.cancelling()
            and not self._settling
        ):
            self._caller_cancellation = self._caller_cancellation or error

    def bind_close(
        self, close: Callable[[BaseException | None], Awaitable[None]]
    ) -> None:
        """Give the native Task its one stream settlement operation."""

        if self._close_native is not None:
            raise TinkerFinLifecycleError("a Run already owns a native stream")
        self._close_native = close

    async def prepare(
        self, prepare: Callable[[RunOwner], Awaitable[PreparedT]]
    ) -> PreparedT:
        ready: asyncio.Future[_Outcome[PreparedT]] = (
            asyncio.get_running_loop().create_future()
        )

        async def serve() -> None:
            token = _current_owner.set(self)
            try:
                try:
                    value = await prepare(self)
                except BaseException as error:  # noqa: BLE001 - return preparation and control failures to the caller
                    ready.set_result(_Outcome(error=self.failure_for(error)))
                    self._stopping = True
                else:
                    ready.set_result(_Outcome(value=value))
                while not self._stopping:
                    execute = await self._commands.get()
                    await execute()
            except BaseException as error:  # noqa: BLE001 - retain stop cause before native settlement
                self._failure = self.failure_for(error)
            finally:
                self._settling = True
                try:
                    close = self._close_native
                    if close is not None:
                        await close(self._failure)
                except BaseException as error:  # noqa: BLE001 - aclose delivers the retained cleanup failure
                    self._cleanup_error = error
                finally:
                    self._release.set()
                    if self._fail_pending is not None:
                        self._fail_pending(
                            self._cleanup_error
                            or self._failure
                            or asyncio.CancelledError("Run closed")
                        )
                    _current_owner.reset(token)

        async def supervise() -> None:
            scope: AbstractAsyncContextManager[None] | None = None
            scope_primary: BaseException | None = None
            try:
                if self._coordinator is not None:
                    from ._runtime_streams import _coordination_error

                    try:
                        candidate = self._coordinator(self._identity)
                        await candidate.__aenter__()
                    except Exception as error:  # noqa: BLE001 - translate the replaceable coordinator boundary
                        self._admission_error = _coordination_error("enter", error)
                    except BaseException as error:  # noqa: BLE001 - admission preserves cancellation and process control
                        self._admission_error = error
                    else:
                        scope = candidate
                if self._stopping and scope is None:
                    if not ready.done():
                        ready.set_result(
                            _Outcome(
                                error=asyncio.CancelledError(
                                    "Run closed before admission"
                                )
                            )
                        )
                    return
                self._native = asyncio.create_task(
                    serve(), name="tinkerfin-native-run-owner"
                )
                try:
                    await self._release.wait()
                except asyncio.CancelledError as error:
                    scope_primary = error
                    self.stop(
                        RunCoordinationOwnershipLostError(
                            "Run coordination stopped before execution settled",
                            cause=error,
                        )
                    )
                    # The native Task closes its source and workspace before it asks
                    # for coordinator exit. Do not cancel that cleanup a second time.
                    while not self._release.is_set():
                        try:
                            await self._release.wait()
                        except asyncio.CancelledError:
                            continue
                release_error: BaseException | None = None
                if scope is not None:
                    primary = scope_primary or self._scope_error
                    try:
                        await scope.__aexit__(
                            None if primary is None else type(primary),
                            primary,
                            None if primary is None else primary.__traceback__,
                        )
                    except Exception as error:  # noqa: BLE001 - translate the replaceable coordinator boundary
                        from ._runtime_streams import _coordination_error

                        release_error = _coordination_error("exit", error)
                    except BaseException as error:  # noqa: BLE001 - return control failures to native settlement
                        release_error = error
                # Losing admission before native resources close is a Run failure,
                # even if __aexit__ handles the cancellation normally. Deliver it
                # before terminal observation, without interrupting cleanup again.
                if self._failure is not None:
                    release_error = (
                        self._failure
                        if release_error is None
                        else select_failure(self._failure, release_error)
                    )
                self._released.set_result(_Outcome(error=release_error))
                await join_task(self._native)
            except BaseException as error:  # noqa: BLE001 - settle waiters and expose failure through aclose
                self._cleanup_error = error
                if not ready.done():
                    ready.set_result(_Outcome(error=error))
                if not self._released.done():
                    self._released.set_result(_Outcome(error=error))
            finally:
                if not self._released.done():
                    self._released.set_result(_Outcome())

        self._supervisor = asyncio.create_task(
            supervise(), name="tinkerfin-run-coordination-owner"
        )
        try:
            return (await asyncio.shield(ready)).unwrap()
        except BaseException as primary:
            self._record_caller_cancellation(primary)
            await self._close_after_failure(primary)
            if ready.done():
                ready.result().retain_failure(primary)
            raise

    async def call(self, operation: Callable[[], Awaitable[ResultT]]) -> ResultT:
        """Execute one operation with backpressure in the same native Task."""

        if self.is_current():
            return await operation()
        native = self._native
        if native is None or native.done() or self._stopping:
            if self._failure is not None:
                raise self._failure
            raise TinkerFinLifecycleError("the native Run owner is closed")
        if self._pending:
            raise TinkerFinLifecycleError("a native Run operation is already pending")
        self._pending = True
        result: asyncio.Future[_Outcome[ResultT]] = (
            asyncio.get_running_loop().create_future()
        )

        def fail_pending(error: BaseException) -> None:
            if not result.done():
                result.set_result(_Outcome(error=error))

        self._fail_pending = fail_pending

        async def execute() -> None:
            try:
                result.set_result(_Outcome(value=await operation()))
            except BaseException as error:  # noqa: BLE001 - return operation failures to the caller in its Task
                result.set_result(_Outcome(error=self.failure_for(error)))

        self._commands.put_nowait(execute)
        try:
            try:
                return (await asyncio.shield(result)).unwrap()
            except asyncio.CancelledError as primary:
                self._record_caller_cancellation(primary)
                await self._close_after_failure(primary)
                if result.done():
                    result.result().retain_failure(primary)
                raise
        except BaseException:
            if self._failure is not None:
                self._failure_delivered = True
            raise
        finally:
            self._pending = False
            self._fail_pending = None

    def stop(self, error: BaseException | None = None) -> None:
        """Request one work cancellation; further stop requests only retain evidence."""

        if error is not None:
            self._failure = (
                error if self._failure is None else select_failure(self._failure, error)
            )
        self._stopping = True
        native = self._native
        if native is not None:
            if (
                not self._settling
                and not self._closing_iterators
                and not native.done()
                and not native.cancelling()
                and not self.is_current()
            ):
                native.cancel("Run execution stopped")
        elif self._supervisor is not None and not self._supervisor.cancelling():
            self._supervisor.cancel("Run closed before admission")

    def watch_failure(self, wait_failure: Callable[[], Awaitable[None]]) -> None:
        """Observe a lifecycle failure without moving Graph pulls into new Tasks."""

        async def watch() -> None:
            try:
                await wait_failure()
            except asyncio.CancelledError as error:
                if not self._settling:
                    self.stop(error)
            except BaseException as error:  # noqa: BLE001 - the native owner reports observer failures
                self.stop(error)

        self._watchers.append(
            asyncio.create_task(watch(), name="tinkerfin-run-observer-watch")
        )

    async def begin_settlement(self) -> None:
        """Stop failure watchers before closing source and workspace resources."""

        self._settling = True
        self._stopping = True
        for watcher in self._watchers:
            await join_task(watcher, cancel=True, suppress_task_cancellation=True)
        self._watchers.clear()

    async def release_coordination(self, error: BaseException | None) -> None:
        """Wait for same-Task coordinator exit after native resources have stopped."""

        self._scope_error = error
        self._release.set()
        (await asyncio.shield(self._released)).unwrap()

    async def aclose(self) -> None:
        """Wait for complete native and coordinator cleanup without cancelling it again."""

        self.stop()
        if self.is_current():
            return
        if self._supervisor is not None:
            try:
                await join_task(self._supervisor)
            except asyncio.CancelledError as primary:
                _Outcome(error=self._cleanup_error).retain_failure(primary)
                raise
        if self._cleanup_error is not None:
            raise self._cleanup_error
        if (
            not self._pending
            and not self._failure_delivered
            and self._failure is not None
            and has_non_cancellation_failure(self._failure)
        ):
            # Closing an idle source can attach a resource failure to the owner's
            # stop cancellation after the last pull was delivered. No pending call
            # remains to carry it, so close must expose that retained evidence.
            self._failure_delivered = True
            raise self._failure

    async def _close_after_failure(self, primary: BaseException) -> None:
        try:
            await self.aclose()
        except BaseException as cleanup:  # noqa: BLE001 - retain both the caller and cleanup failures
            _Outcome(error=cleanup).retain_failure(primary)
