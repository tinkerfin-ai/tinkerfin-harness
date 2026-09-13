from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tinkerfin_automation import (
    AttentionResolution,
    AutomationEngine,
    AutomationLifecycleError,
    AutomationService,
    AutomationStoreError,
    ExecutionFailure,
    ExecutionInterrupted,
    ExecutionRequest,
    ExecutionStatus,
    FunctionTarget,
    MemoryAutomationStore,
    OnceSchedule,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import WorkItemClaim
from tinkerfin_automation.targets import normalize_target_result
from tinkerfin_native_stream import NativeRuntimeInterrupt

NOW = datetime(2026, 9, 9, 8, tzinfo=UTC)


class _RenewalStore(MemoryAutomationStore):
    def __init__(self, *, clock: ManualClock) -> None:
        super().__init__(clock=clock)
        self.renewed = asyncio.Event()

    async def renew_claim(
        self, claim: WorkItemClaim, *, lease_duration: timedelta
    ) -> WorkItemClaim:
        renewed = await super().renew_claim(claim, lease_duration=lease_duration)
        self.renewed.set()
        return renewed


class _EngineSetupStore(MemoryAutomationStore):
    def __init__(self, *, clock: ManualClock, events: list[str]) -> None:
        super().__init__(clock=clock)
        self._events = events
        self.setup_attempts = 0
        self.fail_first_setup = False

    async def setup(self) -> None:
        self.setup_attempts += 1
        self._events.append("store.setup")
        if self.fail_first_setup and self.setup_attempts == 1:
            raise AutomationStoreError("setup failed")
        await super().setup()

    async def list_scheduled_tasks(self, namespace, *, limit, cursor):
        self._events.append("store.list_scheduled_tasks")
        return await super().list_scheduled_tasks(namespace, limit=limit, cursor=cursor)


class _EngineSetupScheduler:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.start_calls = 0

    async def start(self, on_task_due) -> None:
        self.start_calls += 1
        self._events.append("scheduler.start")
        self.on_task_due = on_task_due

    async def schedule_task(self, task_id: str, run_at: datetime) -> None:
        self._events.append("scheduler.schedule_task")

    async def remove_task(self, task_id: str) -> None:
        self._events.append("scheduler.remove_task")

    async def close(self) -> None:
        self._events.append("scheduler.close")


class _BlockingSetupStore(MemoryAutomationStore):
    def __init__(self, *, clock: ManualClock) -> None:
        super().__init__(clock=clock)
        self.setup_started = asyncio.Event()
        self.setup_release = asyncio.Event()

    async def setup(self) -> None:
        self.setup_started.set()
        await self.setup_release.wait()
        await super().setup()


async def _task_and_execution(
    service: AutomationService,
    *,
    target: str = "target",
):
    task = await service.create_task(
        owner_id="owner-1",
        name="Task",
        schedule=OnceSchedule(at=NOW + timedelta(days=1)),
        target=target,
    )
    execution = await service.run_task_now(owner_id="owner-1", task_id=task.task_id)
    return task, execution


async def _status(service: AutomationService, execution_id: str) -> ExecutionStatus:
    return (
        await service.get_execution(owner_id="owner-1", execution_id=execution_id)
    ).status


def test_runtime_interrupt_shape_is_normalized_without_decision() -> None:
    outcome = normalize_target_result(
        {
            "messages": [],
            "__interrupt__": [
                NativeRuntimeInterrupt(id="interrupt-1", value={"action": "write"})
            ],
        }
    )
    assert outcome == ExecutionInterrupted(("interrupt-1",))


@pytest.mark.asyncio
async def test_engine_prepares_store_before_scheduler_and_allows_setup_retry() -> None:
    clock = ManualClock(NOW)
    events: list[str] = []
    store = _EngineSetupStore(clock=clock, events=events)
    store.fail_first_setup = True
    scheduler = _EngineSetupScheduler(events)
    service = AutomationService(
        namespace="app", store=store, scheduler=scheduler, clock=clock
    )

    async def execute(_request):
        return {"ok": True}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        clock=clock,
    )
    with pytest.raises(AutomationStoreError, match="setup failed"):
        await engine.start()
    assert events == ["store.setup"]
    assert scheduler.start_calls == 0

    await engine.start()
    try:
        assert events[:4] == [
            "store.setup",
            "store.setup",
            "scheduler.start",
            "store.list_scheduled_tasks",
        ]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_engine_start_cancellation_prevents_scheduler_start() -> None:
    clock = ManualClock(NOW)
    events: list[str] = []
    store = _BlockingSetupStore(clock=clock)
    scheduler = _EngineSetupScheduler(events)
    service = AutomationService(
        namespace="app", store=store, scheduler=scheduler, clock=clock
    )

    async def execute(_request):
        return {"ok": True}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        clock=clock,
    )
    starting = asyncio.create_task(engine.start())
    await store.setup_started.wait()
    starting.cancel("host startup stopped")
    with pytest.raises(asyncio.CancelledError) as caught:
        await starting
    assert caught.value.args == ("host startup stopped",)
    assert scheduler.start_calls == 0

    store.setup_release.set()
    await engine.start()
    await engine.close()


