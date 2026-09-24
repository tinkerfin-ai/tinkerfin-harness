from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta

import pytest

from tinkerfin_automation import (
    AutomationError,
    AutomationErrorCode,
    AutomationWaitTimeout,
    ExecutionFailure,
    ExecutionLimits,
    ExecutionStatus,
    IntervalSchedule,
    MisfirePolicy,
    TaskConflictError,
)


def test_execution_status_terminal_contract() -> None:
    assert ExecutionStatus.SUCCEEDED.is_terminal
    assert ExecutionStatus.FAILED.is_terminal
    assert ExecutionStatus.TIMED_OUT.is_terminal
    assert ExecutionStatus.CANCELLED.is_terminal
    assert not ExecutionStatus.INTERRUPTED.is_terminal
    assert not ExecutionStatus.NEEDS_ATTENTION.is_terminal


def test_limits_reject_unbounded_values() -> None:
    with pytest.raises(ValueError, match="execution_timeout"):
        ExecutionLimits(execution_timeout=timedelta(0))
    with pytest.raises(ValueError, match="max_queued_runs"):
        ExecutionLimits(max_queued_runs=-1)
    with pytest.raises(ValueError, match="max_catch_up"):
        MisfirePolicy(max_catch_up=0)


def test_interval_schedule_normalizes_aware_anchor() -> None:
    schedule = IntervalSchedule(
        every_seconds=60,
        start_at=datetime(2026, 9, 9, tzinfo=UTC),
    )
    assert schedule.start_at == datetime(2026, 9, 9, tzinfo=UTC)


def test_failure_requires_stable_public_information() -> None:
    failure = ExecutionFailure(code="host.denied", message="Access was denied")
    assert failure.code == "host.denied"
    with pytest.raises(ValueError, match="canonical"):
        ExecutionFailure(code=" host.denied", message="Access was denied")


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (TaskConflictError, AutomationErrorCode.TASK_CONFLICT),
        (AutomationWaitTimeout, AutomationErrorCode.WAIT_TIMEOUT),
    ],
)
def test_error_context_is_safe_immutable_and_cause_is_chained(
    error_type: type[AutomationError], code: AutomationErrorCode
) -> None:
    public = {"task_id": "task-1"}
    diagnostic = {"operation": "update"}
    cause = RuntimeError("private detail")
    error = error_type(
        "Operation did not complete",
        context=public,
        diagnostic_context=diagnostic,
        cause=cause,
    )
    public["task_id"] = "changed"
    diagnostic["operation"] = "changed"

    assert isinstance(error, AutomationError)
    assert error.code is code
    assert error.context == {"task_id": "task-1"}
    assert error.diagnostic_context == {"operation": "update"}
    assert error.__cause__ is cause
    assert not isinstance(error.context, MutableMapping)
