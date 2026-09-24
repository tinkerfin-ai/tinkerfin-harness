from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tinkerfin_automation import (
    AttentionResolution,
    AutomationExecution,
    AutomationTask,
    ExecutionLimits,
    ExecutionOrigin,
    ExecutionStatus,
    MemoryAutomationStore,
    MisfirePolicy,
    OnceSchedule,
    QueueFullError,
    RequestConflictError,
    StartAlreadyAuthorizedError,
    TaskConflictError,
    TaskNotFoundError,
    TaskStatus,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore, WorkKind
from tinkerfin_contracts import RunIdentity

NOW = datetime(2026, 9, 9, 8, tzinfo=UTC)


def _task(*, task_id: str = "task-1", max_runs: int = 1) -> AutomationTask:
    return AutomationTask(
        task_id=task_id,
        namespace="app",
        owner_id="owner-1",
        execution_namespace="app",
        name="Daily summary",
        target="summary",
        input={"project_id": "project-1"},
        schedule=OnceSchedule(at=NOW + timedelta(hours=1)),
        status=TaskStatus.ENABLED,
        revision=1,
        next_run_at=NOW + timedelta(hours=1),
        misfire_policy=MisfirePolicy(),
        limits=ExecutionLimits(
            max_concurrent_runs=max_runs,
            max_queued_runs=1,
            execution_timeout=timedelta(minutes=30),
            queue_timeout=timedelta(hours=1),
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def _execution(
    task: AutomationTask,
    *,
    execution_id: str,
    queued_at: datetime = NOW,
    retry_of: str | None = None,
) -> AutomationExecution:
    return AutomationExecution(
        execution_id=execution_id,
        task_id=task.task_id,
        namespace=task.namespace,
        owner_id=task.owner_id,
        identity=RunIdentity(
            namespace=task.namespace,
            thread_id=f"thread-{execution_id}",
            run_id=f"run-{execution_id}",
        ),
        target=task.target,
        input=task.input,
        limits=task.limits,
        origin=ExecutionOrigin.RETRY if retry_of else ExecutionOrigin.MANUAL,
        status=ExecutionStatus.QUEUED,
        attempt=2 if retry_of else 1,
        retry_of=retry_of,
        scheduled_for=None,
        queued_at=queued_at,
        queue_deadline=queued_at + task.limits.queue_timeout,
        execution_started_at=None,
        execution_deadline=None,
        finished_at=None,
        failure_code=None,
        failure_message=None,
        result=None,
        interrupt_ids=(),
        start_authorized_at=None,
        start_token=None,
        created_at=queued_at,
        updated_at=queued_at,
    )


def _taskless_execution(
    *,
    execution_id: str,
    owner_id: str = "owner-1",
    limits: ExecutionLimits | None = None,
) -> AutomationExecution:
    task = replace(_task(), owner_id=owner_id)
    return replace(
        _execution(task, execution_id=execution_id),
        task_id=None,
        origin=ExecutionOrigin.ONE_TIME,
        limits=limits or task.limits,
    )


def test_memory_store_conforms_to_public_store_protocol() -> None:
    store: AutomationStore = MemoryAutomationStore(clock=ManualClock(NOW))
    assert store is not None


@pytest.mark.asyncio
async def test_task_commands_are_scoped_revisioned_and_idempotent(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    task = _task()

    created = await store.create_task(
        task, request_id="create-1", input_digest="digest-create"
    )
    repeated = await store.create_task(
        task, request_id="create-1", input_digest="digest-create"
    )
    assert repeated == created
    with pytest.raises(RequestConflictError):
        await store.create_task(task, request_id="create-1", input_digest="different")
    with pytest.raises(TaskNotFoundError):
        await store.get_task("app", "another-owner", task.task_id)

    updated = replace(task, name="Renamed", revision=2)
    with pytest.raises(TaskConflictError):
        await store.update_task(
            updated,
            expected_revision=0,
            request_id=None,
            input_digest="digest-update",
        )
    assert (
        await store.update_task(
            updated,
            expected_revision=1,
            request_id="update-1",
            input_digest="digest-update",
        )
    ).name == "Renamed"


@pytest.mark.asyncio
async def test_queue_limit_and_occurrence_identity_are_atomic(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    task = _task()
    await store.create_task(task, request_id=None, input_digest="task")
    first = _execution(task, execution_id="execution-1")
    assert (
        await store.enqueue_execution(
            first,
            occurrence_key="occurrence-1",
            request_id=None,
            input_digest="first",
        )
    ) == first
    duplicate = await store.enqueue_execution(
        replace(first, execution_id="other"),
        occurrence_key="occurrence-1",
        request_id=None,
        input_digest="duplicate",
    )
    assert duplicate.execution_id == first.execution_id
    with pytest.raises(QueueFullError):
        await store.enqueue_execution(
            _execution(task, execution_id="execution-2"),
            occurrence_key="occurrence-2",
            request_id=None,
            input_digest="second",
        )


@pytest.mark.asyncio
async def test_taskless_queue_limit_is_scoped_per_owner(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    limits = ExecutionLimits(max_concurrent_runs=1, max_queued_runs=1)
    first = _taskless_execution(execution_id="execution-1", limits=limits)
    await store.enqueue_execution(
        first,
        occurrence_key="occurrence-1",
        request_id=None,
        input_digest="first",
    )
    with pytest.raises(QueueFullError):
        await store.enqueue_execution(
            _taskless_execution(execution_id="execution-2", limits=limits),
            occurrence_key="occurrence-2",
            request_id=None,
            input_digest="second",
        )
    other_owner = _taskless_execution(
        execution_id="execution-3", owner_id="owner-2", limits=limits
    )
    assert (
        await store.enqueue_execution(
            other_owner,
            occurrence_key="occurrence-3",
            request_id=None,
            input_digest="third",
        )
    ).owner_id == "owner-2"


@pytest.mark.asyncio
async def test_taskless_concurrency_is_scoped_per_owner(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    limits = ExecutionLimits(max_concurrent_runs=1, max_queued_runs=2)
    executions = (
        _taskless_execution(execution_id="execution-1", limits=limits),
        _taskless_execution(execution_id="execution-2", limits=limits),
        _taskless_execution(
            execution_id="execution-3", owner_id="owner-2", limits=limits
        ),
    )
    for index, execution in enumerate(executions, start=1):
        await store.enqueue_execution(
            execution,
            occurrence_key=f"occurrence-{index}",
            request_id=None,
            input_digest=f"execution-{index}",
        )

    claims = await store.claim_work(
        "app",
        "worker",
        limit=3,
        lease_duration=timedelta(minutes=1),
        global_concurrency=3,
    )
    assert len(claims) == 2
    claimed = {
        (await store.get_scheduled_execution("app", claim.execution_id)).owner_id
        for claim in claims
    }
    assert claimed == {"owner-1", "owner-2"}

    claims_by_owner = {
        (await store.get_scheduled_execution("app", claim.execution_id)).owner_id: claim
        for claim in claims
    }
    owner_one_claim = claims_by_owner["owner-1"]
    await store.finish_execution(
        owner_one_claim, status=ExecutionStatus.SUCCEEDED, result={"ok": True}
    )
    (next_claim,) = await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=3,
    )
    assert (
        await store.get_scheduled_execution("app", next_claim.execution_id)
    ).owner_id == "owner-1"


@pytest.mark.asyncio
async def test_taskless_uncertainty_holds_owner_capacity_until_resolution(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    limits = ExecutionLimits(max_concurrent_runs=1, max_queued_runs=2)
    first = _taskless_execution(execution_id="execution-1", limits=limits)
    second = _taskless_execution(execution_id="execution-2", limits=limits)
    for index, execution in enumerate((first, second), start=1):
        await store.enqueue_execution(
            execution,
            occurrence_key=f"occurrence-{index}",
            request_id=None,
            input_digest=f"execution-{index}",
        )

    (first_claim,) = await store.claim_work(
        "app",
        "worker",
        limit=2,
        lease_duration=timedelta(minutes=1),
        global_concurrency=2,
    )
    pending_execution_id = next(
        execution.execution_id
        for execution in (first, second)
        if execution.execution_id != first_claim.execution_id
    )
    await store.authorize_start(first_claim, execution_timeout=timedelta(minutes=30))
    await store.finish_execution(
        first_claim,
        status=ExecutionStatus.NEEDS_ATTENTION,
        failure_code="host.unknown",
        failure_message="External state is unknown",
    )
    assert not await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=2,
    )

    await store.resolve_execution(
        "app",
        "owner-1",
        first_claim.execution_id,
        resolution=AttentionResolution.FAILED,
        request_id="resolve-taskless-1",
        input_digest="resolution",
        reason="Operator confirmed the external run stopped",
    )
    (second_claim,) = await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=2,
    )
    assert second_claim.execution_id == pending_execution_id


@pytest.mark.asyncio
async def test_claim_authorization_is_fenced_and_issued_once(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    task = _task()
    await store.create_task(task, request_id=None, input_digest="task")
    execution = _execution(task, execution_id="execution-1")
    await store.enqueue_execution(
        execution,
        occurrence_key="occurrence-1",
        request_id=None,
        input_digest="execution",
    )

    claims = await store.claim_work(
        "app",
        "worker-1",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=16,
    )
    assert len(claims) == 1
    authorization = await store.authorize_start(
        claims[0], execution_timeout=timedelta(minutes=30)
    )
    assert authorization.execution.status is ExecutionStatus.RUNNING
    with pytest.raises(StartAlreadyAuthorizedError):
        await store.authorize_start(claims[0], execution_timeout=timedelta(minutes=30))

    await clock.advance(timedelta(minutes=2))
    assert not await store.claim_work(
        "app",
        "worker-2",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=16,
    )
    uncertain = await store.get_execution("app", "owner-1", execution.execution_id)
    assert uncertain.status is ExecutionStatus.NEEDS_ATTENTION


@pytest.mark.asyncio
async def test_task_concurrency_releases_only_after_terminal_settlement(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    task = replace(_task(), limits=replace(_task().limits, max_queued_runs=2))
    await store.create_task(task, request_id=None, input_digest="task")
    for ordinal in (1, 2):
        await store.enqueue_execution(
            _execution(task, execution_id=f"execution-{ordinal}"),
            occurrence_key=f"occurrence-{ordinal}",
            request_id=None,
            input_digest=f"execution-{ordinal}",
        )

    first_claims = await store.claim_work(
        "app",
        "worker",
        limit=2,
        lease_duration=timedelta(minutes=1),
        global_concurrency=16,
    )
    assert len(first_claims) == 1
    first_execution_id = first_claims[0].execution_id
    await store.authorize_start(
        first_claims[0], execution_timeout=timedelta(minutes=30)
    )
    await store.finish_execution(
        first_claims[0], status=ExecutionStatus.SUCCEEDED, result={"ok": True}
    )

    second_claims = await store.claim_work(
        "app",
        "worker",
        limit=2,
        lease_duration=timedelta(minutes=1),
        global_concurrency=16,
    )
    assert len(second_claims) == 1
    assert second_claims[0].execution_id != first_execution_id
    assert {second_claims[0].execution_id, first_execution_id} == {
        "execution-1",
        "execution-2",
    }


@pytest.mark.asyncio
async def test_interrupt_waits_without_restart_and_expires_original_deadline(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    task = _task()
    execution = _execution(task, execution_id="execution-1")
    await store.create_task(task, request_id=None, input_digest="task")
    await store.enqueue_execution(
        execution,
        occurrence_key="occurrence-1",
        request_id=None,
        input_digest="execution",
    )
    (claim,) = await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(hours=1),
        global_concurrency=16,
    )
    await store.authorize_start(claim, execution_timeout=timedelta(minutes=30))
    interrupted = await store.mark_interrupted(claim, interrupt_ids=("interrupt-1",))
    assert interrupted.status is ExecutionStatus.INTERRUPTED

    await clock.advance(timedelta(minutes=30))
    (expiry,) = await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=16,
    )
    assert expiry.kind is WorkKind.EXPIRE_INTERRUPT
    timed_out = await store.finish_execution(
        expiry,
        status=ExecutionStatus.TIMED_OUT,
        failure_code="automation.execution_timeout",
        failure_message="Execution exceeded its deadline",
    )
    assert timed_out.status is ExecutionStatus.TIMED_OUT
    assert timed_out.identity == execution.identity


@pytest.mark.asyncio
async def test_uncertain_execution_requires_explicit_audited_resolution(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    task = _task()
    execution = _execution(task, execution_id="execution-1")
    await store.create_task(task, request_id=None, input_digest="task")
    await store.enqueue_execution(
        execution,
        occurrence_key="occurrence-1",
        request_id=None,
        input_digest="execution",
    )
    (claim,) = await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=16,
    )
    await store.authorize_start(claim, execution_timeout=timedelta(minutes=30))
    uncertain = await store.finish_execution(
        claim,
        status=ExecutionStatus.NEEDS_ATTENTION,
        failure_code="host.unknown",
        failure_message="External state is unknown",
    )
    assert uncertain.status is ExecutionStatus.NEEDS_ATTENTION

    resolved = await store.resolve_execution(
        "app",
        "owner-1",
        execution.execution_id,
        resolution=AttentionResolution.FAILED,
        request_id="resolve-1",
        input_digest="resolution",
        reason="Operator confirmed the external run stopped",
    )
    assert resolved.status is ExecutionStatus.FAILED
    assert resolved.failure_message == "Operator confirmed the external run stopped"


async def test_expired_unstarted_claim_releases_capacity_after_queue_timeout(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    execution = _taskless_execution(execution_id="expired-execution")
    await store.enqueue_execution(
        execution, occurrence_key="expired", request_id=None, input_digest="expired"
    )
    (claim,) = await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=1,
    )
    assert claim.execution_id == execution.execution_id
    await clock.advance(timedelta(hours=2))
    assert not await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=1,
    )
    assert (
        await store.get_execution("app", "owner-1", execution.execution_id)
    ).status is ExecutionStatus.TIMED_OUT
    later = replace(
        _taskless_execution(execution_id="later-execution"),
        queued_at=clock.now(),
        queue_deadline=clock.now() + timedelta(hours=1),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await store.enqueue_execution(
        later, occurrence_key="later", request_id=None, input_digest="later"
    )
    (next_claim,) = await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=1,
    )
    assert next_claim.execution_id == later.execution_id
