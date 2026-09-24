"""Verify engine failure visibility, task ownership, and safe capacity accounting."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import pytest

from tinkerfin_automation import (
    AutomationStoreError,
    ExecutionInterrupted,
    ExecutionStatus,
    FunctionTarget,
    MemoryAutomationStore,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.engine import AutomationEngine
from tinkerfin_automation.scheduler import MemoryScheduler
from tinkerfin_automation.service import AutomationService
from tinkerfin_automation.store import WorkItemClaim

NOW = datetime(2026, 9, 9, 8, tzinfo=UTC)
_T = TypeVar("_T")


async def _capture(operation: Awaitable[_T]) -> _T | BaseException:
    try:
        return await operation
    except BaseException as error:  # noqa: BLE001 - assert original control outcomes
        return error


def _causes(error: BaseException) -> list[BaseException]:
    pending = [error]
    result: list[BaseException] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(
            cause
            for cause in (current.__cause__, current.__context__)
            if cause is not None
        )
    return result


class _Clock(ManualClock):
    def __init__(self) -> None:
        super().__init__(NOW)
        self.poll_waiting = asyncio.Event()
        self.drain_waiting = asyncio.Event()

    async def wait_until(self, when: datetime) -> None:
        delay = when - self.now()
        if delay == timedelta(seconds=2):
            self.poll_waiting.set()
        if delay == timedelta(seconds=7):
            self.drain_waiting.set()
        await super().wait_until(when)


class _Store(MemoryAutomationStore):
    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock=clock)
        self.failure = AutomationStoreError("Storage is unavailable")
        self.fail_claim = False
        self.fail_renewal = False
        self.claim_failed = asyncio.Event()
        self.renewal_failed = asyncio.Event()

    async def claim_work(
        self, namespace, worker_id, *, limit, lease_duration, global_concurrency
    ):
        if self.fail_claim:
            self.claim_failed.set()
            raise self.failure
        return await super().claim_work(
            namespace,
            worker_id,
            limit=limit,
            lease_duration=lease_duration,
            global_concurrency=global_concurrency,
        )

    async def renew_claim(
        self, claim: WorkItemClaim, *, lease_duration: timedelta
    ) -> WorkItemClaim:
        if self.fail_renewal:
            self.renewal_failed.set()
            raise self.failure
        return await super().renew_claim(claim, lease_duration=lease_duration)


@pytest.mark.parametrize("operation_kind", ["target", "callback"])
async def test_renewal_failure_joins_extension_and_keeps_unconfirmed_capacity(
    operation_kind: str,
) -> None:
    clock = _Clock()
    store = _Store(clock)
    service = AutomationService(namespace="app", store=store, clock=clock)
    entered, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def extension(_request):
        entered.set()
        try:
            await release.wait()
        finally:
            stopped.set()
        return None

    async def target(request):
        if operation_kind == "callback":
            return ExecutionInterrupted(("approval",))
        await extension(request)
        return {"ok": True}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(target, cancellation_is_final=True)},
        on_interrupt=extension if operation_kind == "callback" else None,
        clock=clock,
        poll_interval=timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
    )
    try:
        await engine.start()
        execution = await service.execute_once(
            owner_id="owner", target="target", input={}
        )
        await entered.wait()
        store.fail_renewal = True
        await clock.advance(timedelta(seconds=15))
        await store.renewal_failed.wait()
        result = await _capture(engine.wait_until_idle())
        assert result is store.failure
        assert stopped.is_set()
        saved = await service.get_execution(
            owner_id="owner", execution_id=execution.execution_id
        )
        assert saved.status is ExecutionStatus.RUNNING
        assert saved.failure_code is None
        # A second worker cannot use capacity reserved for unconfirmed work.
        await service.execute_once(owner_id="owner", target="target", input={})
        assert (
            await store.claim_work(
                "app",
                "peer",
                limit=1,
                lease_duration=timedelta(seconds=30),
                global_concurrency=1,
            )
            == ()
        )
    finally:
        release.set()
        result = await _capture(engine.close())
        assert result is store.failure
        await store.close()


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_failed_supervisor_stops_admission_and_close_joins_owned_target(
    cancel_waiter: bool,
) -> None:
    clock = _Clock()
    store = _Store(clock)
    service = AutomationService(namespace="app", store=store, clock=clock)
    entered, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fully_stopped = asyncio.Event()

    async def target(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await release.wait()
            fully_stopped.set()

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(target, cancellation_is_final=True)},
        clock=clock,
        poll_interval=timedelta(seconds=2),
        drain_timeout=timedelta(seconds=7),
    )
    closers: list[asyncio.Task[object]] = []
    try:
        await engine.start()
        execution = await service.execute_once(
            owner_id="owner", target="target", input={}
        )
        await entered.wait()
        await clock.poll_waiting.wait()
        store.fail_claim = True
        await clock.advance(timedelta(seconds=2))
        await store.claim_failed.wait()
        assert await _capture(engine.run_ready()) is store.failure
        first = asyncio.create_task(_capture(engine.close()))
        second = asyncio.create_task(_capture(engine.close()))
        closers.extend((first, second))
        await clock.drain_waiting.wait()
        if cancel_waiter:
            first.cancel()
        await clock.advance(timedelta(seconds=7))
        await stopping.wait()
        if cancel_waiter:
            first.cancel()
        assert not first.done() and not second.done()
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert fully_stopped.is_set()
        assert second_result is store.failure
        if cancel_waiter:
            assert isinstance(first_result, asyncio.CancelledError)
            assert store.failure in _causes(first_result)
        else:
            assert first_result is store.failure
        saved = await service.get_execution(
            owner_id="owner", execution_id=execution.execution_id
        )
        assert saved.status is ExecutionStatus.NEEDS_ATTENTION
    finally:
        release.set()
        await asyncio.gather(*closers)
        await _capture(engine.close())
        await store.close()


@pytest.mark.parametrize("operation_kind", ["target", "callback"])
@pytest.mark.parametrize("control_type", [SystemExit, KeyboardInterrupt])
async def test_extension_control_is_delivered_after_its_owned_tasks_exit(
    operation_kind, control_type
) -> None:
    clock = _Clock()
    store = _Store(clock)
    service = AutomationService(namespace="app", store=store, clock=clock)
    control = control_type("extension stopped")

    async def stop(_request):
        raise control

    async def execute(request):
        if operation_kind == "callback":
            return ExecutionInterrupted(("approval",))
        return await stop(request)

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        on_interrupt=stop if operation_kind == "callback" else None,
        clock=clock,
    )
    try:
        await engine.start()
        await service.execute_once(owner_id="owner", target="target")
        result = await _capture(engine.wait_until_idle())
        assert result is control
    finally:
        assert await _capture(engine.close()) is control
        await store.close()


@pytest.mark.parametrize("cleanup_type", [RuntimeError, SystemExit, KeyboardInterrupt])
async def test_scheduler_failure_cannot_skip_target_cleanup_and_both_causes_survive(
    cleanup_type,
) -> None:
    clock = _Clock()
    store = _Store(clock)
    scheduler_failure = OSError("scheduler close failed")
    cleanup_failure = cleanup_type("target cleanup failed")
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Scheduler(MemoryScheduler):
        async def close(self) -> None:
            await super().close()
            raise scheduler_failure

    service = AutomationService(
        namespace="app", store=store, scheduler=Scheduler(clock=clock), clock=clock
    )

    async def execute(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
            raise cleanup_failure

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        clock=clock,
        drain_timeout=timedelta(seconds=7),
    )
    closing = None
    try:
        await engine.start()
        await service.execute_once(owner_id="owner", target="target")
        await entered.wait()
        closing = asyncio.create_task(_capture(engine.close()))
        await clock.drain_waiting.wait()
        await clock.advance(timedelta(seconds=7))
        result = await closing
        assert stopped.is_set()
        assert isinstance(result, BaseException)
        assert scheduler_failure in _causes(result)
        assert cleanup_failure in _causes(result)
        if not isinstance(cleanup_failure, Exception):
            assert result is cleanup_failure
    finally:
        if closing is not None:
            await closing
        await _capture(engine.close())
        await store.close()
