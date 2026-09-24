"""Shutdown settles every owned resource and preserves its caller's cancellation."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from test_engine_failures import _causes

from tinkerfin_automation import (
    AutomationLifecycleError,
    AutomationSchedulerError,
    AutomationStoreError,
    MemoryAutomationStore,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.scheduler import MemoryScheduler
from tinkerfin_automation.service import AutomationService


@pytest.mark.parametrize("borrowed", [False, True])
async def test_scheduler_close_failure_cannot_skip_owned_store(
    monkeypatch: pytest.MonkeyPatch, borrowed: bool
) -> None:
    scheduler_error = AutomationSchedulerError("Scheduler close failed")
    store_error = AutomationStoreError("Store close failed")
    closed: list[MemoryAutomationStore] = []
    original_close = MemoryAutomationStore.close

    class Scheduler(MemoryScheduler):
        async def close(self) -> None:
            raise scheduler_error

    async def store_close(store: MemoryAutomationStore) -> None:
        closed.append(store)
        await original_close(store)
        if not borrowed:
            raise store_error

    monkeypatch.setattr(MemoryAutomationStore, "close", store_close)
    external = MemoryAutomationStore() if borrowed else None
    service = AutomationService(namespace="app", scheduler=Scheduler(), store=external)
    try:
        for _ in range(2):
            with pytest.raises(AutomationSchedulerError) as caught:
                await service.close()
            assert scheduler_error in _causes(caught.value)
            if not borrowed:
                assert store_error in _causes(caught.value)
        assert len(closed) == (0 if borrowed else 1)
        if external is not None:
            await external.current_time()
    finally:
        if external is not None:
            await original_close(external)


async def test_service_close_waiter_cancellation_still_closes_default_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    closed: list[MemoryAutomationStore] = []
    original_close = MemoryAutomationStore.close

    class Scheduler(MemoryScheduler):
        async def close(self) -> None:
            entered.set()
            await release.wait()

    async def store_close(store: MemoryAutomationStore) -> None:
        closed.append(store)
        await original_close(store)

    monkeypatch.setattr(MemoryAutomationStore, "close", store_close)
    service = AutomationService(namespace="app", scheduler=Scheduler())
    closing = asyncio.create_task(service.close())
    try:
        await entered.wait()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert len(closed) == 1
        await service.close()
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


async def test_scheduler_close_keeps_callback_cleanup_and_waiter_cancellation() -> None:
    clock = ManualClock(datetime(2026, 9, 12, tzinfo=UTC))
    scheduler = MemoryScheduler(clock=clock)
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def callback(task_id: str) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    await scheduler.schedule_task("task", clock.now())
    await scheduler.start(callback)
    await entered.wait()
    closing = asyncio.create_task(scheduler.close())
    try:
        await cleaning.wait()
        other_closer = asyncio.create_task(scheduler.close())
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await other_closer
        assert cleaned.is_set()
        await scheduler.close()
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


async def test_scheduler_close_joins_a_timer_already_being_replaced() -> None:
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    class Clock(ManualClock):
        async def wait_until(self, when: datetime) -> None:
            entered.set()
            try:
                await super().wait_until(when)
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

    clock = Clock(datetime(2026, 9, 12, tzinfo=UTC))
    scheduler = MemoryScheduler(clock=clock)

    async def callback(task_id: str) -> None:
        raise AssertionError("No scheduled time has arrived")

    await scheduler.schedule_task("task", clock.now() + timedelta(hours=1))
    await scheduler.start(callback)
    await entered.wait()
    await scheduler.schedule_task("task", clock.now() + timedelta(hours=2))
    await cleaning.wait()
    closing = asyncio.create_task(scheduler.close())
    # A second close joins the same accepted shutdown while the timer is settling.
    other = asyncio.create_task(scheduler.close())
    try:
        release.set()
        await closing
        await other
        assert cleaned.is_set()
    finally:
        release.set()
        await asyncio.gather(closing, other, return_exceptions=True)


@pytest.mark.parametrize("close_service", [False, True])
async def test_due_callback_rejects_waiting_for_its_own_shutdown(
    close_service: bool,
) -> None:
    clock = ManualClock(datetime(2026, 9, 12, tzinfo=UTC))
    scheduler = MemoryScheduler(clock=clock)
    service = AutomationService(namespace="app", scheduler=scheduler, clock=clock)
    observed = asyncio.Event()

    async def callback(task_id: str) -> None:
        with pytest.raises(AutomationLifecycleError, match="after its callback"):
            await (service.close() if close_service else scheduler.close())
        observed.set()

    await scheduler.schedule_task("task", clock.now())
    await scheduler.start(callback)
    await observed.wait()
    await service.close()


async def test_completed_callback_does_not_restrict_a_later_child_shutdown() -> None:
    clock = ManualClock(datetime(2026, 9, 12, tzinfo=UTC))
    scheduler = MemoryScheduler(clock=clock)
    service = AutomationService(namespace="app", scheduler=scheduler, clock=clock)
    returned, release = asyncio.Event(), asyncio.Event()
    children: list[asyncio.Task[None]] = []

    async def close_later() -> None:
        await release.wait()
        await service.close()

    async def callback(task_id: str) -> None:
        children.append(asyncio.create_task(close_later()))
        returned.set()

    await scheduler.schedule_task("task", clock.now())
    await scheduler.start(callback)
    try:
        await returned.wait()
        release.set()
        await asyncio.gather(*children)
    finally:
        release.set()
        await service.close()
        await asyncio.gather(*children, return_exceptions=True)


async def test_nested_callback_cannot_close_an_active_ancestor() -> None:
    clock = ManualClock(datetime(2026, 9, 12, tzinfo=UTC))
    first, second = MemoryScheduler(clock=clock), MemoryScheduler(clock=clock)
    observed = asyncio.Event()

    async def nested(task_id: str) -> None:
        with pytest.raises(AutomationLifecycleError, match="after its callback"):
            await first.close()
        observed.set()

    async def outer(task_id: str) -> None:
        await second.schedule_task("nested", clock.now())
        await second.dispatch_due()

    await second.start(nested)
    await first.schedule_task("outer", clock.now())
    await first.start(outer)
    try:
        await observed.wait()
    finally:
        await first.close()
        await second.close()


async def test_callback_can_close_an_unrelated_scheduler() -> None:
    clock = ManualClock(datetime(2026, 9, 12, tzinfo=UTC))
    first, second = MemoryScheduler(clock=clock), MemoryScheduler(clock=clock)
    observed = asyncio.Event()

    async def callback(task_id: str) -> None:
        await second.close()
        observed.set()

    await first.schedule_task("outer", clock.now())
    await first.start(callback)
    await observed.wait()
    await first.close()