@pytest.mark.asyncio
async def test_engine_is_not_observably_running_while_store_setup_waits() -> None:
    clock = ManualClock(NOW)
    events: list[str] = []
    store = _BlockingSetupStore(clock=clock)
    scheduler = _EngineSetupScheduler(events)
    service = AutomationService(
        namespace="app", store=store, scheduler=scheduler, clock=clock
    )

    async def execute(_request):
        return {"ok": True}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        clock=clock,
    )
    starting = asyncio.create_task(engine.start())
    await store.setup_started.wait()
    with pytest.raises(AutomationLifecycleError, match="not running"):
        await engine.run_ready()
    with pytest.raises(AutomationLifecycleError, match="not running"):
        await engine.wait_until_idle()
    assert scheduler.start_calls == 0

    store.setup_release.set()
    await starting
    try:
        assert scheduler.start_calls == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_engine_close_during_setup_prevents_remaining_startup() -> None:
    clock = ManualClock(NOW)
    events: list[str] = []
    store = _BlockingSetupStore(clock=clock)
    scheduler = _EngineSetupScheduler(events)
    service = AutomationService(
        namespace="app", store=store, scheduler=scheduler, clock=clock
    )

    async def execute(_request):
        return {"ok": True}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        clock=clock,
    )
    starting = asyncio.create_task(engine.start())
    await store.setup_started.wait()
    await engine.close()
    store.setup_release.set()

    with pytest.raises(AutomationLifecycleError, match="closed during startup"):
        await starting
    assert scheduler.start_calls == 0
    assert events == ["scheduler.close"]


