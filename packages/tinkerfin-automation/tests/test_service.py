from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tinkerfin_automation import (
    AutomationLifecycleError,
    ExecutionOrigin,
    ExecutionStatus,
    IntervalSchedule,
    MemoryAutomationStore,
    RequestConflictError,
    TaskNotFoundError,
    TaskStatus,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.service import AutomationService

NOW = datetime(2026, 9, 9, 8, tzinfo=UTC)


class _RecordingScheduler:
    def __init__(self) -> None:
        self.scheduled: dict[str, datetime] = {}
        self.closed = False

    async def start(self, on_task_due) -> None:
        self.on_task_due = on_task_due

    async def schedule_task(self, task_id: str, run_at: datetime) -> None:
        self.scheduled[task_id] = run_at

    async def remove_task(self, task_id: str) -> None:
        self.scheduled.pop(task_id, None)

    async def close(self) -> None:
        self.closed = True


class _RecordingSetupStore(MemoryAutomationStore):
    def __init__(self, *, clock: ManualClock) -> None:
        super().__init__(clock=clock)
        self.events: list[str] = []

    async def setup(self) -> None:
        self.events.append("store.setup")
        await super().setup()

    async def current_time(self) -> datetime:
        self.events.append("store.current_time")
        return await super().current_time()


class _BlockingServiceSetupStore(MemoryAutomationStore):
    def __init__(self, *, clock: ManualClock) -> None:
        super().__init__(clock=clock)
        self.setup_started = asyncio.Event()
        self.setup_release = asyncio.Event()
        self.current_time_called = False

    async def setup(self) -> None:
        self.setup_started.set()
        await self.setup_release.wait()
        await super().setup()

    async def current_time(self) -> datetime:
        self.current_time_called = True
        return await super().current_time()


@pytest.mark.asyncio
async def test_service_crud_uses_task_names_and_keeps_scheduler_in_sync() -> None:
    clock = ManualClock(NOW)
    scheduler = _RecordingScheduler()
    service = AutomationService(
        namespace="app",
        store=MemoryAutomationStore(clock=clock),
        scheduler=scheduler,
        clock=clock,
    )
    schedule = IntervalSchedule(every_seconds=60, start_at=NOW)

    task = await service.create_task(
        owner_id="owner-1",
        name="Summary",
        schedule=schedule,
        target="summary",
        request_id="create-1",
    )
    repeated = await service.create_task(
        owner_id="owner-1",
        name="Summary",
        schedule=schedule,
        target="summary",
        request_id="create-1",
    )
    assert repeated.task_id == task.task_id
    assert scheduler.scheduled[task.task_id] == NOW + timedelta(minutes=1)
    with pytest.raises(RequestConflictError):
        await service.create_task(
            owner_id="owner-1",
            name="Different",
            schedule=schedule,
            target="summary",
            request_id="create-1",
        )

    updated = await service.update_task(
        owner_id="owner-1",
        task_id=task.task_id,
        expected_revision=1,
        name="Renamed",
        request_id="update-1",
    )
    assert updated.revision == 2
    assert updated.name == "Renamed"

    paused = await service.pause_task(
        owner_id="owner-1",
        task_id=task.task_id,
        expected_revision=2,
        request_id="pause-1",
    )
    assert paused.status is TaskStatus.PAUSED
    assert task.task_id not in scheduler.scheduled

    await clock.advance(timedelta(minutes=10))
    enabled = await service.enable_task(
        owner_id="owner-1",
        task_id=task.task_id,
        expected_revision=3,
        request_id="enable-1",
    )
    assert enabled.status is TaskStatus.ENABLED
    assert enabled.next_run_at == NOW + timedelta(minutes=11)

    page = await service.list_tasks(owner_id="owner-1")
    assert [item.task_id for item in page.items] == [task.task_id]
    await service.delete_task(
        owner_id="owner-1",
        task_id=task.task_id,
        expected_revision=4,
        request_id="delete-1",
    )
    await service.delete_task(
        owner_id="owner-1",
        task_id=task.task_id,
        expected_revision=4,
        request_id="delete-1",
    )
    with pytest.raises(TaskNotFoundError):
        await service.get_task(owner_id="owner-1", task_id=task.task_id)
    assert task.task_id not in scheduler.scheduled
    await service.close()
    assert scheduler.closed


@pytest.mark.asyncio
async def test_service_prepares_store_before_direct_use() -> None:
    clock = ManualClock(NOW)
    store = _RecordingSetupStore(clock=clock)
    service = AutomationService(namespace="app", store=store, clock=clock)

    await service.execute_once(owner_id="owner-1", target="summary")
    await service.list_executions(owner_id="owner-1")

    assert store.events[:2] == ["store.setup", "store.current_time"]
    assert store.events.count("store.setup") == 2


@pytest.mark.asyncio
async def test_service_close_during_setup_prevents_operation() -> None:
    clock = ManualClock(NOW)
    store = _BlockingServiceSetupStore(clock=clock)
    service = AutomationService(namespace="app", store=store, clock=clock)
    operation = asyncio.create_task(
        service.execute_once(owner_id="owner-1", target="summary")
    )
    await store.setup_started.wait()
    await service.close()
    store.setup_release.set()

    with pytest.raises(AutomationLifecycleError, match="closed"):
        await operation
    assert not store.current_time_called
    await store.close()


@pytest.mark.asyncio
async def test_task_run_now_is_idempotent_and_keeps_schedule_unchanged() -> None:
    clock = ManualClock(NOW)
    store = MemoryAutomationStore(clock=clock)
    service = AutomationService(namespace="app", store=store, clock=clock)
    task = await service.create_task(
        owner_id="owner-1",
        name="Summary",
        schedule=IntervalSchedule(every_seconds=60, start_at=NOW),
        target="summary",
    )
    next_run_at = task.next_run_at
    first = await service.run_task_now(
        owner_id="owner-1", task_id=task.task_id, request_id="manual-1"
    )
    repeated = await service.run_task_now(
        owner_id="owner-1", task_id=task.task_id, request_id="manual-1"
    )
    assert repeated.execution_id == first.execution_id
    assert (
        await service.get_task(owner_id="owner-1", task_id=task.task_id)
    ).next_run_at == next_run_at


@pytest.mark.asyncio
async def test_execute_once_is_taskless_idempotent_and_retries_as_taskless() -> None:
    clock = ManualClock(NOW)
    store = MemoryAutomationStore(clock=clock)
    service = AutomationService(namespace="app", store=store, clock=clock)
    first = await service.execute_once(
        owner_id="owner-1",
        target="summary",
        input={"project_id": "project-1"},
        request_id="once-1",
    )
    repeated = await service.execute_once(
        owner_id="owner-1",
        target="summary",
        input={"project_id": "project-1"},
        request_id="once-1",
    )
    assert repeated.execution_id == first.execution_id
    assert first.task_id is None
    assert first.origin is ExecutionOrigin.ONE_TIME
    assert not (await service.list_tasks(owner_id="owner-1")).items
    with pytest.raises(RequestConflictError):
        await service.execute_once(
            owner_id="owner-1",
            target="summary",
            input={"project_id": "different"},
            request_id="once-1",
        )

    (claim,) = await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=16,
    )
    await store.authorize_start(claim, execution_timeout=timedelta(minutes=30))
    await store.finish_execution(
        claim,
        status=ExecutionStatus.FAILED,
        failure_code="host.failed",
        failure_message="Host failed",
    )
    retry = await service.retry_execution(
        owner_id="owner-1",
        execution_id=first.execution_id,
        request_id="retry-1",
    )
    assert retry.retry_of == first.execution_id
    assert retry.task_id is None
    assert retry.attempt == 2
    assert retry.execution_id != first.execution_id
    assert retry.identity != first.identity


