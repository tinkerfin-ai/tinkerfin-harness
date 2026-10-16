"""Ownership-scoped filters for task discovery and execution history."""

from dataclasses import dataclass
from datetime import datetime

from .models import ExecutionStatus, TaskStatus


def _validate_name(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str) or len(value) > 255 or "\x00" in value
    ):
        raise ValueError("name_contains must be at most 255 characters without NUL")


@dataclass(frozen=True, slots=True)
class TaskFilter:
    """Match literal case-insensitive name text and any selected task status."""

    name_contains: str | None = None
    statuses: tuple[TaskStatus, ...] = ()

    def __post_init__(self) -> None:
        """Validate the bounded text and immutable status selection."""
        _validate_name(self.name_contains)
        if not isinstance(self.statuses, tuple) or any(
            not isinstance(value, TaskStatus) for value in self.statuses
        ):
            raise ValueError("statuses must be a tuple of TaskStatus values")


@dataclass(frozen=True, slots=True)
class ExecutionFilter:
    """Filter saved execution names and an inclusive/exclusive UTC queue interval.

    Empty statuses select every status. Time bounds must be timezone-aware.
    Matching uses the name captured when work was enqueued, including deleted tasks.
    """

    name_contains: str | None = None
    statuses: tuple[ExecutionStatus, ...] = ()
    queued_from: datetime | None = None
    queued_until: datetime | None = None

    def __post_init__(self) -> None:
        """Validate text, status values and the ordered time interval."""
        _validate_name(self.name_contains)
        if not isinstance(self.statuses, tuple) or any(
            not isinstance(value, ExecutionStatus) for value in self.statuses
        ):
            raise ValueError("statuses must be a tuple of ExecutionStatus values")
        for value in (self.queued_from, self.queued_until):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("queue boundaries must be timezone-aware")
        if (
            self.queued_from is not None
            and self.queued_until is not None
            and self.queued_from >= self.queued_until
        ):
            raise ValueError("queued_from must precede queued_until")


__all__ = ["ExecutionFilter", "TaskFilter"]
