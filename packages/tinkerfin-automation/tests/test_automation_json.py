"""Reject malformed boundary inputs without changing persisted Automation facts.

Parameterized invalid inputs deliberately remain untyped until the public API
validates them; the production signatures retain their precise accepted types.
"""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from pydantic import JsonValue
from test_store_contract import _task, _taskless_execution

from tinkerfin_automation import (
    AttentionResolution,
    ExecutionStatus,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore, ScheduledExecution


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"owner", " owner", "owner\x00id", "owner\ud800"])
async def test_store_implementations_reject_invalid_direct_scope(
    store_with_clock: tuple[AutomationStore, ManualClock], value: Any
) -> None:
    store, _ = store_with_clock
    with pytest.raises((TypeError, ValueError)):
        await store.get_task("app", value, "task")
    with pytest.raises((TypeError, ValueError)):
        await store.list_tasks(
            "app",
            value,
            limit=10,
            cursor=None,
        )
    with pytest.raises((TypeError, ValueError)):
        await store.get_execution(
            "app",
            value,
            "execution",
        )
    with pytest.raises((TypeError, ValueError)):
        await store.list_executions(
            "app",
            value,
            task_id=None,
            limit=10,
            cursor=None,
        )
    with pytest.raises((TypeError, ValueError)):
        await store.cancel_execution(
            "app",
            value,
            "execution",
            request_id=None,
            input_digest="cancel",
        )
    with pytest.raises((TypeError, ValueError)):
        await store.resolve_execution(
            "app",
            value,
            "execution",
            resolution=AttentionResolution.FAILED,
            request_id="resolve",
            input_digest="resolve",
            reason="operator-confirmed",
        )
    with pytest.raises((TypeError, ValueError)):
        await store.get_scheduled_task(
            value,
            "task",
        )
    with pytest.raises((TypeError, ValueError)):
        await store.list_scheduled_tasks(
            value,
            limit=10,
            cursor=None,
        )
    with pytest.raises((TypeError, ValueError)):
        await store.get_scheduled_execution(
            value,
            "execution",
        )
    with pytest.raises((TypeError, ValueError)):
        (
            await store.claim_work(
                "app",
                value,
                limit=1,
                lease_duration=timedelta(minutes=1),
                global_concurrency=1,
            )
        ).claims


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", [b"reason", " reason", "reason\x00text", "reason\ud800"]
)
async def test_store_implementations_reject_invalid_resolution_reason(
    store_with_clock: tuple[AutomationStore, ManualClock], reason: Any
) -> None:
    store, _ = store_with_clock
    with pytest.raises((TypeError, ValueError)):
        await store.resolve_execution(
            "app",
            "owner",
            "execution",
            resolution=AttentionResolution.FAILED,
            request_id="resolve",
            input_digest="resolve",
            reason=reason,
        )


async def test_nonfinite_completion_keeps_the_claim_and_execution_unchanged(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    execution = _taskless_execution(execution_id="run")
    await store.enqueue_execution(
        execution, occurrence_key="run", request_id=None, input_digest="run"
    )
    (claim,) = (
        await store.claim_work(
            "app",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )
    ).claims
    with pytest.raises(ValueError):
        await store.finish_execution(
            claim, status=ExecutionStatus.SUCCEEDED, result={"nested": [float("nan")]}
        )
    assert await store.get_execution("app", "owner-1", "run") == execution
    result: JsonValue = {"nul\x00key": [10**100, 1.25, True, None]}
    assert (
        await store.finish_execution(
            claim, status=ExecutionStatus.SUCCEEDED, result=result
        )
    ).result == result
    assert (await store.get_execution("app", "owner-1", "run")).result == result


async def test_schedule_batch_deduplicates_occurrences_before_queue_admission(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    task = _task()
    await store.create_task(task, request_id=None, input_digest="task")
    item = ScheduledExecution(
        execution=_taskless_execution(execution_id="one"), occurrence_key="occurrence"
    )
    item = replace(item, execution=replace(item.execution, task_id=task.task_id))
    second = replace(item, execution=replace(item.execution, execution_id="second"))
    assert task.next_run_at is not None
    materialized = await store.materialize_task(
        replace(task, next_run_at=None),
        expected_next_run_at=task.next_run_at,
        executions=(item, second),
    )
    assert materialized.executions == (item.execution,)
    assert (
        await store.list_executions(
            "app", "owner-1", task_id=task.task_id, limit=100, cursor=None
        )
    ).items == (item.execution,)