@pytest.mark.asyncio
async def test_delete_cancels_unstarted_execution_but_retains_history() -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    task = await service.create_task(
        owner_id="owner-1",
        name="Summary",
        schedule=IntervalSchedule(every_seconds=60, start_at=NOW),
        target="summary",
    )
    execution = await service.run_task_now(owner_id="owner-1", task_id=task.task_id)
    await service.delete_task(
        owner_id="owner-1",
        task_id=task.task_id,
        expected_revision=1,
    )

    retained = await service.get_execution(
        owner_id="owner-1", execution_id=execution.execution_id
    )
    assert retained.status is ExecutionStatus.CANCELLED


@pytest.mark.asyncio
async def test_due_wakeup_atomically_advances_task_and_queues_latest_misfire() -> None:
    clock = ManualClock(NOW)
    scheduler = _RecordingScheduler()
    service = AutomationService(
        namespace="app",
        store=MemoryAutomationStore(clock=clock),
        scheduler=scheduler,
        clock=clock,
    )
    task = await service.create_task(
        owner_id="owner-1",
        execution_namespace="owner-runtime",
        name="Summary",
        schedule=IntervalSchedule(every_seconds=60, start_at=NOW),
        target="summary",
    )

    await clock.advance(timedelta(minutes=5, seconds=20))
    await service._materialize_due_task(task.task_id)

    history = await service.list_executions(owner_id="owner-1", task_id=task.task_id)
    assert len(history.items) == 1
    assert history.items[0].namespace == "app"
    assert history.items[0].identity.namespace == "owner-runtime"
    assert history.items[0].scheduled_for == NOW + timedelta(minutes=5)
    updated = await service.get_task(owner_id="owner-1", task_id=task.task_id)
    assert updated.next_run_at == NOW + timedelta(minutes=6)
    assert scheduler.scheduled[task.task_id] == NOW + timedelta(minutes=6)
