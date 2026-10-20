from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from ag_ui.core import BaseEvent

from tinkerfin import AgUiResumeReceipt, AgUiResumeResponse
from tinkerfin_contracts import (
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    ThreadIdentity,
)
from tinkerfin_messaging import MessageChannel, Messaging, RunNotFound
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_notifications import Notification
from tinkerfin_studio.conversation.coordinator import ConversationTraceCoordinator
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.models import ConversationRunRegistration
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_tracing import Tracer


async def _messaging_channel() -> tuple[
    Messaging,
    MessageChannel[BaseEvent, BaseEvent],
]:
    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(
        name="studio-conversation-agui",
        codec=AgUiCodec(),
    )
    return messaging, channel


class _MissingRunBarrierChannel:
    """在 missing 观测后暂停，让 owner preflight 与回收删除精确交错"""

    def __init__(self) -> None:
        self.checked = asyncio.Event()
        self.release = asyncio.Event()

    async def get_run_status(self, *, identity: RunIdentity) -> str:
        self.checked.set()
        await self.release.wait()
        raise RunNotFound(identity=identity)


async def _setup_run(database, *, thread_id: str, run_id: str):
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            project_id="project-1",
            user_id=1,
            thread_id=thread_id,
            title="协调器测试",
            model_id="model-main",
        )
        registration = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=run_id,
            parent_run_id=None,
            model_id="model-main",
            input_json={"runId": run_id},
        )
        assert await repository.activate_run_registration(
            thread_pk=thread.id, run_pk=registration.id, run_id=run_id
        )
        await repository.commit()
        thread_pk = thread.id
    context = RunSourceContext(
        identity=RunIdentity(namespace="ns_1", thread_id=thread_id, run_id=run_id),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"id": "user-1", "role": "user", "content": "执行任务"}]},
        config={},
    )
    trace_session = await tracer.open_run(context)
    now = datetime.now(UTC)
    await trace_session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await trace_session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        )
    )
    return tracer, context, trace_session, thread_pk


async def _thread(database, thread_pk: int):
    async with database.session() as session:
        return await ConversationRepository(session).get_thread_by_pk(thread_pk)


