"""Own Trace operations and lifecycle work without losing concurrent failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Generic, TypeAlias, TypeGuard, TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _Value(Generic[_T]):
    value: _T


TaskOutcome: TypeAlias = _Value[_T] | BaseException


def _is_group(error: BaseException) -> TypeGuard[BaseExceptionGroup[BaseException]]:
    return isinstance(error, BaseExceptionGroup)


def _stored_cause(error: BaseException) -> BaseException | None:
    # Framework errors retain diagnostic causes as instance data. Inspect that
    # data without invoking descriptors supplied by a custom backend exception.
    value = vars(error).get("cause")
    return value if isinstance(value, BaseException) else None


def _contains(error: BaseException, target: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is target:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend(
            item
            for item in (current.__cause__, current.__context__)
            if item is not None
        )
        if _is_group(current):
            pending.extend(current.exceptions)
        cause = _stored_cause(current)
        if cause is not None:
            pending.append(cause)
    return False


def cancellation_only(error: BaseException) -> bool:
    """Recognize expected cancellation without hiding retained failures."""

    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if _is_group(current):
            pending.extend(current.exceptions)
        elif not isinstance(current, asyncio.CancelledError):
            return False
        pending.extend(
            item
            for item in (current.__cause__, current.__context__, _stored_cause(current))
            if item is not None
        )
    return True


def _retain(primary: BaseException, secondary: BaseException) -> None:
    # Preserve original objects and explicit causes. A cancelled operation can
    # already refer to its caller's cancellation; remove that back edge before
    # attaching independent failures, so exception rendering cannot form a cycle.
    if _contains(primary, secondary):
        return
    retained: dict[int, BaseException | None] = {id(primary): None}

    def detach(error: BaseException | None) -> BaseException | None:
        if error is None:
            return None
        if id(error) in retained:
            return retained[id(error)]
        result: BaseException | None = error
        if _is_group(error):
            _, result = error.split(lambda item: item is primary)
        retained[id(error)] = result
        if result is None:
            return None
        result.__cause__ = detach(result.__cause__)
        result.__context__ = detach(result.__context__)
        cause = _stored_cause(result)
        if cause is not None:
            vars(result)["cause"] = detach(cause)
        if _is_group(result):
            for item in result.exceptions:
                detach(item)
        return result

    original = detach(primary.__cause__ or primary.__context__)
    primary.__context__ = detach(primary.__context__)
    additional = detach(secondary)
    if additional is not None:
        primary.__cause__ = (
            additional
            if original is None
            else original
            if _contains(original, additional)
            else BaseExceptionGroup("Trace operation failures", [original, additional])
        )


def _priority(error: BaseException) -> int:
    if _is_group(error):
        return max(_priority(item) for item in error.exceptions)
    if isinstance(error, asyncio.CancelledError):
        return 1
    return 0 if isinstance(error, Exception) else 2


def select_failure(primary: BaseException, secondary: BaseException) -> BaseException:
    """Keep process control ahead of cancellation and ordinary failure."""

    if _priority(secondary) > _priority(primary):
        _retain(secondary, primary)
        return secondary
    _retain(primary, secondary)
    return primary


async def capture(operation: Awaitable[_T]) -> TaskOutcome[_T]:
    """Transport process control as a value until its owning caller can raise it."""

    try:
        return _Value(await operation)
    except BaseException as error:  # noqa: BLE001 - preserve the exact task outcome
        return error


def _cancel_task_once(task: asyncio.Task[TaskOutcome[_T]]) -> None:
    # Several owners may stop the same accepted operation. Repeated cancellation
    # must not interrupt the operation's own finally block.
    if not task.done() and not task.cancelling():
        task.cancel()


async def join_owned_task(
    task: asyncio.Task[TaskOutcome[_T]], *, cancel_operation: bool
) -> _T:
    """Join accepted work, preserving control, cancellation, and independent causes.

    Setup is shared and cannot be cancelled by a waiter. A normal operation gets
    at most one cancellation request; repeated caller cancellation still waits for
    its accepted work to finish. The result container prevents a child SystemExit or
    KeyboardInterrupt from escaping through asyncio's task machinery.

    Args:
        task: Accepted work that must settle before this caller exits.
        cancel_operation: Send one cancellation to private work when its caller
            cancels. Shared setup and close tasks use False.

    Returns:
        The completed operation's value.

    Raises:
        BaseException: Work failure, caller cancellation, or process control,
            with concurrent failures retained in the selected exception's causes.
    """

    cancellation: asyncio.CancelledError | None = None
    cancel_handle: asyncio.Handle | None = None
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
                if cancel_operation and cancel_handle is None and not task.done():
                    # The task is already queued. Schedule cancellation after its
                    # first step so capture owns the coroutine even when the caller
                    # was cancelled before the child began executing.
                    cancel_handle = asyncio.get_running_loop().call_soon(
                        _cancel_task_once, task
                    )
        outcome = task.result()
    finally:
        if cancel_handle is not None:
            cancel_handle.cancel()
    failure = outcome if isinstance(outcome, BaseException) else None
    if cancellation is not None:
        if failure is None:
            failure = cancellation
        else:
            failure = select_failure(cancellation, failure)
    if failure is not None:
        raise failure
    assert isinstance(outcome, _Value)
    return outcome.value


async def run_owned_operation(operation: Awaitable[_T], *, task_name: str) -> _T:
    """Keep the complete operation and its cleanup under one owner."""

    task = asyncio.create_task(capture(operation), name=task_name)
    return await join_owned_task(task, cancel_operation=True)
