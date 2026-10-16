"""Own Automation work and preserve concurrent failures until callers can observe them."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Generic, TypeAlias, TypeGuard, TypeVar

_T = TypeVar("_T")
_T_co = TypeVar("_T_co", covariant=True)


@dataclass(frozen=True, slots=True)
class _Value(Generic[_T_co]):
    value: _T_co


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
            else BaseExceptionGroup(
                "Automation operation failures", [original, additional]
            )
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


@dataclass(slots=True)
class Cancellation:
    """Carry a caller's stop request to the write transaction's commit decision."""

    error: asyncio.CancelledError | None = None


async def join_owned_task(
    task: asyncio.Task[TaskOutcome[_T]], *, cancellation: Cancellation | None = None
) -> _T:
    """Wait for owned work and preserve its complete failure outcome.

    A cancelled waiter cannot abandon shared setup, shutdown, or child cleanup.
    Database writes may record the stop request before deciding to commit. Store
    and client timeouts bound database I/O; engine shutdown waits for target cleanup.

    Args:
        task: Captured work that must settle before its caller exits.
        cancellation: Write stop request checked before COMMIT, if applicable.

    Returns:
        The accepted operation's result.

    Raises:
        BaseException: Process control, caller cancellation, or an operation failure,
            retaining concurrent failures and their original causes.
    """

    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
            if cancellation is not None and cancellation.error is None:
                cancellation.error = error
    outcome = task.result()
    failure = outcome if isinstance(outcome, BaseException) else None
    if cancelled is not None:
        failure = cancelled if failure is None else select_failure(cancelled, failure)
    if failure is not None:
        raise failure
    assert isinstance(outcome, _Value)
    return outcome.value


async def capture_call(operation: Callable[[], Awaitable[_T]]) -> TaskOutcome[_T]:
    """Capture extension failures raised either before or during the await."""

    try:
        return _Value(await operation())
    except BaseException as error:  # noqa: BLE001 - retain extension control signals
        return error


def task_result(task: asyncio.Task[TaskOutcome[_T]]) -> _T:
    """Read a completed child and propagate its original failure."""

    outcome = task.result()
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome.value


async def stop_owned_tasks(
    tasks: Iterable[asyncio.Task[TaskOutcome[object]]],
) -> None:
    """Cancel unfinished children once, join every child, and retain cleanup failures."""

    children = tuple(tasks)
    cancelled = {task for task in children if not task.done() and task.cancel()}
    failure: BaseException | None = None
    for task in children:
        try:
            outcome = await task
        except BaseException as error:  # noqa: BLE001 - includes pre-start cancellation
            outcome = error
        if isinstance(outcome, BaseException):
            if isinstance(outcome, asyncio.CancelledError) and task in cancelled:
                additional = outcome.__cause__ or outcome.__context__
                if additional is None:
                    continue
                outcome = additional
            failure = outcome if failure is None else select_failure(failure, outcome)
    if failure is not None:
        raise failure