async def test_delayed_same_run_snapshot_cannot_restore_stale_running_status(
    notifications,
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独立协调器较晚提交的旧快照不能覆盖相同事件前缀的失活观测"""
    tracer, context, trace_session, thread_pk = await _setup_run(
        database, thread_id="thread-late-snapshot", run_id="run-late-snapshot"
    )
    old_view = await tracer.get(
        context.identity.thread, projections=("studio.conversation.failures",)
    )
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    original_get = tracer.get

    async def delayed_get(
        identity: ThreadIdentity,
        *,
        head_run_id: str | None = None,
        projections: tuple[str, ...] = (),
    ):
        if not read_started.is_set():
            read_started.set()
            await release_read.wait()
            return old_view
        return await original_get(
            identity,
            head_run_id=head_run_id,
            projections=("studio.conversation.failures",),
        )

    monkeypatch.setattr(tracer, "get", delayed_get)
    messaging, channel = await _messaging_channel()
    delayed = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
        notifications=notifications,
    )
    current = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
        notifications=notifications,
    )
    pending = asyncio.create_task(
        delayed.reconcile(thread_pk=thread_pk, identity=context.identity)
    )
    try:
        await read_started.wait()
        await trace_session.aclose()
        await current.reconcile(thread_pk=thread_pk, identity=context.identity)
        closed = await _thread(database, thread_pk)
        assert closed is not None and closed.status == "error"
        release_read.set()
        await pending
        settled = await _thread(database, thread_pk)
        assert settled is not None and settled.status == "error"
    finally:
        release_read.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await current.aclose()
        await delayed.aclose()
        await messaging.aclose()


async def test_owner_preflight_cas_fences_a_stale_recovery_delete(
    notifications,
    database,
    fixed_utc_time: datetime,
) -> None:
    """missing 检查后的 owner 激活必须让延迟删除 CAS 失效"""

    identity = RunIdentity(
        namespace="ns_1", thread_id="thread-preflight-race", run_id="run-preflight-race"
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            project_id="project-1",
            user_id=1,
            thread_id=identity.thread_id,
            title="激活竞态",
            model_id="model-main",
        )
        registration = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=identity.run_id,
            parent_run_id=None,
            model_id="model-main",
            input_json={"runId": identity.run_id},
        )
        registration.created_at = fixed_utc_time.replace(tzinfo=None) - timedelta(
            minutes=1
        )
        thread.last_run_id = identity.run_id
        await repository.commit()
        thread_pk = thread.id
        run_pk = registration.id

    barrier = _MissingRunBarrierChannel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=Tracer(
            projections=(ConversationFailureProjection(),),
        ),
        conversation_channel=cast(
            MessageChannel[BaseEvent, BaseEvent],
            barrier,
        ),
        notifications=notifications,
    )
    recovery = asyncio.create_task(coordinator.recover(thread_pk=thread_pk))
    await barrier.checked.wait()
    async with database.session() as session:
        repository = ConversationRepository(session)
        assert await repository.activate_run_registration(
            thread_pk=thread_pk,
            run_pk=run_pk,
            run_id=identity.run_id,
        )
        await repository.commit()
    barrier.release.set()
    await recovery

    async with database.session() as session:
        repository = ConversationRepository(session)
        run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
        assert run is not None
        assert run.status == "starting"

    await coordinator.aclose()


async def test_saved_receipt_settles_public_responses_idempotently(
    notifications, database, monkeypatch
) -> None:
    """业务认领仅依赖已保存回执，重复交付保持相同结算结果"""

    tracer, context, trace_session, thread_pk = await _setup_run(
        database, thread_id="thread-receipt", run_id="run-receipt"
    )
    receipt = AgUiResumeReceipt(
        identity=context.identity,
        parent_run_id="run-review",
        receipt_id="receipt-id",
        responses=(
            AgUiResumeResponse("public-approve", "resolved"),
            AgUiResumeResponse("public-cancel", "cancelled"),
        ),
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        await repository.create_interrupt_claims(
            thread_pk=thread_pk,
            source_run_id="run-review",
            claimed_run_id=context.identity.run_id,
            interrupt_ids=tuple(
                response.interrupt_id for response in receipt.responses
            ),
        )
        await repository.commit()
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
        notifications=notifications,
    )
    published: list[Notification] = []
    publish = notifications.publish

    async def capture(notification: Notification) -> None:
        if notification.topic == "studio.conversation.interactions.changed":
            async with database.session() as session:
                saved = await ConversationRepository(session).list_claims_for_update(
                    thread_pk=thread_pk,
                    interrupt_ids=frozenset({"public-approve", "public-cancel"}),
                )
                assert all(claim.resolution_id == receipt.receipt_id for claim in saved)
            published.append(notification)
        await publish(notification)

    monkeypatch.setattr(notifications, "publish", capture)
    try:
        await coordinator.settle_resume(thread_pk=thread_pk, receipt=receipt)
        await coordinator.settle_resume(thread_pk=thread_pk, receipt=receipt)
        async with database.session() as session:
            claims = await ConversationRepository(session).list_claims_for_update(
                thread_pk=thread_pk,
                interrupt_ids=frozenset(
                    response.interrupt_id for response in receipt.responses
                ),
            )
        assert {
            claim.interrupt_id: (claim.status, claim.resolution_id) for claim in claims
        } == {
            "public-approve": ("resolved", "receipt-id"),
            "public-cancel": ("cancelled", "receipt-id"),
        }
        assert len(published) == 2
        assert all(notice.key == context.identity.thread_id for notice in published)
        assert all(
            dict(notice.details) == {"submissionRunId": context.identity.run_id}
            for notice in published
        )
    finally:
        await coordinator.aclose()
        await messaging.aclose()
        await trace_session.aclose()


@pytest.mark.parametrize("conflict", ["run", "receipt", "status"])
async def test_saved_receipt_cannot_replace_a_different_claim_resolution(
    notifications, database, conflict: str
) -> None:
    """拒绝身份、回执或审批状态不一致的重复结算"""

    tracer, context, trace_session, thread_pk = await _setup_run(
        database, thread_id="thread-receipt-conflict", run_id="run-receipt-conflict"
    )
    response = AgUiResumeResponse("public-review", "resolved")
    receipt = AgUiResumeReceipt(
        identity=context.identity,
        parent_run_id="run-review",
        receipt_id="receipt-id",
        responses=(response,),
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        await repository.create_interrupt_claims(
            thread_pk=thread_pk,
            source_run_id="run-review",
            claimed_run_id=context.identity.run_id,
            interrupt_ids=(response.interrupt_id,),
        )
        await repository.commit()
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
        notifications=notifications,
    )
    try:
        await coordinator.settle_resume(thread_pk=thread_pk, receipt=receipt)
        different = AgUiResumeReceipt(
            identity=(
                RunIdentity(
                    namespace=context.identity.namespace,
                    thread_id=context.identity.thread_id,
                    run_id="another-run",
                )
                if conflict == "run"
                else context.identity
            ),
            parent_run_id="run-review",
            receipt_id="different-id" if conflict == "receipt" else receipt.receipt_id,
            responses=(
                AgUiResumeResponse("public-review", "cancelled")
                if conflict == "status"
                else response,
            ),
        )
        with pytest.raises(RuntimeError, match="恢复"):
            await coordinator.settle_resume(thread_pk=thread_pk, receipt=different)
        async with database.session() as session:
            claims = await ConversationRepository(session).list_claims_for_update(
                thread_pk=thread_pk, interrupt_ids=frozenset({response.interrupt_id})
            )
        assert [(claim.status, claim.resolution_id) for claim in claims] == [
            ("resolved", "receipt-id")
        ]
    finally:
        await coordinator.aclose()
        await messaging.aclose()
        await trace_session.aclose()


async def test_abandoned_trace_settles_the_complete_claim_batch_as_cancelled(
    notifications,
    database,
) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    identity = RunIdentity(
        namespace="ns_1", thread_id="thread-abandon", run_id="run-abandon"
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            project_id="project-1",
            user_id=1,
            thread_id=identity.thread_id,
            title="放弃恢复",
            model_id="model-main",
        )
        registration = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=identity.run_id,
            parent_run_id="run-interrupted",
            model_id="model-main",
            input_json={"runId": identity.run_id},
        )
        await repository.create_interrupt_claims(
            thread_pk=thread.id,
            source_run_id="run-interrupted",
            claimed_run_id=identity.run_id,
            interrupt_ids=("interrupt-1", "interrupt-2"),
        )
        assert await repository.activate_run_registration(
            thread_pk=thread.id, run_pk=registration.id, run_id=identity.run_id
        )
        await repository.commit()
        thread_pk = thread.id
        registration_pk = registration.id
    context = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="abandon",
        parent_run_id="run-interrupted",
        input={},
        config={},
        resume=(
            RunResumeSummary(interrupt_id="interrupt-1", status="cancelled"),
            RunResumeSummary(interrupt_id="interrupt-2", status="cancelled"),
        ),
    )
    trace_session = await tracer.open_run(context)
    now = datetime.now(UTC)
    await trace_session.observe(
        RunStartedObservation(
            identity=identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await trace_session.observe(
        RunInputObservation(
            identity=identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        )
    )
    await trace_session.observe(
        RunTerminalObservation(
            identity=identity,
            outcome="abandoned",
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await trace_session.observe(
        RunClosedObservation(
            identity=identity,
            outcome="abandoned",
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await trace_session.aclose()
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
        notifications=notifications,
    )

    await coordinator.reconcile(thread_pk=thread_pk, identity=identity)

    async with database.session() as session:
        repository = ConversationRepository(session)
        claims = await repository.list_claims_for_update(
            thread_pk=thread_pk,
            interrupt_ids=frozenset({"interrupt-1", "interrupt-2"}),
        )
        stored_registration = await session.get(
            ConversationRunRegistration,
            registration_pk,
        )
    assert {claim.status for claim in claims} == {"cancelled"}
    assert all(claim.resolution_id is not None for claim in claims)
    assert stored_registration is not None
    assert stored_registration.status == "abandoned"
    await coordinator.aclose()
    await messaging.aclose()


pytestmark = pytest.mark.usefixtures("projects")
