"""Keep caller-owned JSON separate from saved tasks, executions and command results."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_store_contract import _execution, _task, _taskless_execution

from tinkerfin_automation import (
    AttentionResolution,
    AutomationExecution,
    AutomationTask,
    ExecutionStatus,
    TaskNotFoundError,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore, ScheduledExecution


def _alter(snapshot: AutomationTask | AutomationExecution) -> None:
    nested = snapshot.input["nested"]
    assert isinstance(nested, list)
    nested[0] = float("nan")


async def test_invalid_mutated_input_is_rejected_before_creating_a_task(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    task = replace(_task(), input={"nested": [0.0]})
    _alter(task)
    with pytest.raises(ValueError):
        await store.create_task(task, request_id="invalid", input_digest="invalid")
    with pytest.raises(TaskNotFoundError):
        await store.get_task(task.namespace, task.owner_id, task.task_id)


async def test_task_inputs_read_pages_and_command_results_are_independent_snapshots(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    task = replace(_task(), input={"nested": [0.0]})
    created = await store.create_task(task, request_id="create", input_digest="create")
    _alter(task)
    _alter(created)
    saved = await store.get_task("app", "owner-1", task.task_id)
    assert saved.input == {"nested": [0.0]}
    _alter(saved)
    _alter((await store.list_tasks("app", "owner-1", limit=100, cursor=None)).items[0])
    _alter((await store.list_scheduled_tasks("app", limit=100, cursor=None)).items[0])
    _alter(await store.get_scheduled_task("app", task.task_id))
    original = replace(task, input={"nested": [0.0]})
    repeated = await store.create_task(
        original, request_id="create", input_digest="create"
    )
    assert repeated.input == original.input
    _alter(repeated)
    updated = await store.update_task(
        replace(original, name="changed", revision=2),
        expected_revision=1,
        request_id="update",
        input_digest="update",
    )
    _alter(updated)
    assert (await store.get_task("app", "owner-1", task.task_id)).input == {
        "nested": [0.0]
    }
    await store.delete_task(
        "app",
        "owner-1",
        task.task_id,
        expected_revision=2,
        request_id="delete",
        input_digest="delete",
    )
    assert (
        await store.create_task(
            replace(original, input={"nested": [0.0]}),
            request_id="create",
            input_digest="create",
        )
    ).input == {"nested": [0.0]}


@pytest.mark.parametrize("settlement", ["interrupt", "finish", "resolve"])
async def test_execution_and_authorization_outputs_cannot_mutate_saved_json(
    store_with_clock: tuple[AutomationStore, ManualClock], settlement: str
) -> None:
    store, _ = store_with_clock
    execution = replace(
        _taskless_execution(execution_id="run"), input={"nested": [0.0]}
    )
    enqueued = await store.enqueue_execution(
        execution, occurrence_key="run", request_id="enqueue", input_digest="enqueue"
    )
    _alter(execution)
    _alter(enqueued)
    original = replace(execution, input={"nested": [0.0]})
    for value in (
        await store.get_execution("app", "owner-1", "run"),
        await store.get_scheduled_execution("app", "run"),
        (
            await store.list_executions(
                "app", "owner-1", task_id=None, limit=100, cursor=None
            )
        ).items[0],
        await store.enqueue_execution(
            original, occurrence_key="run", request_id="enqueue", input_digest="enqueue"
        ),
        await store.enqueue_execution(
            original, occurrence_key="run", request_id=None, input_digest="enqueue"
        ),
    ):
        assert value.input == original.input
        _alter(value)
    (claim,) = (
        await store.claim_work(
            "app",
            "worker",
            limit=1,
            lease_duration=timedelta(hours=1),
            global_concurrency=1,
        )
    ).claims
    authorization = await store.authorize_start(
        claim, execution_timeout=timedelta(minutes=10)
    )
    _alter(authorization.execution)
    if settlement == "interrupt":
        _alter(await store.mark_interrupted(claim, interrupt_ids=("approval",)))
        terminal = await store.cancel_execution(
            "app", "owner-1", "run", request_id="cancel", input_digest="cancel"
        )
    elif settlement == "resolve":
        _alter(
            await store.finish_execution(claim, status=ExecutionStatus.NEEDS_ATTENTION)
        )
        terminal = await store.resolve_execution(
            "app",
            "owner-1",
            "run",
            resolution=AttentionResolution.FAILED,
            request_id="resolve",
            input_digest="resolve",
            reason="Confirmed stopped",
        )
    else:
        terminal = await store.finish_execution(
            claim, status=ExecutionStatus.SUCCEEDED, result={"nested": [1.0]}
        )
        assert isinstance(terminal.result, dict)
        nested = terminal.result["nested"]
        assert isinstance(nested, list)
        nested[0] = float("nan")
        assert (await store.get_execution("app", "owner-1", "run")).result == {
            "nested": [1.0]
        }
    _alter(terminal)
    assert (await store.get_execution("app", "owner-1", "run")).input == original.input


async def test_materialized_outputs_and_queued_deletion_preserve_saved_snapshots(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    task = replace(_task(), input={"nested": [0.0]})
    await store.create_task(task, request_id=None, input_digest="task")
    execution = _execution(task, execution_id="run")
    assert task.next_run_at is not None
    result = await store.materialize_task(
        replace(task, next_run_at=None),
        expected_next_run_at=task.next_run_at,
        executions=(ScheduledExecution(execution=execution, occurrence_key="run"),),
    )
    _alter(execution)
    _alter(result.task)
    _alter(result.executions[0])
    await store.delete_task(
        "app",
        "owner-1",
        task.task_id,
        expected_revision=1,
        request_id=None,
        input_digest="delete",
    )
    saved = await store.get_execution("app", "owner-1", "run")
    assert saved.status is ExecutionStatus.CANCELLED
    assert saved.input == {"nested": [0.0]}
