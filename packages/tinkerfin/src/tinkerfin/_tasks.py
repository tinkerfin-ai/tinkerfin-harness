"""Cancellation-safe joins for runtime-owned asynchronous tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generic, TypeAlias, TypeVar, cast

from anyio import to_thread

from ._failure_evidence import retain_failure, select_failure

__all__ = ["join_task"]

_TaskResult = TypeVar("_TaskResult")
_OPERATION_FAILURES: ContextVar[OwnedOperationFailures | None] = ContextVar(
    "tinkerfin_owned_operation_failures", default=None
)


class OwnedOperationFailures:
    """Retain resource failures that upstream cancellation can hide.

    One run owns these failed resource operations until its source closes. A
    failed owned operation must end the run; collecting only failures keeps this
    bounded by outstanding resource work, rather than streamed item count.
    """

    def __init__(self) -> None:
        self._failures: list[BaseException] = []
        self._active: list[tuple[asyncio.Task[object], int, bool]] = []
        self._parent: OwnedOperationFailures | None = None
        self._bound = False
        self._settled = False
        self._closing_requested = False

    @classmethod
    def for_source(cls) -> tuple[OwnedOperationFailures, bool]:
        """Reuse the enclosing run's owner or create one for a direct Graph call."""

        enclosing = _OPERATION_FAILURES.get()
        return (enclosing, False) if enclosing is not None else (cls(), True)

    @contextmanager
    def capture(self, *, closing: bool = False) -> Generator[None, None, None]:
        """Bind one pull or close operation without keeping a token across yields."""

        if closing:
            self._closing_requested = True
        if not self._bound:
            self._parent = _OPERATION_FAILURES.get()
            if self._parent is self:
                self._parent = None
            self._bound = True
        task = cast(asyncio.Task[object] | None, asyncio.current_task())
        active = None if task is None else (task, task.cancelling(), closing)
        if active is not None:
            self._active.append(active)
        token = _OPERATION_FAILURES.set(self)
        try:
            yield
        finally:
            _OPERATION_FAILURES.reset(token)
            if active is not None:
                self._active.remove(active)

    def _cancellation_requested(self) -> bool:
        return not self._settled and (
            self._closing_requested
            or any(
                closing or task.cancelling() > baseline
                for task, baseline, closing in self._active
            )
            or (self._parent is not None and self._parent._cancellation_requested())
        )

    def _propagate(
        self, error: BaseException | None, failures: tuple[BaseException, ...] = ()
    ) -> None:
        # A caller can handle an independent child Graph's failure normally.
        # Retain it in an ancestor only while that ancestor is being cancelled,
        # when LangGraph may discard the child exception before delivery.
        if (
            error is not None
            and not isinstance(error, StopAsyncIteration | GeneratorExit)
            and self._parent is not None
            and self._parent._cancellation_requested()
        ):
            # A cancellation wrapper's string does not include its exception notes.
            # Preserve original faults as well so ancestor settlement can report
            # the failed operation even if upstream replaces that wrapper.
            for failure in failures:
                self._parent.record(failure)
            self._parent.record(error)

    def record(self, failure: BaseException) -> None:
        """Retain each failure once until the owning source has closed."""

        if all(item is not failure for item in self._failures):
            self._failures.append(failure)

    def transfer(self, failure: Exception) -> None:
        """Hand a known failure to an error stream without redelivering it on close."""

        self._failures = [item for item in self._failures if item is not failure]

    def take(self) -> tuple[BaseException, ...]:
        """Transfer failures to final settlement after upstream tasks have joined."""

        failures = tuple(self._failures)
        self._failures.clear()
        return failures

    def settle(self, primary: BaseException | None) -> None:
        """Deliver retained failures when a direct source has finished closing."""

        failures = self.take()
        self._settled = True
        if not failures:
            self._propagate(primary)
            return
        candidates = (
            [] if primary is None or isinstance(primary, GeneratorExit) else [primary]
        )
        candidates.extend(item for item in failures if item is not primary)
        if not candidates:
            return
        chosen = next(
            (
                item
                for item in candidates
                if not isinstance(item, Exception | asyncio.CancelledError)
            ),
            next(
                (
                    item
                    for item in candidates
                    if isinstance(item, asyncio.CancelledError)
                ),
                candidates[0],
            ),
        )
        for item in candidates:
            if item is not chosen:
                retain_failure(
                    chosen, item, label="Owned resource operation also failed"
                )
        self._propagate(chosen, failures)
        if chosen is not primary:
            raise chosen


