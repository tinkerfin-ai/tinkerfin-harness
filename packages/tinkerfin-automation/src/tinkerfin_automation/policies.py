"""Validated resource and missed-schedule policies for Automation work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class MisfireMode(StrEnum):
    """How missed schedule times become execution records."""

    SKIP = "skip"
    LATEST = "latest"
    CATCH_UP = "catch_up"


@dataclass(frozen=True, slots=True)
class MisfirePolicy:
    """Bound how the scheduler materializes task times missed while unavailable."""

    mode: MisfireMode = MisfireMode.LATEST
    grace: timedelta = timedelta(minutes=1)
    catch_up_window: timedelta = timedelta(hours=24)
    max_catch_up: int = 100

    def __post_init__(self) -> None:
        """Reject negative or unbounded policy values before scheduling."""

        if self.grace < timedelta(0):
            raise ValueError("grace must not be negative")
        if self.catch_up_window <= timedelta(0):
            raise ValueError("catch_up_window must be positive")
        if self.max_catch_up < 1:
            raise ValueError("max_catch_up must be at least 1")


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Bound queueing and execution resources for one task or one-time owner.

    Task executions share these limits by ``task_id``. Executions submitted through
    :meth:`~tinkerfin_automation.AutomationOwner.run` share them with other
    taskless executions owned by the same subject. Every execution also consumes global
    Engine capacity.
    """

    max_concurrent_runs: int = 1
    max_queued_runs: int = 10
    execution_timeout: timedelta = timedelta(minutes=30)
    queue_timeout: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        """Reject limits that cannot provide bounded execution."""

        if self.max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be at least 1")
        if self.max_queued_runs < 0:
            raise ValueError("max_queued_runs must not be negative")
        if self.execution_timeout <= timedelta(0):
            raise ValueError("execution_timeout must be positive")
        if self.queue_timeout <= timedelta(0):
            raise ValueError("queue_timeout must be positive")


__all__ = ["ExecutionLimits", "MisfireMode", "MisfirePolicy"]
