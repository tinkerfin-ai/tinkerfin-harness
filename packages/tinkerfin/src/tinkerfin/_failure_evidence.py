"""Retain concurrent failures without replacing the selected public exception."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import TypeGuard

from .errors import TinkerFinError


def _is_group(error: BaseException) -> TypeGuard[BaseExceptionGroup[BaseException]]:
    return isinstance(error, BaseExceptionGroup)


def select_failure(
    primary: BaseException,
    secondary: BaseException,
    *,
    label: str = "Run also failed",
) -> BaseException:
    """Keep the first failure unless cancellation or process control takes priority."""

    def priority(error: BaseException) -> int:
        if not isinstance(error, Exception | asyncio.CancelledError):
            return 2
        return 1 if isinstance(error, asyncio.CancelledError) else 0

    if priority(secondary) > priority(primary):
        retain_failure(secondary, primary, label=label)
        return secondary
    retain_failure(primary, secondary, label=label)
    return primary


def _exceptions(error: BaseException) -> Iterator[BaseException]:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend(
            item
            for item in (current.__cause__, current.__context__)
            if item is not None
        )
        if isinstance(current, TinkerFinError) and current.cause is not None:
            pending.append(current.cause)
        if _is_group(current):
            pending.extend(current.exceptions)


def has_non_cancellation_failure(error: BaseException) -> bool:
    """Find actual failures or process control inside a cancellation's evidence.

    Exception groups are containers. A domain error remains a failure even when
    its own cause is cancellation; cancellation-only notes do not establish one.

    Args:
        error: The outcome whose causes and groups must be inspected.

    Returns:
        Whether any retained object represents more than cancellation alone.
    """

    return any(
        not isinstance(item, asyncio.CancelledError) and not _is_group(item)
        for item in _exceptions(error)
    )


def retain_failure(
    primary: BaseException, secondary: BaseException, *, label: str
) -> None:
    """Preserve notes and original causes while leaving failure priority unchanged.

    Cleanup failures often refer back to the currently handled primary exception
    through Python's implicit context. Remove only those back-references before
    attaching the secondary cause; the primary remains the outer exception.
    Exception groups retain all independent members. Repeated close does not add
    another copy of evidence already reachable from the primary.

    Args:
        primary: Exception whose type and priority the caller has selected.
        secondary: Additional failure with its original diagnostic evidence.
        label: Context for the human-readable secondary failure note.
    """

    if primary is secondary:
        return
    for note in (
        f"{label}: {type(secondary).__name__}: {secondary}",
        *getattr(secondary, "__notes__", ()),
    ):
        if note not in getattr(primary, "__notes__", ()):
            primary.add_note(note)
    if any(item is secondary for item in _exceptions(primary)):
        return

    retained: dict[int, BaseException | None] = {id(primary): None}

    def detach(error: BaseException | None) -> BaseException | None:
        if error is None:
            return None
        if id(error) in retained:
            return retained[id(error)]
        result: BaseException | None = error
        if _is_group(error) and _group_contains(error, primary):
            _, result = error.split(lambda item: item is primary)
        retained[id(error)] = result
        if result is None:
            return None
        result.__cause__ = detach(result.__cause__)
        result.__context__ = detach(result.__context__)
        if isinstance(result, TinkerFinError):
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
    if original is None:
        primary.__cause__ = additional
    elif any(item is additional for item in _exceptions(original)):
        primary.__cause__ = original
    else:
        primary.__cause__ = BaseExceptionGroup(
            "Additional Run failures", [original, additional]
        )


def _group_contains(
    group: BaseExceptionGroup[BaseException], target: BaseException
) -> bool:
    return any(
        item is target or (_is_group(item) and _group_contains(item, target))
        for item in group.exceptions
    )