@pytest.mark.asyncio
async def test_engine_executes_success_and_failure_outcomes() -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    results = [
        {"answer": 42},
        ExecutionFailure(code="host.denied", message="Host denied execution"),
    ]

    async def execute(_request):
        return results.pop(0)

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        clock=clock,
    )
    await engine.start()
    try:
        _, first = await _task_and_execution(service)
        await engine.run_ready()
        await engine.wait_until_idle()
        assert await _status(service, first.execution_id) is ExecutionStatus.SUCCEEDED

        _, second = await _task_and_execution(service)
        await engine.run_ready()
        await engine.wait_until_idle()
        failed = await service.get_execution(
            owner_id="owner-1", execution_id=second.execution_id
        )
        assert failed.status is ExecutionStatus.FAILED
        assert failed.failure_code == "host.denied"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_engine_executes_taskless_input_with_generated_runtime_identity() -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    observed: list[ExecutionRequest] = []

    async def execute(request):
        observed.append(request)
        return {"answer": 42}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        clock=clock,
    )
    await engine.start()
    try:
        execution = await service.execute_once(
            owner_id="owner-1",
            target="target",
            input={"question": "meaning"},
        )
        await engine.run_ready()
        await engine.wait_until_idle()

        completed = await service.get_execution(
            owner_id="owner-1", execution_id=execution.execution_id
        )
        assert completed.status is ExecutionStatus.SUCCEEDED
        assert completed.task_id is None
        assert observed[0].execution.execution_id == execution.execution_id
        assert observed[0].execution.identity == execution.identity
        assert observed[0].execution.input == {"question": "meaning"}
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_interrupt_none_remains_unfinished_until_original_deadline() -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    callbacks: list[tuple[str, ...]] = []

    async def execute(_request):
        return ExecutionInterrupted(("interrupt-1",))

    async def on_interrupt(execution):
        callbacks.append(execution.interrupt_ids)
        return None

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        on_interrupt=on_interrupt,
        lease_duration=timedelta(hours=1),
        clock=clock,
    )
    await engine.start()
    try:
        _, execution = await _task_and_execution(service)
        await engine.run_ready()
        await engine.wait_until_idle()
        assert (
            await _status(service, execution.execution_id)
            is ExecutionStatus.INTERRUPTED
        )
        assert callbacks == [("interrupt-1",)]

        await clock.advance(timedelta(minutes=30))
        await engine.run_ready()
        await engine.wait_until_idle()
        assert (
            await _status(service, execution.execution_id) is ExecutionStatus.TIMED_OUT
        )
        assert callbacks == [("interrupt-1",)]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_taskless_interrupt_exposes_no_task_and_cancel_releases_owner_capacity() -> (
    None
):
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    interrupted_task_ids: list[str | None] = []
    calls = 0

    async def execute(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ExecutionInterrupted(("interrupt-1",))
        return {"call": calls}

    async def on_interrupt(execution):
        interrupted_task_ids.append(execution.task_id)
        return None

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        on_interrupt=on_interrupt,
        clock=clock,
    )
    await engine.start()
    try:
        first = await service.execute_once(owner_id="owner-1", target="target")
        await engine.run_ready()
        await engine.wait_until_idle()
        assert await _status(service, first.execution_id) is ExecutionStatus.INTERRUPTED
        assert interrupted_task_ids == [None]

        second = await service.execute_once(owner_id="owner-1", target="target")
        assert await engine.run_ready() == 0
        await service.cancel_execution(
            owner_id="owner-1", execution_id=first.execution_id
        )
        await engine.run_ready()
        await engine.wait_until_idle()
        assert await _status(service, second.execution_id) is ExecutionStatus.SUCCEEDED
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_mode", ["failure", "exception"])
async def test_interrupt_callback_can_fail_but_never_resume(callback_mode: str) -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)

    async def execute(_request):
        return ExecutionInterrupted(("interrupt-1",))

    async def on_interrupt(_execution):
        if callback_mode == "exception":
            raise RuntimeError("private callback detail")
        return ExecutionFailure(code="policy.blocked", message="Host blocked execution")

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        on_interrupt=on_interrupt,
        clock=clock,
    )
    await engine.start()
    try:
        _, execution = await _task_and_execution(service)
        await engine.run_ready()
        await engine.wait_until_idle()
        result = await service.get_execution(
            owner_id="owner-1", execution_id=execution.execution_id
        )
        assert result.status is ExecutionStatus.FAILED
        assert result.failure_code == (
            "policy.blocked"
            if callback_mode == "failure"
            else "automation.interrupt_callback_failed"
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_interrupt_callback_timeout_cancels_callback_and_times_out() -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def execute(_request):
        return ExecutionInterrupted(("interrupt-1",))

    async def on_interrupt(_execution):
        callback_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            callback_cancelled.set()

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        on_interrupt=on_interrupt,
        lease_duration=timedelta(hours=1),
        clock=clock,
    )
    await engine.start()
    try:
        _, execution = await _task_and_execution(service)
        await engine.run_ready()
        await callback_started.wait()
        await clock.advance(timedelta(minutes=30))
        await engine.wait_until_idle()
        assert callback_cancelled.is_set()
        assert (
            await _status(service, execution.execution_id) is ExecutionStatus.TIMED_OUT
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancellation_is_final", "expected"),
    [
        (True, ExecutionStatus.TIMED_OUT),
        (False, ExecutionStatus.NEEDS_ATTENTION),
    ],
)
async def test_execution_timeout_preserves_unconfirmed_external_capacity(
    cancellation_is_final: bool, expected: ExecutionStatus
) -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(_request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    engine = AutomationEngine(
        service,
        targets={
            "target": FunctionTarget(
                execute, cancellation_is_final=cancellation_is_final
            )
        },
        lease_duration=timedelta(hours=1),
        clock=clock,
    )
    await engine.start()
    try:
        _, execution = await _task_and_execution(service)
        await engine.run_ready()
        await started.wait()
        await clock.advance(timedelta(minutes=30))
        await engine.wait_until_idle()
        assert cancelled.is_set()
        assert await _status(service, execution.execution_id) is expected
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_running_cancel_propagates_and_settles_when_target_guarantees_stop() -> (
    None
):
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(_request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        clock=clock,
    )
    await engine.start()
    try:
        _, execution = await _task_and_execution(service)
        await engine.run_ready()
        await started.wait()
        requested = await service.cancel_execution(
            owner_id="owner-1",
            execution_id=execution.execution_id,
            request_id="cancel-1",
        )
        assert requested.status is ExecutionStatus.CANCEL_REQUESTED
        await engine.wait_until_idle()
        assert cancelled.is_set()
        assert (
            await _status(service, execution.execution_id) is ExecutionStatus.CANCELLED
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_per_task_concurrency_never_starts_two_targets_together() -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    gates = [asyncio.Event(), asyncio.Event()]
    first_started = asyncio.Event()
    calls = 0
    active = 0
    max_active = 0

    async def execute(_request):
        nonlocal active, calls, max_active
        index = calls
        calls += 1
        active += 1
        max_active = max(max_active, active)
        first_started.set()
        await gates[index].wait()
        active -= 1
        return {"index": index}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        global_concurrency=16,
        clock=clock,
    )
    await engine.start()
    try:
        task, _ = await _task_and_execution(service)
        await service.run_task_now(owner_id="owner-1", task_id=task.task_id)
        await engine.run_ready()
        await first_started.wait()
        assert calls == 1
        gates[0].set()
        gates[1].set()
        await engine.wait_until_idle()
        assert calls == 2
        assert max_active == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_unconfirmed_timeout_holds_capacity_until_audited_resolution() -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    first_started = asyncio.Event()
    calls = 0

    async def execute(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await asyncio.Event().wait()
        return {"call": calls}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=False)},
        lease_duration=timedelta(hours=1),
        clock=clock,
    )
    await engine.start()
    try:
        task, first = await _task_and_execution(service)
        await engine.run_ready()
        await first_started.wait()
        await clock.advance(timedelta(minutes=30))
        await engine.wait_until_idle()
        assert (
            await _status(service, first.execution_id)
            is ExecutionStatus.NEEDS_ATTENTION
        )

        second = await service.run_task_now(owner_id="owner-1", task_id=task.task_id)
        assert await engine.run_ready() == 0
        assert calls == 1

        await service.resolve_execution(
            owner_id="owner-1",
            execution_id=first.execution_id,
            resolution=AttentionResolution.FAILED,
            reason="Operator confirmed the external work stopped",
            request_id="resolve-1",
        )
        await engine.run_ready()
        await engine.wait_until_idle()
        assert calls == 2
        assert await _status(service, second.execution_id) is ExecutionStatus.SUCCEEDED
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_engine_close_drains_owned_target_without_orphan_tasks() -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_request):
        started.set()
        await release.wait()
        return {"done": True}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        clock=clock,
    )
    await engine.start()
    _, execution = await _task_and_execution(service)
    await engine.run_ready()
    await started.wait()

    closing = asyncio.create_task(engine.close())
    release.set()
    await closing

    assert await _status(service, execution.execution_id) is ExecutionStatus.SUCCEEDED
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("tinkerfin-automation-")
    ]


@pytest.mark.asyncio
async def test_engine_renews_fenced_claim_during_long_execution() -> None:
    clock = ManualClock(NOW)
    store = _RenewalStore(clock=clock)
    service = AutomationService(namespace="app", store=store, clock=clock)
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_request):
        started.set()
        await release.wait()
        return {"done": True}

    engine = AutomationEngine(
        service,
        targets={"target": FunctionTarget(execute, cancellation_is_final=True)},
        lease_duration=timedelta(minutes=1),
        clock=clock,
    )
    await engine.start()
    try:
        _, execution = await _task_and_execution(service)
        await engine.run_ready()
        await started.wait()
        await clock.advance(timedelta(seconds=31))
        await store.renewed.wait()
        await clock.advance(timedelta(seconds=40))
        release.set()
        await engine.wait_until_idle()
        assert (
            await _status(service, execution.execution_id) is ExecutionStatus.SUCCEEDED
        )
    finally:
        await engine.close()
