"""Use stable ordering and cursors confined to the current ownership and filters."""

from dataclasses import replace

import pytest
from test_store_contract import _execution, _task

from tinkerfin_automation import TaskStatus
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore


async def test_task_pages_keep_stable_ties_and_owner_scope(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    for task_id in ("c", "a", "b"):
        await store.create_task(
            _task(task_id=task_id), request_id=None, input_digest=task_id
        )
    await store.create_task(
        replace(_task(task_id="outside"), owner_id="other"),
        request_id=None,
        input_digest="other",
    )
    first = await store.list_tasks("app", "owner-1", limit=2, cursor=None)
    assert tuple(item.task_id for item in first.items) == ("a", "b")
    assert first.next_cursor is not None
    second = await store.list_tasks("app", "owner-1", limit=2, cursor=first.next_cursor)
    assert tuple(item.task_id for item in second.items) == ("c",)
    assert second.next_cursor is None
    with pytest.raises(ValueError, match="cursor"):
        await store.list_tasks("app", "owner-1", limit=2, cursor="outside")
    with pytest.raises(ValueError, match="another query"):
        await store.list_tasks("another", "owner-1", limit=2, cursor=first.next_cursor)


async def test_scheduler_cursor_must_belong_to_the_enabled_task_selection(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    for task_id, status in (
        ("a", TaskStatus.PAUSED),
        ("b", TaskStatus.ENABLED),
        ("c", TaskStatus.ENABLED),
    ):
        await store.create_task(
            replace(_task(task_id=task_id), status=status),
            request_id=None,
            input_digest=task_id,
        )
    first = await store.list_scheduled_tasks("app", limit=1, cursor=None)
    assert tuple(item.task_id for item in first.items) == ("b",)
    assert first.next_cursor == "b"
    second = await store.list_scheduled_tasks("app", limit=1, cursor=first.next_cursor)
    assert tuple(item.task_id for item in second.items) == ("c",)
    assert second.next_cursor is None
    assert not (await store.list_scheduled_tasks("app", limit=1, cursor="a")).items


async def test_execution_cursor_must_belong_to_the_selected_task(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    task = replace(
        _task(task_id="task"), limits=replace(_task().limits, max_queued_runs=3)
    )
    other = _task(task_id="other")
    for current in (task, other):
        await store.create_task(current, request_id=None, input_digest=current.task_id)
    for execution_id, current in (("a", task), ("b", task), ("c", other)):
        execution = _execution(current, execution_id=execution_id)
        await store.enqueue_execution(
            execution,
            occurrence_key=execution_id,
            request_id=None,
            input_digest=execution_id,
        )
    first = await store.list_executions(
        "app", "owner-1", task_id="task", limit=1, cursor=None
    )
    assert tuple(item.execution_id for item in first.items) == ("b",)
    assert first.next_cursor is not None
    second = await store.list_executions(
        "app", "owner-1", task_id="task", limit=1, cursor=first.next_cursor
    )
    assert tuple(item.execution_id for item in second.items) == ("a",)
    assert second.next_cursor is None
    with pytest.raises(ValueError, match="another query"):
        await store.list_executions(
            "app", "owner-1", task_id="other", limit=1, cursor=first.next_cursor
        )
    assert tuple(
        item.execution_id
        for item in (
            await store.list_executions(
                "app", "owner-1", task_id=None, limit=100, cursor=None
            )
        ).items
    ) == ("c", "b", "a")