@dataclass(frozen=True, slots=True)
class _TaskValue(Generic[_TaskResult]):
    value: _TaskResult


@dataclass(frozen=True, slots=True)
class _TaskFailure:
    error: BaseException


_OperationOutcome: TypeAlias = _TaskValue[_TaskResult] | _TaskFailure


async def _capture_operation(
    operation: Callable[[], Awaitable[_TaskResult]],
) -> _OperationOutcome[_TaskResult]:
    """Keep process control inside the owned task until a caller receives it."""

    try:
        return _TaskValue(await operation())
    except BaseException as error:  # noqa: BLE001 - transport the original task outcome
        return _TaskFailure(error)


def _operation_result(outcome: _OperationOutcome[_TaskResult]) -> _TaskResult:
    if isinstance(outcome, _TaskFailure):
        raise outcome.error
    return outcome.value


def _cancel_operation_once(task: asyncio.Task[_OperationOutcome[_TaskResult]]) -> None:
    if not task.done() and not task.cancelling():
        task.cancel()


async def _join_operation(
    task: asyncio.Task[_OperationOutcome[_TaskResult]], *, cancel_on_interrupt: bool
) -> _TaskResult:
    """Wait for accepted work without cancelling the caller or its later work.

    Args:
        task: One operation owned by the stream, never a borrowed consumer task.
        cancel_on_interrupt: Cancel private work once when its waiting caller is
            cancelled. Retained cleanup tasks use False and always finish.

    Returns:
        The operation result after its own cleanup has settled.

    Raises:
        BaseException: Original operation failure or caller cancellation, with
            concurrent failures preserved and process control taking priority.
    """

    cancellation: asyncio.CancelledError | None = None
    cancel_handle: asyncio.Handle | None = None
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
                if cancel_on_interrupt and cancel_handle is None and not task.done():
                    # Enter capture before delivering cancellation, including when
                    # the waiter was cancelled before the child task's first step.
                    cancel_handle = asyncio.get_running_loop().call_soon(
                        _cancel_operation_once, task
                    )
        outcome = task.result()
    finally:
        if cancel_handle is not None:
            cancel_handle.cancel()
    if cancellation is not None:
        if isinstance(outcome, _TaskFailure):
            raise select_failure(cancellation, outcome.error)
        raise cancellation
    return _operation_result(outcome)


async def stop_operation(task: asyncio.Task[_OperationOutcome[_TaskResult]]) -> None:
    """Cancel accepted work once while retaining independent close-caller failure."""

    cancel_handle = asyncio.get_running_loop().call_soon(_cancel_operation_once, task)
    try:
        await _join_operation(task, cancel_on_interrupt=False)
    except asyncio.CancelledError as error:
        outcome = task.result()
        if not isinstance(outcome, _TaskFailure) or outcome.error is not error:
            raise
        # The active pull's waiter receives this exact captured cancellation,
        # including its causes. A closer must not turn it into a second failed
        # cleanup; only cancellation of this independent closer propagates here.
    finally:
        cancel_handle.cancel()


async def run_sync_owned(operation: Callable[[], _TaskResult]) -> _TaskResult:
    """Run bounded synchronous preparation and join its worker before cancellation.

    A Python thread cannot be cancelled. The owning run therefore waits for the
    operation to finish before releasing resources it might still access. AnyIO's
    shared thread limiter bounds capacity. Process-control failures are returned to
    the owning caller before being raised, so they cannot escape from a child task.
    There is no independent worker deadline; the operation must bound its own I/O.
    """

    return await run_async_owned(
        lambda: to_thread.run_sync(operation), task_name="tinkerfin-sync-preparation"
    )


