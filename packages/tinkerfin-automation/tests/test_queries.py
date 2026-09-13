"""Search, aggregate and paginate the same owner-scoped saved facts."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_store_contract import _execution, _task

from tinkerfin_automation import (
    ExecutionFilter,
    ExecutionStatus,
    TaskFilter,
    TaskStatus,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore


async def test_literal_casefold_search_and_counts_match_across_pages(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    for task_id, name, status, owner in [
        ("a", "STRASSE 100%_", TaskStatus.ENABLED, "owner-1"),
        ("b", "Straße 100%_", TaskStatus.PAUSED, "owner-1"),
        ("c", "strasse 100xx", TaskStatus.ENABLED, "owner-1"),
        ("d", "STRASSE 100%_", TaskStatus.ENABLED, "other"),
    ]:
        task = replace(_task(task_id=task_id), name=name, status=status, owner_id=owner)
        await store.create_task(task, request_id=None, input_digest=task_id)
    filters = TaskFilter(name_contains="straße 100%_")
    page = await store.list_tasks(
        "app", "owner-1", filters=filters, limit=1, cursor=None
    )
    assert [task.task_id for task in page.items] == ["a"]
    assert page.next_cursor is not None
    assert await store.summarize_tasks("app", "owner-1", filters=filters) == {
        TaskStatus.ENABLED: 1,
        TaskStatus.PAUSED: 1,
    }
    await store.delete_task(
        "app",
        "owner-1",
        "a",
        expected_revision=1,
        request_id=None,
        input_digest="delete",
    )
    following = await store.list_tasks(
        "app", "owner-1", filters=filters, limit=1, cursor=page.next_cursor
    )
    assert [task.task_id for task in following.items] == ["b"]
    with pytest.raises(ValueError, match="another query"):
        await store.list_tasks(
            "app",
            "owner-1",
            filters=TaskFilter(statuses=(TaskStatus.PAUSED,)),
            limit=1,
            cursor=page.next_cursor,
        )


async def test_history_uses_captured_name_and_half_open_queue_interval(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    now = clock.now()
    task = replace(_task(), limits=replace(_task().limits, max_queued_runs=3))
    await store.create_task(task, request_id=None, input_digest="task")
    for offset in (0, 1, 2):
        execution = replace(
            _execution(task, execution_id=str(offset)),
            task_name="日报",
            queued_at=now + timedelta(minutes=offset),
        )
        await store.enqueue_execution(
            execution,
            occurrence_key=str(offset),
            request_id=None,
            input_digest=str(offset),
        )
    filters = ExecutionFilter(
        name_contains="日报",
        queued_from=now + timedelta(minutes=1),
        queued_until=now + timedelta(minutes=2),
    )
    page = await store.list_executions(
        "app", "owner-1", task_id=None, filters=filters, limit=50, cursor=None
    )
    assert [item.execution_id for item in page.items] == ["1"]
    counts = await store.summarize_executions("app", "owner-1", filters=filters)
    assert counts[ExecutionStatus.QUEUED] == 1
    assert sum(counts.values()) == 1
    assert (
        sum(
            (await store.summarize_executions("app", "other", filters=filters)).values()
        )
        == 0
    )
