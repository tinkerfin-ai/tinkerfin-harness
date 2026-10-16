"""Wait for shared Store initialization and retain its original failure outcome."""

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
                "Memory Store operation failures", [original, additional]
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


async def join_owned_task(task: asyncio.Task[TaskOutcome[_T]]) -> _T:
    """Wait for accepted Store initialization and preserve its complete failure outcome.

    The current call owns every accepted operation. A cancelled waiter cannot
    abandon database work or its connection cleanup. The shared initialization finishes atomically even if one waiter cancels.
    The client deadline or a 30-second default bounds database I/O.

    Args:
        task: Captured work that must settle before its caller exits.

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
    outcome = task.result()
    failure = outcome if isinstance(outcome, BaseException) else None
    if cancelled is not None:
        failure = cancelled if failure is None else select_failure(cancelled, failure)
    if failure is not None:
        raise failure
    assert isinstance(outcome, _Value)
    return outcome.value
