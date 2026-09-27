"""Keep rejected commands atomic and claims inside their full ownership scope."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_store_contract import NOW, _execution, _task, _taskless_execution

from tinkerfin_automation import (
    AttentionResolution,
    AutomationStoreError,
    ClaimLostError,
    ExecutionNotFoundError,
    ExecutionStatus,
    MemoryAutomationStore,
    MemoryStoreLimits,
    TaskNotFoundError,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore
from tinkerfin_contracts import RunIdentity


@pytest.mark.parametrize("command", ["create", "update", "delete", "cancel", "resolve"])
async def test_memory_operation_capacity_rejection_leaves_all_facts_unchanged(
    command: str,
) -> None:
    store = MemoryAutomationStore(
        clock=ManualClock(NOW), limits=MemoryStoreLimits(max_operations=1)
    )
    try:
        task = _task()
        await store.create_task(task, request_id="fill", input_digest="fill")
        execution = _execution(task, execution_id="run")
        await store.enqueue_execution(
            execution, occurrence_key="run", request_id=None, input_digest="run"
        )
        if command == "resolve":
            (claim,) = (
                await store.claim_work(
                    "app",
                    "worker",
                    limit=1,
                    lease_duration=timedelta(minutes=1),
                    global_concurrency=1,
                )
            ).claims
            execution = await store.finish_execution(
                claim, status=ExecutionStatus.NEEDS_ATTENTION
            )
        for _ in range(2):
            with pytest.raises(AutomationStoreError):
                if command == "create":
                    await store.create_task(
                        _task(task_id="second"),
                        request_id="blocked",
                        input_digest="blocked",
                    )
                elif command == "update":
                    await store.update_task(
                        replace(task, name="changed", revision=2),
                        expected_revision=1,
                        request_id="blocked",
                        input_digest="blocked",
                    )
                elif command == "delete":
                    await store.delete_task(
                        "app",
                        "owner-1",
                        task.task_id,
                        expected_revision=1,
                        request_id="blocked",
                        input_digest="blocked",
                    )
                elif command == "cancel":
                    await store.cancel_execution(
                        "app",
                        "owner-1",
                        execution.execution_id,
                        request_id="blocked",
                        input_digest="blocked",
                    )
                else:
                    await store.resolve_execution(
                        "app",
                        "owner-1",
                        execution.execution_id,
                        resolution=AttentionResolution.FAILED,
                        reason="Confirmed stopped",
                        request_id="blocked",
                        input_digest="blocked",
                    )
            assert await store.get_task("app", "owner-1", task.task_id) == task
            assert (
                await store.get_execution("app", "owner-1", execution.execution_id)
                == execution
            )
            with pytest.raises(TaskNotFoundError):
                await store.get_task("app", "owner-1", "second")
    finally:
        await store.close()


async def test_memory_work_capacity_rejection_does_not_leave_execution_or_occurrence() -> (
    None
):
    store = MemoryAutomationStore(
        clock=ManualClock(NOW), limits=MemoryStoreLimits(max_work_items=1)
    )
    try:
        first = _taskless_execution(execution_id="first", owner_id="one")
        second = _taskless_execution(execution_id="second", owner_id="two")
        await store.enqueue_execution(
            first, occurrence_key="first", request_id=None, input_digest="first"
        )
        for _ in range(2):
            with pytest.raises(AutomationStoreError):
                await store.enqueue_execution(
                    second,
                    occurrence_key="second",
                    request_id=None,
                    input_digest="second",
                )
            with pytest.raises(ExecutionNotFoundError):
                await store.get_execution("app", "two", "second")
        (claim,) = (
            await store.claim_work(
                "app",
                "worker",
                limit=2,
                lease_duration=timedelta(minutes=1),
                global_concurrency=2,
            )
        ).claims
        assert claim.execution_id == "first"
    finally:
        await store.close()


async def test_memory_failed_interrupt_retains_running_execution_and_valid_claim() -> (
    None
):
    store = MemoryAutomationStore(
        clock=ManualClock(NOW), limits=MemoryStoreLimits(max_work_items=1)
    )
    try:
        execution = _taskless_execution(execution_id="run")
        await store.enqueue_execution(
            execution, occurrence_key="run", request_id=None, input_digest="run"
        )
        (claim,) = (
            await store.claim_work(
                "app",
                "worker",
                limit=1,
                lease_duration=timedelta(hours=1),
                global_concurrency=1,
            )
        ).claims
        started = await store.authorize_start(
            claim, execution_timeout=timedelta(minutes=1)
        )
        with pytest.raises(AutomationStoreError):
            await store.mark_interrupted(claim, interrupt_ids=("approval",))
        assert await store.get_execution("app", "owner-1", "run") == started.execution
        assert (
            await store.renew_claim(claim, lease_duration=timedelta(hours=1))
        ).claim.fence == claim.fence
        assert (
            await store.finish_execution(claim, status=ExecutionStatus.FAILED)
        ).status is ExecutionStatus.FAILED
    finally:
        await store.close()


async def test_claim_recovery_only_changes_the_selected_namespace(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    execution = replace(
        _taskless_execution(execution_id="other-run"),
        namespace="other",
        identity=RunIdentity(namespace="other", thread_id="thread", run_id="run"),
    )
    await store.enqueue_execution(
        execution, occurrence_key="occurrence", request_id=None, input_digest="run"
    )
    (claim,) = (
        await store.claim_work(
            "other",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )
    ).claims
    started = await store.authorize_start(claim, execution_timeout=timedelta(hours=1))
    await clock.advance(timedelta(minutes=2))
    assert not (
        await store.claim_work(
            "app",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )
    ).claims
    assert (
        await store.get_execution("other", "owner-1", execution.execution_id)
        == started.execution
    )
    assert not (
        await store.claim_work(
            "other",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )
    ).claims
    assert (
        await store.get_execution("other", "owner-1", execution.execution_id)
    ).status is ExecutionStatus.NEEDS_ATTENTION


async def test_claim_cannot_authorize_an_execution_different_from_its_persisted_identity(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    execution = _taskless_execution(execution_id="actual")
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
    with pytest.raises(ClaimLostError):
        await store.authorize_start(
            replace(claim, execution_id="different"),
            execution_timeout=timedelta(minutes=1),
        )
    assert (
        await store.get_execution("app", "owner-1", execution.execution_id) == execution
    )
    assert (
        await store.authorize_start(claim, execution_timeout=timedelta(minutes=1))
    ).execution.identity == execution.identity
