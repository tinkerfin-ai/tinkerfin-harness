"""Source commits, maintenance deadlines, and capacity-aware candidate selection."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from test_store_contract import _execution, _task, _taskless_execution

from tinkerfin_automation import ExecutionLimits, ExecutionStatus, TaskStatus
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import (
    AutomationStore,
    MaterializationResult,
    ScheduledExecution,
    WorkClaimBatch,
)
from tinkerfin_notifications import Notification, Notifications, NotificationScope


async def claim(
    store: AutomationStore, *, limit: int = 1, lease: int = 60, capacity: int = 2
) -> WorkClaimBatch:
    return await store.claim_work(
        "app",
        "worker",
        limit=limit,
        lease_duration=timedelta(seconds=lease),
        global_concurrency=capacity,
    )


async def enqueue(
    store: AutomationStore,
    execution_id: str,
    *,
    owner: str = "owner-1",
    limits: ExecutionLimits | None = None,
) -> None:
    execution = _taskless_execution(
        execution_id=execution_id, owner_id=owner, limits=limits
    )
    execution = replace(
        execution, queue_deadline=execution.queued_at + execution.limits.queue_timeout
    )
    await store.enqueue_execution(
        execution,
        occurrence_key=execution_id,
        request_id=None,
        input_digest=execution_id,
    )


async def test_task_materialization_and_deletion_emit_owner_scoped_changes(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
    notification_service: Notifications,
) -> None:
    store, clock = notifying_store_with_clock
    scope = NotificationScope("app", "owner-1")
    async with notification_service.subscribe(scope=scope) as changes:
        task = _task()
        await store.create_task(task, request_id="create", input_digest="create")
        created = await anext(changes)
        assert isinstance(created, Notification)
        assert (created.topic, created.key) == ("automation.task.changed", task.task_id)
        paused = replace(task, status=TaskStatus.PAUSED, revision=2, next_run_at=None)
        await store.update_task(
            paused, expected_revision=1, request_id=None, input_digest="pause"
        )
        assert await anext(changes) == created
        task = replace(task, revision=3)
        await store.update_task(
            task, expected_revision=2, request_id=None, input_digest="enable"
        )
        assert await anext(changes) == created
        assert task.next_run_at is not None
        await clock.advance(timedelta(hours=1))
        execution = _execution(task, execution_id="scheduled", queued_at=clock.now())
        result: MaterializationResult = await store.materialize_task(
            replace(task, next_run_at=None),
            expected_next_run_at=task.next_run_at,
            executions=(ScheduledExecution(execution, "occurrence"),),
        )
        assert result.executions == (execution,)
        materialized = [await anext(changes), await anext(changes)]
        assert {
            (item.topic, item.key)
            for item in materialized
            if isinstance(item, Notification)
        } == {
            ("automation.task.changed", task.task_id),
            ("automation.execution.changed", execution.execution_id),
        }
        await store.delete_task(
            "app",
            "owner-1",
            task.task_id,
            expected_revision=3,
            request_id=None,
            input_digest="delete",
        )
        deleted = [await anext(changes), await anext(changes)]
        assert {
            (item.topic, item.key) for item in deleted if isinstance(item, Notification)
        } == {
            ("automation.task.changed", task.task_id),
            ("automation.execution.changed", execution.execution_id),
        }
        assert (
            await store.get_execution("app", "owner-1", "scheduled")
        ).status is ExecutionStatus.CANCELLED


async def test_execution_changes_and_renewal_repair_a_lost_cancellation_hint(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
    notification_service: Notifications,
) -> None:
    store, _ = notifying_store_with_clock
    async with notification_service.subscribe(
        scope=NotificationScope("app", "owner-1"),
        topics={"automation.execution.changed"},
    ) as changes:
        await enqueue(store, "run")
        queued = await anext(changes)
        assert isinstance(queued, Notification)
        assert queued.key == "run"
        owned = (await claim(store)).claims[0]
        await store.authorize_start(owned, execution_timeout=timedelta(minutes=5))
        assert await anext(changes) == queued
        await store.cancel_execution(
            "app", "owner-1", "run", request_id=None, input_digest="cancel"
        )
        # Cancellation remains authoritative even when the receiver discards its hint.
        assert await anext(changes) == queued
        renewal = await store.renew_claim(owned, lease_duration=timedelta(minutes=1))
        assert renewal.cancellation_requested
        assert renewal.claim.fence == owned.fence
        await store.finish_execution(renewal.claim, status=ExecutionStatus.CANCELLED)
        assert await anext(changes) == queued


async def test_zero_capacity_maintains_pending_timeouts_without_claiming_new_work(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
    notification_service: Notifications,
) -> None:
    store, clock = notifying_store_with_clock
    limits = ExecutionLimits(max_queued_runs=3, queue_timeout=timedelta(seconds=5))
    await enqueue(store, "active", limits=limits)
    owned = (await claim(store, capacity=1)).claims[0]
    await store.authorize_start(owned, execution_timeout=timedelta(minutes=5))
    await enqueue(store, "pending", limits=limits)
    waiting = await claim(store, limit=0, capacity=1)
    assert waiting.claims == ()
    assert waiting.next_check_after_seconds == 5.0
    async with notification_service.subscribe(
        scope=NotificationScope("app"), key="pending"
    ) as changes:
        await clock.advance(timedelta(seconds=5))
        maintained = await claim(store, limit=0, capacity=1)
        assert maintained.claims == ()
        notice = await anext(changes)
        assert (
            isinstance(notice, Notification)
            and notice.topic == "automation.execution.changed"
        )
    assert (
        await store.get_execution("app", "owner-1", "pending")
    ).status is ExecutionStatus.TIMED_OUT
    assert (
        await store.get_execution("app", "owner-1", "active")
    ).status is ExecutionStatus.RUNNING
    await store.finish_execution(owned, status=ExecutionStatus.SUCCEEDED)


async def test_zero_capacity_settles_due_interrupts_and_releases_capacity_once(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = notifying_store_with_clock
    await enqueue(store, "interrupted")
    owned = (await claim(store, capacity=1)).claims[0]
    await store.authorize_start(owned, execution_timeout=timedelta(seconds=5))
    await store.mark_interrupted(owned, interrupt_ids=("review",))
    waiting = await claim(store, limit=0, capacity=1)
    assert waiting.claims == () and waiting.next_check_after_seconds == 5.0
    await clock.advance(timedelta(seconds=5))
    assert (await claim(store, limit=0, capacity=1)).claims == ()
    assert (
        await store.get_execution("app", "owner-1", "interrupted")
    ).status is ExecutionStatus.TIMED_OUT
    assert (await claim(store, limit=0, capacity=1)).claims == ()
    await enqueue(store, "later")
    assert len((await claim(store, capacity=1)).claims) == 1


async def test_capacity_blocked_prefix_cannot_hide_an_independent_ready_execution(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = notifying_store_with_clock
    limits = ExecutionLimits(max_concurrent_runs=1, max_queued_runs=10)
    await enqueue(store, "active", limits=limits)
    owned = (await claim(store)).claims[0]
    await store.authorize_start(owned, execution_timeout=timedelta(minutes=5))
    for index in range(4):
        await enqueue(store, f"blocked-{index}", limits=limits)
    other = replace(
        _taskless_execution(
            execution_id="independent", owner_id="owner-2", limits=limits
        ),
        queued_at=clock.now() + timedelta(microseconds=1),
    )
    await store.enqueue_execution(
        other, occurrence_key="independent", request_id=None, input_digest="independent"
    )
    await clock.advance(timedelta(seconds=1))
    accepted = await claim(store)
    assert [item.execution_id for item in accepted.claims] == ["independent"]
    await store.finish_execution(owned, status=ExecutionStatus.SUCCEEDED)
    await store.finish_execution(accepted.claims[0], status=ExecutionStatus.SUCCEEDED)


async def test_claimed_queue_deadline_does_not_trigger_unauthorized_maintenance(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = notifying_store_with_clock
    await enqueue(
        store, "reserved", limits=ExecutionLimits(queue_timeout=timedelta(seconds=2))
    )
    original = (await claim(store, lease=5)).claims[0]
    assert (await claim(store, limit=0)).next_check_after_seconds == 5.0
    await clock.advance(timedelta(seconds=3))
    assert (await claim(store, limit=0)).next_check_after_seconds == 2.0
    assert (
        await store.get_execution("app", "owner-1", "reserved")
    ).status is ExecutionStatus.QUEUED
    await clock.advance(timedelta(seconds=2))
    assert (await claim(store, limit=0)).claims == ()
    assert (
        await store.get_execution("app", "owner-1", "reserved")
    ).status is ExecutionStatus.TIMED_OUT
    assert original.fence == 1


async def test_future_work_deadline_is_returned_without_empty_polling(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = notifying_store_with_clock
    execution = replace(
        _taskless_execution(execution_id="future"),
        queued_at=clock.now() + timedelta(seconds=7),
    )
    await store.enqueue_execution(
        execution, occurrence_key="future", request_id=None, input_digest="future"
    )
    batch = await claim(store)
    assert batch.claims == ()
    assert batch.next_check_after_seconds == 7.0
