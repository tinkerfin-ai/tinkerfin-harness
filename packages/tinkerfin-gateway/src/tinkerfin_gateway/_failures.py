"""Preserve independent failures while prioritizing process control and cancellation."""

from __future__ import annotations

import asyncio
from typing import TypeGuard


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
                "Gateway operation failures", [original, additional]
            )
        )


def _priority(error: BaseException) -> int:
    if _is_group(error):
        return max(_priority(item) for item in error.exceptions)
    if isinstance(error, asyncio.CancelledError):
        return 1
    return 0 if isinstance(error, Exception) else 2


def _select_failure(primary: BaseException, secondary: BaseException) -> BaseException:
    """Keep process control ahead of cancellation and ordinary failure."""

    if _priority(secondary) > _priority(primary):
        _retain(secondary, primary)
        return secondary
    _retain(primary, secondary)
    return primary


__all__ = ["_select_failure"]
