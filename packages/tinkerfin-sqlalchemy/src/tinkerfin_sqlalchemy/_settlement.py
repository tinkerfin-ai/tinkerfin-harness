"""Wait for connection cleanup without losing cancellation or independent failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Generic, TypeGuard, TypeVar

from .errors import SqlTransactionError

ResultT = TypeVar("ResultT")


def _is_group(error: BaseException) -> TypeGuard[BaseExceptionGroup[BaseException]]:
    return isinstance(error, BaseExceptionGroup)


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
        if isinstance(current, SqlTransactionError) and current.cause is not None:
            pending.append(current.cause)
    return False


def retain_failure(primary: BaseException, secondary: BaseException) -> None:
    """Keep independent causes without creating a back-reference to the primary."""

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
        if isinstance(result, SqlTransactionError):
            result.cause = detach(result.cause)
        if _is_group(result):
            for item in result.exceptions:
                detach(item)
        return result

    original = detach(primary.__cause__ or primary.__context__)
    primary.__context__ = detach(primary.__context__)
    additional = detach(secondary)
    if additional is None:
        return
    primary.__cause__ = (
        additional
        if original is None
        else original
        if _contains(original, additional)
        else BaseExceptionGroup("SQL transaction failures", [original, additional])
    )
    note = f"SQL cleanup also failed: {type(secondary).__name__}"
    if note not in getattr(primary, "__notes__", ()):
        primary.add_note(note)


def select_failure(primary: BaseException, secondary: BaseException) -> BaseException:
    """Keep process control ahead of cancellation and ordinary failures."""

    def priority(error: BaseException) -> int:
        if isinstance(error, asyncio.CancelledError):
            return 1
        return 0 if isinstance(error, Exception) else 2

    if priority(secondary) > priority(primary):
        retain_failure(secondary, primary)
        return secondary
    retain_failure(primary, secondary)
    return primary


def restore_caller_cancellation(
    error: BaseException, previous_cancellations: int
) -> BaseException:
    """Preserve cancellation replaced by a driver's failing invalidation listener."""

    caller = asyncio.current_task()
    if caller is None or caller.cancelling() <= previous_cancellations:
        return error
    # SQLAlchemy Connection._handle_dbapi_exception may replace CancelledError
    # while disposing the interrupted connection. Only a newly requested caller
    # cancellation with matching exception evidence can change the scope outcome.
    pending = [error]
    seen: set[int] = set()
    cancellations: list[asyncio.CancelledError] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, asyncio.CancelledError):
            cancellations.append(current)
        pending.extend(
            item
            for item in (current.__cause__, current.__context__)
            if item is not None
        )
        if _is_group(current):
            pending.extend(current.exceptions)
        if isinstance(current, SqlTransactionError) and current.cause is not None:
            pending.append(current.cause)
    selected = error
    for cancellation in cancellations:
        selected = select_failure(selected, cancellation)
    return selected


@dataclass(frozen=True, slots=True)
class _Result(Generic[ResultT]):
    value: ResultT | None = None
    error: BaseException | None = None


async def settle(operation: Awaitable[ResultT]) -> ResultT | None:
    """Join one owned I/O operation before delivering repeated caller cancellation."""

    async def capture() -> _Result[ResultT]:
        try:
            return _Result(value=await operation)
        except BaseException as error:  # noqa: BLE001 - transport process control to the owning caller
            return _Result(error=error)

    task = asyncio.create_task(capture(), name="tinkerfin-sql-connection-settlement")
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    result = task.result()
    error = result.error
    if cancellation is not None:
        error = cancellation if error is None else select_failure(cancellation, error)
    if error is not None:
        raise error
    return result.value
