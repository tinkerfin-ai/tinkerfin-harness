"""Notifications wake workers and observers without frequent empty Store reads."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue
from test_store_contract import _taskless_execution

from tinkerfin_automation import (
    Automation,
    ExecutionStatus,
    FunctionTarget,
    MemoryAutomationStore,
    OnceSchedule,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.engine import AutomationEngine
from tinkerfin_automation.errors import AutomationStoreError
from tinkerfin_automation.models import AutomationExecution
from tinkerfin_automation.scheduler import MemoryScheduler
from tinkerfin_automation.service import AutomationService
from tinkerfin_automation.store import (
    StartAuthorization,
    WorkClaimBatch,
    WorkItemClaim,
    WorkKind,
)
from tinkerfin_automation.targets import ExecutionRequest
from tinkerfin_notifications import MemoryBackend, Notifications

NOW = datetime(2026, 9, 9, 8, tzinfo=UTC)


class ObservedClock(ManualClock):
    def __init__(self) -> None:
        super().__init__(NOW)
        self.waits: asyncio.Queue[datetime] = asyncio.Queue(maxsize=64)

    async def wait_until(self, when: datetime) -> None:
        self.waits.put_nowait(when)
        await super().wait_until(when)


class ObservedStore(MemoryAutomationStore):
    def __init__(self, clock: ManualClock, notifications: Notifications) -> None:
        super().__init__(clock=clock, notifications=notifications)
        self.claim_calls = 0
        self.reads: asyncio.Queue[AutomationExecution] = asyncio.Queue(maxsize=64)

    async def claim_work(
        self,
        namespace: str,
        worker_id: str,
        *,
        limit: int,
        lease_duration: timedelta,
        global_concurrency: int,
    ) -> WorkClaimBatch:
        self.claim_calls += 1
        return await super().claim_work(
            namespace,
            worker_id,
            limit=limit,
            lease_duration=lease_duration,
            global_concurrency=global_concurrency,
        )

    async def get_execution(
        self, namespace: str, owner_id: str, execution_id: str
    ) -> AutomationExecution:
        execution = await super().get_execution(namespace, owner_id, execution_id)
        self.reads.put_nowait(execution)
        return execution


async def test_idle_worker_waits_for_repair_and_remote_enqueue_wakes_it_immediately() -> (
    None
):
    clock = ObservedClock()
    started, release = asyncio.Event(), asyncio.Event()

    async def target(_request: ExecutionRequest) -> None:
        started.set()
        await release.wait()

    async with Notifications() as notifications:
        store = ObservedStore(clock, notifications)
        service = AutomationService(namespace="app", store=store, clock=clock)
        remote = AutomationService(namespace="app", store=store, clock=clock)
        async with AutomationEngine(
            service, targets={"task": FunctionTarget(target)}, clock=clock
        ) as engine:
            try:
                assert await clock.waits.get() == NOW + timedelta(seconds=30)
                assert store.claim_calls == 1
                await remote.execute_once(owner_id="owner", target="task")
                await started.wait()
                assert clock.now() == NOW
            finally:
                release.set()
            await engine.wait_until_idle()
        await remote.close()
        await service.close()
        await store.close()


async def test_remote_cancellation_rereads_authority_and_stops_the_owned_target() -> (
    None
):
    clock = ManualClock(NOW)
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def target(_request: ExecutionRequest) -> None:
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()

    async with Notifications() as notifications:
        store = MemoryAutomationStore(clock=clock, notifications=notifications)
        worker_service = AutomationService(namespace="app", store=store, clock=clock)
        remote = AutomationService(namespace="app", store=store, clock=clock)
        async with AutomationEngine(
            worker_service,
            targets={"task": FunctionTarget(target, cancellation_is_final=True)},
            clock=clock,
        ) as engine:
            try:
                execution = await remote.execute_once(owner_id="owner", target="task")
                await started.wait()
                await remote.cancel_execution(
                    owner_id="owner", execution_id=execution.execution_id
                )
                await stopped.wait()
                await engine.wait_until_idle()
                assert (
                    await remote.get_execution(
                        owner_id="owner", execution_id=execution.execution_id
                    )
                ).status is ExecutionStatus.CANCELLED
            finally:
                release.set()
        await remote.close()
        await worker_service.close()
        await store.close()


@pytest.mark.parametrize("cancel_by", ["execution", "task_deletion"])
async def test_remote_cancellation_before_authorization_keeps_worker_available(
    cancel_by: str,
) -> None:
    clock = ManualClock(NOW)
    attempted, release = asyncio.Event(), asyncio.Event()
    started: list[str] = []

    class HeldStart(MemoryAutomationStore):
        async def authorize_start(
            self, claim: WorkItemClaim, *, execution_timeout: timedelta
        ) -> StartAuthorization:
            attempted.set()
            await release.wait()
            return await super().authorize_start(
                claim, execution_timeout=execution_timeout
            )

    async def target(request: ExecutionRequest) -> None:
        started.append(request.execution.execution_id)

    async with Notifications() as notifications:
        store = HeldStart(clock=clock, notifications=notifications)
        service = AutomationService(namespace="app", store=store, clock=clock)
        remote = AutomationService(namespace="app", store=store, clock=clock)
        async with AutomationEngine(
            service, targets={"task": FunctionTarget(target)}, clock=clock
        ) as engine:
            task = await remote.create_task(
                owner_id="owner",
                name="Remote task",
                target="task",
                schedule=OnceSchedule(at=NOW + timedelta(hours=1)),
            )
            cancelled = await remote.run_task_now(
                owner_id="owner", task_id=task.task_id
            )
            try:
                await attempted.wait()
                if cancel_by == "execution":
                    await remote.cancel_execution(
                        owner_id="owner", execution_id=cancelled.execution_id
                    )
                else:
                    await remote.delete_task(
                        owner_id="owner",
                        task_id=task.task_id,
                        expected_revision=task.revision,
                    )
            finally:
                release.set()
            await engine.wait_until_idle()
            await engine.check_ready()
            following = await remote.execute_once(owner_id="owner", target="task")
            await engine.wait_until_idle()
            assert started == [following.execution_id]
            assert (
                await remote.get_execution(
                    owner_id="owner", execution_id=cancelled.execution_id
                )
            ).status is ExecutionStatus.CANCELLED
        await remote.close()
        await service.close()
        await store.close()


async def test_cancellation_of_claimed_interrupt_expiry_keeps_worker_available() -> (
    None
):
    clock = ManualClock(NOW)
    attempted, release = asyncio.Event(), asyncio.Event()

    async def unused(_request: ExecutionRequest) -> None:
        raise AssertionError("Expiring an interrupted execution cannot start a target")

    class HeldExpiry(MemoryAutomationStore):
        async def finish_execution(
            self,
            claim: WorkItemClaim,
            *,
            status: ExecutionStatus,
            result: JsonValue | None = None,
            failure_code: str | None = None,
            failure_message: str | None = None,
        ) -> AutomationExecution:
            if claim.kind is WorkKind.EXPIRE_INTERRUPT:
                attempted.set()
                await release.wait()
            return await super().finish_execution(
                claim,
                status=status,
                result=result,
                failure_code=failure_code,
                failure_message=failure_message,
            )

    async with Notifications() as notifications:
        store = HeldExpiry(clock=clock, notifications=notifications)
        execution = _taskless_execution(execution_id="interrupted")
        await store.enqueue_execution(
            execution, occurrence_key="one", request_id=None, input_digest="one"
        )
        (claim,) = (
            await store.claim_work(
                "app",
                "first",
                limit=1,
                lease_duration=timedelta(minutes=1),
                global_concurrency=1,
            )
        ).claims
        await store.authorize_start(claim, execution_timeout=timedelta(seconds=5))
        await store.mark_interrupted(claim, interrupt_ids=("approval",))
        await clock.advance(timedelta(seconds=5))
        service = AutomationService(namespace="app", store=store, clock=clock)
        async with AutomationEngine(
            service, targets={"task": FunctionTarget(unused)}, clock=clock
        ) as engine:
            try:
                await attempted.wait()
                await store.cancel_execution(
                    "app",
                    execution.owner_id,
                    execution.execution_id,
                    request_id=None,
                    input_digest="cancel",
                )
            finally:
                release.set()
            await engine.wait_until_idle()
            await engine.check_ready()
            assert (
                await store.get_execution(
                    "app", execution.owner_id, execution.execution_id
                )
            ).status is ExecutionStatus.CANCELLED
        await service.close()
        await store.close()


async def test_observer_subscribes_before_its_read_and_wakes_without_advancing_time() -> (
    None
):
    clock = ObservedClock()
    async with Notifications() as notifications:
        store = ObservedStore(clock, notifications)
        async with Automation(namespace="app", store=store, clock=clock) as client:
            run = await client.for_owner("owner").run("remote")
            waiting = asyncio.create_task(run.wait(timeout=120))
            await store.reads.get()
            await store.cancel_execution(
                "app", "owner", run.id, request_id=None, input_digest="cancel"
            )
            assert await waiting is run
            assert run.status is ExecutionStatus.CANCELLED
            assert clock.now() == NOW
        await store.close()


async def test_remote_task_deletion_removes_the_local_wakeup_and_stale_due_ids_are_safe() -> (
    None
):
    clock = ManualClock(NOW)

    class ObservedScheduler(MemoryScheduler):
        def __init__(self) -> None:
            super().__init__(clock=clock)
            self.scheduled: asyncio.Queue[str] = asyncio.Queue(maxsize=16)
            self.removed: asyncio.Queue[str] = asyncio.Queue(maxsize=16)

        async def schedule_task(self, task_id: str, run_at: datetime) -> None:
            await super().schedule_task(task_id, run_at)
            self.scheduled.put_nowait(task_id)

        async def remove_task(self, task_id: str) -> None:
            await super().remove_task(task_id)
            self.removed.put_nowait(task_id)

    async def unused(_request: ExecutionRequest) -> None:
        raise AssertionError("deleted task must not execute")

    async with Notifications() as notifications:
        store = MemoryAutomationStore(clock=clock, notifications=notifications)
        scheduler = ObservedScheduler()
        service = AutomationService(
            namespace="app", store=store, scheduler=scheduler, clock=clock
        )
        remote = AutomationService(namespace="app", store=store, clock=clock)
        async with AutomationEngine(
            service, targets={"task": FunctionTarget(unused)}, clock=clock
        ) as engine:
            task = await remote.create_task(
                owner_id="owner",
                name="Scheduled",
                target="task",
                schedule=OnceSchedule(at=NOW + timedelta(hours=1)),
            )
            assert await scheduler.scheduled.get() == task.task_id
            await remote.delete_task(
                owner_id="owner", task_id=task.task_id, expected_revision=task.revision
            )
            assert await scheduler.removed.get() == task.task_id
            # A scheduler can have captured a due ID before the remote deletion.
            await scheduler.schedule_task(task.task_id, NOW)
            await scheduler.dispatch_due()
            await engine.check_ready()
        await remote.close()
        await service.close()
        await store.close()


async def test_claim_handoff_is_independent_of_notification_transport_latency() -> None:
    clock = ManualClock(NOW)
    sending, release = asyncio.Event(), asyncio.Event()

    class HeldPublisher(MemoryBackend):
        async def publish(self, payload: bytes) -> None:
            sending.set()
            await release.wait()
            await super().publish(payload)

    async with Notifications(backend=HeldPublisher()) as notifications:
        store = MemoryAutomationStore(clock=clock, notifications=notifications)
        expired = replace(
            _taskless_execution(execution_id="expired"),
            queued_at=NOW - timedelta(seconds=10),
            queue_deadline=NOW + timedelta(seconds=1),
        )
        ready = _taskless_execution(execution_id="ready", owner_id="another-owner")
        for execution in (expired, ready):
            await store.enqueue_execution(
                execution,
                occurrence_key=execution.execution_id,
                request_id=None,
                input_digest=execution.execution_id,
            )
        await clock.advance(timedelta(seconds=2))
        pending = asyncio.create_task(
            store.claim_work(
                "app",
                "worker",
                limit=1,
                lease_duration=timedelta(seconds=5),
                global_concurrency=2,
            )
        )
        try:
            await sending.wait()
            batch = await pending
            assert len(batch.claims) == 1
            started = await store.authorize_start(
                batch.claims[0], execution_timeout=timedelta(minutes=1)
            )
            assert started.execution.status is ExecutionStatus.RUNNING
            await store.finish_execution(
                batch.claims[0], status=ExecutionStatus.SUCCEEDED
            )
        finally:
            release.set()
            await store.close()


async def test_stopped_notification_service_ends_observation_with_a_store_error() -> (
    None
):
    clock = ObservedClock()
    async with Notifications() as notifications:
        store = ObservedStore(clock, notifications)
        async with Automation(namespace="app", store=store, clock=clock) as client:
            run = await client.for_owner("owner").run("remote")
            waiting = asyncio.create_task(run.wait(timeout=120))
            await store.reads.get()
            await notifications.aclose()
            with pytest.raises(AutomationStoreError):
                await waiting
            assert run.status is ExecutionStatus.QUEUED
        await store.close()


async def test_notification_bound_client_can_manage_remote_schedule_definitions() -> (
    None
):
    clock = ManualClock(NOW)
    async with Notifications() as notifications:
        store = MemoryAutomationStore(clock=clock, notifications=notifications)
        async with Automation(namespace="app", store=store, clock=clock) as client:
            owner = client.for_owner("owner")
            task = await owner.create_task(
                name="Remote schedule",
                target="remote",
                schedule=OnceSchedule(at=NOW + timedelta(hours=1)),
            )
            await task.pause(expected_revision=task.revision)
            await task.delete(expected_revision=task.revision)
        await store.close()