async def run_async_owned(
    operation: Callable[[], Awaitable[_TaskResult]], *, task_name: str
) -> _TaskResult:
    """Join an operation through cancellation while preserving its final failure.

    Process-control errors are delivered as task results and raised by the owner,
    so they cannot escape through asyncio's child-task exception handling.

    Args:
        operation: One operation whose resources must settle before caller exit.
        task_name: Diagnostic name of its single owned task.

    Returns:
        The operation result after it has fully settled.

    Raises:
        asyncio.CancelledError: Caller cancellation, with concurrent ordinary
            operation failures retained as notes.
        BaseException: Operation failure. Process-control failures retain priority
            over concurrent caller cancellation.
    """

    task = asyncio.create_task(_capture_operation(operation), name=task_name)
    try:
        outcome = await join_task(task)
    except asyncio.CancelledError as cancellation:
        # The operation has settled, even though cancellation prevents join_task
        # from returning its value. Preserve its outcome before propagating.
        outcome = task.result()
        if isinstance(outcome, _TaskFailure):
            failure = outcome.error
            if scope := _OPERATION_FAILURES.get():
                # LangGraph may replace a cancelled node's error while cancelling
                # its outer stream. Final run settlement still owns this failure.
                scope.record(failure)
            if not isinstance(failure, Exception | asyncio.CancelledError):
                failure.add_note(
                    "Caller cancellation also occurred during the owned operation"
                )
                retain_failure(failure, cancellation, label="Caller cancellation")
                raise failure
            retain_failure(cancellation, failure, label="Owned operation also failed")
        raise
    assert outcome is not None
    if isinstance(outcome, _TaskFailure):
        if scope := _OPERATION_FAILURES.get():
            # The operation can fail just before cancellation reaches an upstream
            # wrapper. Retain failures even when this task was not yet cancelled.
            scope.record(outcome.error)
        raise outcome.error
    return outcome.value


async def join_task(
    task: asyncio.Task[_TaskResult],
    *,
    cancel: bool = False,
    suppress_task_cancellation: bool = False,
) -> _TaskResult | None:
    """Join an owned task without losing cancellation of the joining caller.

    Args:
        task: Runtime-owned task that must settle before this call returns.
        cancel: Whether to request cancellation if no cancellation is already pending.
        suppress_task_cancellation: Whether owned task cancellation is expected.

    Returns:
        The owned task result, or ``None`` for an expected task cancellation.

    Raises:
        asyncio.CancelledError: The caller or owned task is cancelled.
        Exception: The owned task fails without caller cancellation.
        BaseException: The owned task raises a process-control exception, which
            propagates unchanged even if the caller has requested cancellation.
    """

    if cancel and not task.done() and not task.cancelling():
        task.cancel()
    caller_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            # wait() neither cancels the owned task nor propagates its outcome.
            # Every cancellation here therefore belongs to the joining caller;
            # ordinary failure and owned cancellation are read once below. Always
            # await once so a pending caller cancellation is delivered even when
            # the owned task has already completed.
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            if caller_cancellation is None:
                caller_cancellation = error
            if not task.done():
                continue
        break

    task_error: Exception | asyncio.CancelledError | None = None
    result: _TaskResult | None = None
    try:
        result = task.result()
    except asyncio.CancelledError as error:
        if not suppress_task_cancellation:
            task_error = error
    except Exception as error:  # noqa: BLE001 - retain failure behind caller cancellation
        task_error = error

    if caller_cancellation is not None:
        if task_error is not None:
            retain_failure(
                caller_cancellation, task_error, label="owned task also failed"
            )
        raise caller_cancellation.with_traceback(caller_cancellation.__traceback__)
    if task_error is not None:
        raise task_error.with_traceback(task_error.__traceback__)
    return result
