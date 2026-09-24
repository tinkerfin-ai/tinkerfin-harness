"""The application facade owns one lifecycle and never adopts borrowed stores."""

import asyncio
from datetime import UTC, datetime

import pytest

from tinkerfin_automation import (
    AutomationLifecycleError,
    MemoryAutomationStore,
    Schedule,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.facade import Automation
from tinkerfin_automation.scheduler import MemoryScheduler
from tinkerfin_automation.targets import ExecutionRequest

NOW = datetime(2026, 9, 22, tzinfo=UTC)


class RecordingStore(MemoryAutomationStore):
    def __init__(self, *, blocked: bool = False) -> None:
        super().__init__(clock=ManualClock(NOW))
        self.setups = 0
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def setup(self) -> None:
        self.setups += 1
        self.started.set()
        await self.release.wait()
        await super().setup()

    async def close(self) -> None:
        self.closed = True
        await super().close()


async def report(request: ExecutionRequest) -> None:
    return None


async def test_configuration_is_pure_and_registration_preserves_identity() -> None:
    store = RecordingStore()
    app = Automation(namespace="app", store=store)
    assert app.target("report")(report) is report
    assert app.for_owner("subject").owner_id == "subject"
    assert app.for_owner("subject").namespace == "app"
    context = app.worker()
    assert store.setups == 0
    with pytest.raises(ValueError):
        app.target("report", report)
    with pytest.raises(AutomationLifecycleError):
        await app.preview_schedule(Schedule.every(minutes=1, start_at=NOW))
    async with context as worker:
        await worker.check_ready()
        assert await app.preview_schedule(
            Schedule.every(minutes=1, start_at=NOW), count=1
        )
        with pytest.raises(AutomationLifecycleError):
            app.target("new", report)
        with pytest.raises(AutomationLifecycleError):
            await app.__aenter__()
        with pytest.raises(AutomationLifecycleError):
            app.worker()
    assert not store.closed
    with pytest.raises(AutomationLifecycleError):
        await app.__aenter__()
    await store.close()


@pytest.mark.parametrize("owner_id", [42])
def test_owner_requires_an_explicit_string_identity(owner_id) -> None:
    with pytest.raises(TypeError):
        Automation().for_owner(owner_id)


async def test_client_has_no_target_requirement_and_closed_snapshots_are_local() -> (
    None
):
    app = Automation(clock=ManualClock(NOW))
    owner = app.for_owner("subject")
    async with app:
        assert await app.preview_schedule(
            Schedule.every(minutes=1, start_at=NOW), count=1
        )
    assert owner.owner_id == "subject"
    await app.aclose()
    with pytest.raises(AutomationLifecycleError):
        await app.preview_schedule(Schedule.every(minutes=1, start_at=NOW))


async def test_invalid_worker_start_closes_the_instance() -> None:
    app = Automation()
    with pytest.raises(ValueError, match="targets"):
        async with app.worker():
            pytest.fail("empty worker entered")
    with pytest.raises(AutomationLifecycleError):
        app.target("late", report)


async def test_cancelled_startup_settles_setup_and_closes_owned_scheduler() -> None:
    store = RecordingStore(blocked=True)
    app = Automation(store=store)
    starting = asyncio.create_task(app.__aenter__())
    await store.started.wait()
    starting.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await starting
    with pytest.raises(AutomationLifecycleError):
        await app.__aenter__()
    assert not store.closed
    await store.close()


async def test_close_during_startup_does_not_publish_an_active_client() -> None:
    store = RecordingStore(blocked=True)
    app = Automation(store=store)
    starting = asyncio.create_task(app.__aenter__())
    await store.started.wait()
    closing_started = asyncio.Event()

    async def close() -> None:
        closing_started.set()
        await app.aclose()

    closing = asyncio.create_task(close())
    await closing_started.wait()
    store.release.set()
    await closing
    with pytest.raises(AutomationLifecycleError):
        await starting
    await store.close()


async def test_body_and_shared_cleanup_failure_remain_observable() -> None:
    class FailingScheduler(MemoryScheduler):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError("shutdown failed")

    app = Automation(scheduler=FailingScheduler())
    with pytest.raises(ValueError, match="body") as caught:
        async with app:
            raise ValueError("body")
    assert isinstance(caught.value.__cause__, AutomationLifecycleError)
    results = await asyncio.gather(app.aclose(), app.aclose(), return_exceptions=True)
    assert results[0] is results[1]
    assert isinstance(results[0], AutomationLifecycleError)
