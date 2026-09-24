"""Bound scheduled occurrences while keeping manual work and history independent."""

from datetime import UTC, datetime, timedelta

import pytest

from tinkerfin_automation import (
    CronSchedule,
    IntervalSchedule,
    InvalidScheduleError,
    MisfireMode,
    MisfirePolicy,
    OnceSchedule,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.schedules import materialize_schedule, preview_schedule
from tinkerfin_automation.service import AutomationService
from tinkerfin_automation.store import AutomationStore

START = datetime(2026, 9, 15, tzinfo=UTC)
END = START + timedelta(minutes=3)


@pytest.mark.parametrize("kind", ["once", "interval", "cron"])
def test_active_period_is_inclusive_then_exclusive(kind: str) -> None:
    if kind == "once":
        schedule = OnceSchedule(at=START, active_from=START, active_until=END)
    elif kind == "interval":
        schedule = IntervalSchedule(
            every_seconds=60,
            start_at=START - timedelta(days=1),
            active_from=START,
            active_until=END,
        )
    else:
        schedule = CronSchedule(
            expression="* * * * *", timezone="UTC", active_from=START, active_until=END
        )
    result = preview_schedule(schedule, after=START - timedelta(days=3))
    assert result[0] == START
    assert all(START <= value < END for value in result)
    assert len(result) == (1 if kind == "once" else 3)
    assert preview_schedule(schedule, after=END) == ()


@pytest.mark.parametrize("mode", list(MisfireMode))
def test_recovery_never_materializes_an_occurrence_after_expiry(
    mode: MisfireMode,
) -> None:
    schedule = IntervalSchedule(
        every_seconds=60, start_at=START, active_from=START, active_until=END
    )
    result = materialize_schedule(
        schedule,
        next_run_at=START,
        now=END + timedelta(minutes=1),
        policy=MisfirePolicy(mode=mode),
    )
    assert all(START <= value < END for value in result.due_at)
    assert result.next_run_at is None


@pytest.mark.parametrize("end", [START, START - timedelta(seconds=1)])
def test_invalid_active_period_is_rejected(end: datetime) -> None:
    with pytest.raises(ValueError, match="active_from"):
        OnceSchedule(at=START, active_from=START, active_until=end)


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
            owner_id="owner", name="Original", schedule=schedule, target="summary"
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


async def test_once_outside_window_is_not_saved() -> None:
    async with AutomationService(
        clock=ManualClock(START - timedelta(days=1))
    ) as service:
        with pytest.raises(InvalidScheduleError):
            await service.create_task(
                owner_id="owner",
                name="Outside",
                target="summary",
                schedule=OnceSchedule(at=END, active_from=START, active_until=END),
            )
