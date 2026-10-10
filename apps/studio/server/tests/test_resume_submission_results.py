"""已确认未保存的提交保留原登记，与当前交互认领分别维护"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec

import pytest
from test_conversation_history import _finish_trace, _open_trace, _register, _service
from test_trace_coordinator import _messaging_channel

from tinkerfin import AgUiResumeReceipt, AgUiResumeResponse, RunIdentity
from tinkerfin_contracts import (
    NativeInterruptRecord,
    NativeStateObservation,
    RunInputObservation,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
)
from tinkerfin_gateway import Gateway, RunAcceptance
from tinkerfin_notifications import Notification
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.coordinator import ConversationTraceCoordinator
from tinkerfin_studio.conversation.delivery import (
    ConversationAdmission,
    ConversationResumeSettlement,
)
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.history_queries import HistoryQueryAdmission
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.run_preparation import RegisteredRun
from tinkerfin_studio.conversation.service import ConversationChatService
from tinkerfin_studio.conversation.todo_groups import TodoGroupProjection
from tinkerfin_studio.resources import ApplicationResources
from tinkerfin_tracing import Tracer


async def _pending(repository):
    tracer = Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection())
    )
    thread = await _register(
        repository, user_id=1, thread_id="resume-result", run_id="paused"
    )
    context, source = await _open_trace(
        tracer, thread_id=thread.thread_id, run_id="paused"
    )
    await source.observe(
        NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={},
            interrupts=tuple(
                NativeInterruptRecord(
                    id=identifier, value={"kind": "input_required", "message": "确认"}
                )
                for identifier in ("first", "second")
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish_trace(context, source, outcome="interrupted")
    service = _service(repository, tracer=tracer)
    detail = await service.get_detail(thread.thread_id)
    return (
        thread,
        tracer,
        tuple(item.interrupt_id for item in detail.interaction_availability),
    )


async def _submit(repository, thread, interrupt_ids, run_id="submitted"):
    run = await repository.create_run_registration(
        thread_id=thread.id,
        run_id=run_id,
        parent_run_id="paused",
        model_id="model-main",
        input_json={
            "resume": [
                {"interruptId": identifier, "status": "resolved"}
                for identifier in interrupt_ids
            ]
        },
    )
    await repository.create_interrupt_claims(
        thread_pk=thread.id,
        source_run_id="paused",
        claimed_run_id=run_id,
        interrupt_ids=interrupt_ids,
    )
    await repository.commit()
    return run


async def _preflight_trace(tracer, identity):
    context = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="resume",
        parent_run_id="paused",
        input={},
        config={},
        resume=tuple(
            RunResumeSummary(interrupt_id=identifier, status="resolved")
            for identifier in ("first", "second")
        ),
    )
    source = await tracer.open_run(context)
    observed = datetime(2030, 1, 1, tzinfo=UTC)
    await source.observe(
        RunStartedObservation(identity=identity, observed_at=observed, monotonic_ns=1)
    )
    await source.observe(
        RunInputObservation(
            identity=identity, source=context, observed_at=observed, monotonic_ns=2
        )
    )
    return context, source


@pytest.mark.parametrize("owner", ["runtime", "admission"])
async def test_confirmed_not_saved_survives_registration_cleanup_and_notifies_after_commit(
    database, session, notifications, attachments, monkeypatch, owner
):
    repository = ConversationRepository(session)
    thread, tracer, ids = await _pending(repository)
    run = await _submit(repository, thread, ids)
    resources = create_autospec(ApplicationResources, instance=True)
    resources.database = database
    resources.notifications = notifications
    resources.attachments = attachments
    notices: list[Notification] = []
    publish = notifications.publish

    async def capture(notification: Notification) -> None:
        if notification.topic == "studio.conversation.interactions.changed":
            async with database.session() as observer:
                detail = await _service(
                    ConversationRepository(observer), tracer=tracer
                ).get_detail(thread.thread_id, submission_run_id=run.run_id)
            assert detail.model_dump(by_alias=True)["submissionResult"] == {
                "submissionRunId": run.run_id,
                "interruptIds": tuple(sorted(ids)),
                "state": "not_saved",
            }
            notices.append(notification)
        await publish(notification)

    monkeypatch.setattr(notifications, "publish", capture)
    admission = ConversationAdmission(
        resources,
        user_id=1,
        thread_pk=thread.id,
        identity=RunIdentity(
            namespace="ns_1", thread_id=thread.thread_id, run_id=run.run_id
        ),
        registered=RegisteredRun(run.id, True, run.preparation_id),
        thread_created=False,
    )
    if owner == "runtime":
        settlement = ConversationResumeSettlement(
            resources, thread_pk=thread.id, run_id=run.run_id
        )
        await settlement.not_saved()
        await settlement.not_saved()
    await admission.release()
    assert notices
    assert all(
        dict(item.details) == {"submissionRunId": run.run_id} for item in notices
    )
    async with database.session() as observer:
        checking = ConversationRepository(observer)
        retained = await checking.get_run(thread_pk=thread.id, run_id=run.run_id)
        assert retained is not None
        assert retained.resume_not_saved is True
        assert retained.status == "rejected"
        detail = await _service(checking, tracer=tracer).get_detail(
            thread.thread_id, submission_run_id=run.run_id
        )
    assert detail.model_dump(mode="json", by_alias=True)["submissionResult"] == {
        "submissionRunId": run.run_id,
        "interruptIds": sorted(ids),
        "state": "not_saved",
    }
    assert all(
        item.state == "available" and item.submission_run_id is None
        for item in detail.interaction_availability
    )


@pytest.mark.parametrize("status", ["resolved", "cancelled"])
async def test_not_saved_cannot_replace_a_saved_or_cancelled_decision(session, status):
    repository = ConversationRepository(session)
    thread, tracer, ids = await _pending(repository)
    await _submit(repository, thread, ids)
    await repository.settle_claims(
        thread_pk=thread.id,
        receipt=AgUiResumeReceipt(
            identity=RunIdentity(
                namespace="ns_1", thread_id=thread.thread_id, run_id="submitted"
            ),
            parent_run_id="paused",
            receipt_id="saved",
            responses=tuple(
                AgUiResumeResponse(identifier, status) for identifier in ids
            ),
        ),
    )
    await repository.commit()
    with pytest.raises(RuntimeError, match="未保存"):
        await repository.release_claims(thread_pk=thread.id, run_id="submitted")
    await repository.rollback()
    detail = await _service(repository, tracer=tracer).get_detail(
        "resume-result", submission_run_id="submitted"
    )
    assert detail.model_dump(by_alias=True)["submissionResult"] is None
    assert all(item.state == status for item in detail.interaction_availability)


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("trace_exists", [False, True])
async def test_stale_resume_recovery_requires_confirmed_not_saved(
    database,
    session,
    notifications,
    fixed_utc_time,
    monkeypatch,
    confirmed,
    trace_exists,
):
    repository = ConversationRepository(session)
    thread, tracer, ids = await _pending(repository)
    run = await _submit(repository, thread, ids)
    if confirmed:
        await repository.release_claims(thread_pk=thread.id, run_id=run.run_id)
    run.created_at = fixed_utc_time.replace(tzinfo=None) - timedelta(minutes=1)
    await repository.commit()
    if trace_exists:
        context, source = await _preflight_trace(
            tracer,
            RunIdentity(
                namespace="ns_1", thread_id=thread.thread_id, run_id=run.run_id
            ),
        )
        await _finish_trace(context, source, outcome="cancelled")
    notices: list[Notification] = []
    publish = notifications.publish

    async def capture(notification: Notification) -> None:
        if notification.topic == "studio.conversation.interactions.changed":
            async with database.session() as observer:
                retained = await ConversationRepository(observer).get_run(
                    thread_pk=thread.id, run_id=run.run_id
                )
                assert retained is not None and retained.status == "rejected"
                assert retained.resume_not_saved is True
            notices.append(notification)
        await publish(notification)

    monkeypatch.setattr(notifications, "publish", capture)
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
        notifications=notifications,
    )
    try:
        await coordinator.recover(thread_pk=thread.id)
        async with database.session() as observer:
            checking = ConversationRepository(observer)
            retained = await checking.get_run(thread_pk=thread.id, run_id="submitted")
            assert retained is not None
            assert retained.status == ("rejected" if confirmed else "preparing")
            assert retained.resume_not_saved is confirmed
            claims = await checking.list_claims_for_update(
                thread_pk=thread.id, interrupt_ids=frozenset(ids)
            )
            assert len(claims) == (0 if confirmed else len(ids))
            assert all(claim.status == "claimed" for claim in claims)
        assert len(notices) == int(confirmed)
        if notices:
            assert dict(notices[0].details) == {"submissionRunId": run.run_id}
    finally:
        await coordinator.aclose()
        await messaging.aclose()


async def test_not_saved_proof_and_claim_release_rollback_together(session):
    repository = ConversationRepository(session)
    thread, _, ids = await _pending(repository)
    run = await _submit(repository, thread, ids)
    thread_pk, run_id = thread.id, run.run_id
    await repository.release_claims(thread_pk=thread_pk, run_id=run_id)
    await repository.rollback()
    retained = await repository.get_run(thread_pk=thread_pk, run_id=run_id)
    assert retained is not None and retained.resume_not_saved is False
    assert retained.status == "preparing"
    claims = await repository.list_claims_for_update(
        thread_pk=thread_pk, interrupt_ids=frozenset(ids)
    )
    assert {claim.interrupt_id for claim in claims} == set(ids)
    assert all(claim.status == "claimed" for claim in claims)


async def test_submission_proof_is_scoped_to_authorized_thread(session):
    repository = ConversationRepository(session)
    thread, tracer, ids = await _pending(repository)
    await _submit(repository, thread, ids)
    await repository.release_claims(thread_pk=thread.id, run_id="submitted")
    await repository.commit()
    other = await _register(
        repository, user_id=1, thread_id="other-thread", run_id="paused"
    )
    context, source = await _open_trace(
        tracer, thread_id=other.thread_id, run_id="paused"
    )
    await _finish_trace(context, source)
    await _submit(repository, other, ids)
    detail = await _service(repository, tracer=tracer).get_detail(
        other.thread_id, submission_run_id="submitted"
    )
    assert detail.submission_result is None
    assert all(item.state == "confirming" for item in detail.interaction_availability)
    other_user = ConversationHistoryService(
        repository, user_id=2, tracer=tracer, history_queries=HistoryQueryAdmission()
    )
    with pytest.raises(BusinessException) as denied:
        await other_user.get_detail(thread.thread_id, submission_run_id="submitted")
    assert denied.value.error_code is ConversationErrorCode.NOT_FOUND


async def test_rejected_submission_cannot_follow_or_cancel_gateway(session):
    repository = ConversationRepository(session)
    thread, tracer, ids = await _pending(repository)
    run = await _submit(repository, thread, ids)
    await repository.delete_unstarted_run(
        thread_pk=thread.id,
        run_pk=run.id,
        run_id=run.run_id,
        preparation_id=run.preparation_id,
        delete_empty_thread=False,
        resume_not_saved=True,
    )
    await repository.commit()
    with pytest.raises(BusinessException) as follow:
        await _service(repository, tracer=tracer).follow_live(
            thread.thread_id, run_id=run.run_id, last_event_id=None
        )
    assert follow.value.error_code is ConversationErrorCode.RUN_NOT_FOUND
    resources = create_autospec(ApplicationResources, instance=True)
    gateway = create_autospec(Gateway, instance=True)
    resources.gateway = gateway
    service = ConversationChatService(
        session,
        user=UserContext(user_id=1, username="user", roles=(), disabled=False),
        resources=resources,
    )
    with pytest.raises(BusinessException) as cancel:
        await service.cancel(thread_id=thread.thread_id, run_id=run.run_id)
    assert cancel.value.error_code is ConversationErrorCode.RUN_NOT_FOUND
    gateway.run.assert_not_called()


async def test_stale_cleanup_owner_cannot_reject_reclaimed_resume(session):
    repository = ConversationRepository(session)
    thread, _, ids = await _pending(repository)
    run = await _submit(repository, thread, ids)
    prior_owner = run.preparation_id
    run.preparation_id = "another-request"
    await repository.commit()
    result = await repository.delete_unstarted_run(
        thread_pk=thread.id,
        run_pk=run.id,
        run_id=run.run_id,
        preparation_id=prior_owner,
        delete_empty_thread=False,
        resume_not_saved=True,
    )
    await repository.commit()
    assert not result.run_deleted and not result.resume_released
    retained = await repository.get_run(thread_pk=thread.id, run_id=run.run_id)
    assert retained is not None and retained.status == "preparing"
    assert retained.resume_not_saved is False
    claims = await repository.list_claims_for_update(
        thread_pk=thread.id, interrupt_ids=frozenset(ids)
    )
    assert {claim.interrupt_id for claim in claims} == set(ids)


@pytest.mark.parametrize("confirmation", ["before", "during", "after"])
async def test_confirmed_resume_projects_trace_without_releasing_claims(
    database,
    session,
    notifications,
    attachments,
    fixed_utc_time,
    monkeypatch,
    confirmation,
):
    repository = ConversationRepository(session)
    thread, tracer, ids = await _pending(repository)
    run = await _submit(repository, thread, ids)
    identity = RunIdentity(
        namespace="ns_1", thread_id=thread.thread_id, run_id=run.run_id
    )
    context, source = await _preflight_trace(tracer, identity)
    resources = create_autospec(ApplicationResources, instance=True)
    resources.database = database
    resources.notifications = notifications
    resources.attachments = attachments
    admission = ConversationAdmission(
        resources,
        user_id=1,
        thread_pk=thread.id,
        identity=identity,
        registered=RegisteredRun(run.id, True, run.preparation_id),
        thread_created=False,
    )
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
        notifications=notifications,
    )
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    original_get = tracer.get

    async def paused_get(thread_identity, *, head_run_id=None, projections=()):
        trace = await original_get(
            thread_identity, head_run_id=head_run_id, projections=projections
        )
        if trace.summary.status.head_run_id == run.run_id and not read_started.is_set():
            read_started.set()
            await release_read.wait()
        return trace

    recovery = None
    try:
        if confirmation == "before":
            await admission.confirm(RunAcceptance(identity, "new"))
        if confirmation == "during":
            monkeypatch.setattr(tracer, "get", paused_get)
            recovery = asyncio.create_task(coordinator.recover(thread_pk=thread.id))
            await read_started.wait()
            await admission.confirm(RunAcceptance(identity, "new"))
            release_read.set()
            await recovery
        else:
            await coordinator.recover(thread_pk=thread.id)
        if confirmation == "after":
            async with database.session() as observer:
                checking = ConversationRepository(observer)
                waiting = await checking.get_run(thread_pk=thread.id, run_id=run.run_id)
                saved_thread = await checking.get_thread_by_pk(thread.id)
                assert waiting is not None and saved_thread is not None
                assert waiting.status == "preparing"
                assert waiting.trace_as_of_seq is not None
                assert saved_thread.last_run_id == "paused"
            await admission.confirm(RunAcceptance(identity, "new"))
        await _finish_trace(context, source, outcome="cancelled")
        await coordinator.reconcile(thread_pk=thread.id, identity=identity)
        await admission.release()
        async with database.session() as observer:
            checking = ConversationRepository(observer)
            retained = await checking.get_run(thread_pk=thread.id, run_id=run.run_id)
            saved_thread = await checking.get_thread_by_pk(thread.id)
            assert retained is not None and saved_thread is not None
            assert retained.status == "waiting"
            assert retained.resume_not_saved is False
            assert retained.trace_as_of_seq is not None
            assert saved_thread.last_run_id == run.run_id
            claims = await checking.list_claims_for_update(
                thread_pk=thread.id, interrupt_ids=frozenset(ids)
            )
            assert {claim.interrupt_id for claim in claims} == set(ids)
            assert all(claim.status == "claimed" for claim in claims)
    finally:
        release_read.set()
        if recovery is not None:
            if not recovery.done():
                recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        await source.aclose()
        await coordinator.aclose()
        await messaging.aclose()


pytestmark = pytest.mark.usefixtures("projects")
