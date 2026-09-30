"""Committed command receipts remain scoped historical snapshots."""

from dataclasses import replace

import pytest
from test_store_contract import _task, _taskless_execution

from tinkerfin_automation import AutomationExecution, AutomationTask, TaskNotFoundError
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore


@pytest.mark.parametrize("kind", ["task", "execution", "deleted"])
async def test_receipts_preserve_committed_results_and_owner_isolation(
    store_with_clock: tuple[AutomationStore, ManualClock], kind: str
) -> None:
    store, _ = store_with_clock
    task = replace(_task(), input={"nested": [1]})
    assert await store.get_command_receipt("app", "owner-1", "command") is None
    if kind == "execution":
        result = replace(_taskless_execution(execution_id="run"), input=task.input)
        await store.enqueue_execution(
            result, occurrence_key="run", request_id="command", input_digest="digest"
        )
    else:
        await store.create_task(
            task,
            request_id="command" if kind == "task" else None,
            input_digest="digest",
        )
        await store.delete_task(
            "app",
            "owner-1",
            task.task_id,
            expected_revision=1,
            request_id="command" if kind == "deleted" else None,
            input_digest="digest",
        )
        with pytest.raises(TaskNotFoundError):
            await store.get_task("app", "owner-1", task.task_id)

    for scope in [
        ("other", "owner-1", "command"),
        ("app", "other", "command"),
        ("app", "owner-1", "missing"),
    ]:
        assert await store.get_command_receipt(*scope) is None
    receipt = await store.get_command_receipt("app", "owner-1", "command")
    assert receipt is not None and receipt.input_digest == "digest"
    if kind == "deleted":
        assert receipt.result == task.task_id
    else:
        assert isinstance(receipt.result, AutomationTask | AutomationExecution)
        nested = receipt.result.input["nested"]
        assert isinstance(nested, list)
        nested.append(2)
        reread = await store.get_command_receipt("app", "owner-1", "command")
        assert reread is not None
        assert isinstance(reread.result, AutomationTask | AutomationExecution)
        assert reread.result.input == {"nested": [1]}


@pytest.mark.parametrize(
    "scope",
    [
        ("", "owner-1", "request"),
        ("app", " padded ", "request"),
        ("app", "owner-1", ""),
        ("app", "owner-1", "r" * 129),
    ],
)
async def test_receipt_identifiers_are_validated(
    store_with_clock: tuple[AutomationStore, ManualClock],
    scope: tuple[str, str, str],
) -> None:
    store, _ = store_with_clock
    with pytest.raises(ValueError):
        await store.get_command_receipt(*scope)
