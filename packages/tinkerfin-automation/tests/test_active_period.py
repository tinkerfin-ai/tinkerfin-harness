"""Bound scheduled occurrences while keeping manual work and history independent."""

from datetime import timedelta

from tinkerfin_automation import (
    OnceSchedule,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.service import AutomationService
from tinkerfin_automation.store import AutomationStore


async def test_window_and_name_survive_storage_edit_manual_run_and_delete(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    now = clock.now()
    start = now + timedelta(days=1)
    schedule = OnceSchedule(
        at=start, active_from=start, active_until=start + timedelta(hours=1)
    )
    async with AutomationService(namespace="app", store=store, clock=clock) as service:
        task = await service.create_task(
            execution_namespace=service.namespace,
            owner_id="owner",
            name="Original",
            schedule=schedule,
            target="summary",
        )
        saved = await service.get_task(owner_id="owner", task_id=task.task_id)
        assert saved.schedule == schedule
        run = await service.run_task_now(owner_id="owner", task_id=task.task_id)
        assert run.task_name == "Original"
        updated = await service.update_task(
            owner_id="owner",
            task_id=task.task_id,
            expected_revision=task.revision,
            name="Changed",
            schedule=OnceSchedule(at=start),
        )
        assert updated.schedule.active_from is None
        await service.delete_task(
            owner_id="owner", task_id=task.task_id, expected_revision=updated.revision
        )
        assert (
            await service.get_execution(owner_id="owner", execution_id=run.execution_id)
        ).task_name == "Original"
