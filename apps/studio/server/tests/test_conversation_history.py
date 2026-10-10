from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from tinkerfin import AgUiResumeReceipt, AgUiResumeResponse
from tinkerfin_contracts import (
    NativeInterruptRecord,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeStateObservation,
    NativeToolCall,
    ObservationBoundary,
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunObservationSession,
    RunResumeCheckpointedObservation,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RunTerminalOutcome,
)
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.history_queries import HistoryQueryAdmission
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.schemas import (
    ConversationHistoryDetail,
)
from tinkerfin_studio.conversation.todo_groups import (
    TodoGroupProjection,
)
from tinkerfin_tracing import (
    TraceGraphFilter,
    TraceGraphNodeKind,
    Tracer,
)


def _service(
    repository: ConversationRepository,
    *,
    tracer: Tracer,
    history_queries: HistoryQueryAdmission | None = None,
) -> ConversationHistoryService:
    return ConversationHistoryService(
        repository,
        user_id=1,
        tracer=tracer,
        history_queries=history_queries or HistoryQueryAdmission(),
    )


async def _get_detail(
    service: ConversationHistoryService,
    thread_id: str,
    *,
    history_cursor: str | None = None,
    limit: int = 100,
    include_task_trace: bool = True,
) -> ConversationHistoryDetail:
    return await service.get_detail(
        thread_id,
        history_cursor=history_cursor,
        limit=limit,
        include_task_trace=include_task_trace,
    )


@pytest.mark.parametrize("query", [None, "Trace"])
@pytest.mark.parametrize("pinned", [False, True])
async def test_preparing_conversations_do_not_fill_history_pages(
    session, query, pinned
):
    """准备中的记录在查询与分页限制前排除，不占用历史页名额"""
    repository = ConversationRepository(session)
    for index in range(5):
        thread = await repository.create_thread(
            project_id="project-1",
            user_id=1,
            thread_id=f"thread-{index}",
            title="Trace 会话",
            model_id="main",
        )
        thread.updated_at = datetime(2030, 1, 1)
        thread.pinned = pinned and index > 0
        if index < 3:
            thread.last_run_id = f"run-{index}"
    await repository.commit()
    service = _service(repository, tracer=Tracer())
    first = await service.list_history(
        project_id="project-1", page_size=2, cursor=None, query=query
    )
    assert [item.thread_id for item in first.items] == ["thread-2", "thread-1"]
    assert first.next_cursor is not None
    second = await service.list_history(
        project_id="project-1", page_size=2, cursor=first.next_cursor, query=query
    )
    assert [item.thread_id for item in second.items] == ["thread-0"]
    assert second.next_cursor is None


def _context(thread_id: str, run_id: str) -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(namespace="ns_1", thread_id=thread_id, run_id=run_id),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={
            "messages": [
                {
                    "id": f"user-{run_id}",
                    "role": "user",
                    "content": f"request {run_id}",
                }
            ]
        },
        config={},
    )


async def _open_trace(
    tracer: Tracer,
    *,
    thread_id: str,
    run_id: str,
) -> tuple[RunSourceContext, RunObservationSession]:
    context = _context(thread_id, run_id)
    session = await tracer.open_run(context)
    now = datetime.now(UTC)
    await session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        )
    )
    return context, session


async def _finish_trace(
    context: RunSourceContext,
    session: RunObservationSession,
    *,
    outcome: RunTerminalOutcome = "succeeded",
) -> None:
    now = datetime.now(UTC)
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome=outcome,
            observed_at=now,
            monotonic_ns=90,
        )
    )
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome=outcome,
            observed_at=now,
            monotonic_ns=91,
        )
    )
    await session.aclose()


async def _register(
    repository: ConversationRepository,
    *,
    user_id: int,
    thread_id: str,
    run_id: str,
):
    thread = await repository.get_thread(user_id=user_id, thread_id=thread_id)
    if thread is None:
        thread = await repository.create_thread(
            project_id="project-" + str(user_id),
            user_id=user_id,
            thread_id=thread_id,
            title="Trace 会话",
            model_id="model-main",
        )
    await repository.create_run_registration(
        thread_id=thread.id,
        run_id=run_id,
        parent_run_id=thread.last_run_id,
        model_id="model-main",
        input_json={"runId": run_id},
    )
    thread.last_run_id = run_id
    thread.last_model = "model-main"
    await repository.commit()
    return thread


async def test_history_cursor_keeps_original_as_of_after_new_turn(session) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection()),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-cursor",
        run_id="run-first",
    )
    first_context, first_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-first",
    )
    await _finish_trace(first_context, first_session)
    service = _service(repository, tracer=tracer)
    first = await _get_detail(service, thread.thread_id, limit=1)
    assert first.history_cursor is None

    await _register(
        repository,
        user_id=1,
        thread_id=thread.thread_id,
        run_id="run-second",
    )
    second_context, second_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-second",
    )
    await _finish_trace(second_context, second_session)
    latest = await _get_detail(service, thread.thread_id, limit=1)

    assert latest.history_cursor is not None
    fixed = await _get_detail(
        service,
        thread.thread_id,
        history_cursor=latest.history_cursor,
        limit=1,
        include_task_trace=False,
    )
    assert fixed.as_of_seq == latest.as_of_seq
    assert fixed.head_run_id == "run-second"
    assert len(fixed.messages) > len(latest.messages)
    assert latest.task_trace is not None
    assert fixed.task_trace is None


@pytest.mark.parametrize(
    "settlement", ["unknown", "not_saved", "resolved", "cancelled"]
)
async def test_history_exposes_submission_ownership_and_confirmed_settlement(
    session, settlement: str
) -> None:
    """完整公开交互组的认领与回执决定能否再次提交"""
    tracer = Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection())
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository, user_id=1, thread_id="approval-history", run_id="paused-run"
    )
    context, source = await _open_trace(
        tracer, thread_id=thread.thread_id, run_id="paused-run"
    )
    await source.observe(
        NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={},
            interrupts=tuple(
                NativeInterruptRecord(
                    id=interrupt_id, value={"kind": "input_required", "message": "Wait"}
                )
                for interrupt_id in ("approval-first", "approval-second")
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish_trace(context, source, outcome="interrupted")
    initial = await _get_detail(_service(repository, tracer=tracer), thread.thread_id)
    interrupt_ids = tuple(
        item.interrupt_id for item in initial.interaction_availability
    )
    assert len(interrupt_ids) == 2
    await repository.create_run_registration(
        thread_id=thread.id,
        run_id="submitted-run",
        parent_run_id="paused-run",
        model_id="model-main",
        input_json={"resume": [{"interruptId": value} for value in interrupt_ids]},
    )
    thread.last_run_id = "submitted-run"
    await repository.create_interrupt_claims(
        thread_pk=thread.id,
        source_run_id="paused-run",
        claimed_run_id="submitted-run",
        interrupt_ids=interrupt_ids,
    )
    await repository.create_interrupt_claims(
        thread_pk=thread.id,
        source_run_id="other-branch",
        claimed_run_id="unrelated-run",
        interrupt_ids=("unrelated-approval",),
    )
    await repository.commit()
    submitted_context, submitted_source = await _open_trace(
        tracer, thread_id=thread.thread_id, run_id="submitted-run"
    )
    await _finish_trace(submitted_context, submitted_source, outcome="failed")
    if settlement == "not_saved":
        await repository.release_claims(thread_pk=thread.id, run_id="submitted-run")
    elif settlement in {"resolved", "cancelled"}:
        await repository.settle_claims(
            thread_pk=thread.id,
            receipt=AgUiResumeReceipt(
                identity=submitted_context.identity,
                parent_run_id="paused-run",
                receipt_id="confirmed-receipt",
                responses=tuple(
                    AgUiResumeResponse(
                        interrupt_id=value,
                        status="resolved" if settlement == "resolved" else "cancelled",
                    )
                    for value in interrupt_ids
                ),
            ),
        )
    await repository.commit()
    detail = await _get_detail(_service(repository, tracer=tracer), thread.thread_id)
    expected = {"unknown": "confirming", "not_saved": "available"}.get(
        settlement, settlement
    )
    assert {
        (item.interrupt_id, item.state, item.submission_run_id)
        for item in detail.interaction_availability
    } == {
        (value, expected, None if settlement == "not_saved" else "submitted-run")
        for value in interrupt_ids
    }
    if settlement == "not_saved":
        assert detail.submission_result is not None
        assert detail.submission_result.submission_run_id == "submitted-run"
        assert set(detail.submission_result.interrupt_ids) == set(interrupt_ids)
    else:
        assert detail.submission_result is None


async def test_history_rejects_another_users_thread_before_trace_lookup(
    session,
) -> None:
    repository = ConversationRepository(session)
    await _register(
        repository,
        user_id=2,
        thread_id="thread-private",
        run_id="run-private",
    )
    service = _service(
        repository,
        tracer=Tracer(
            projections=(ConversationFailureProjection(), TodoGroupProjection()),
        ),
    )

    with pytest.raises(BusinessException) as captured:
        await service.get_detail("thread-private")

    assert captured.value.error_code is ConversationErrorCode.NOT_FOUND
    with pytest.raises(BusinessException) as graph_error:
        await service.query_trace_graph(
            "thread-private",
            where=TraceGraphFilter(),
            cursor=None,
            limit=100,
        )
    assert graph_error.value.error_code is ConversationErrorCode.NOT_FOUND


async def test_trace_follow_sends_snapshot_then_semantic_update_and_closes(
    session,
) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection()),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-follow",
        run_id="run-follow",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-follow",
    )
    events = await _service(repository, tracer=tracer).follow_trace(thread.thread_id)
    assert session.in_transaction() is False

    snapshot = await anext(events)
    assert snapshot.type == "snapshot"
    assert snapshot.snapshot.status.execution == "running"
    assert snapshot.snapshot.task_trace is not None
    pending = asyncio.create_task(anext(events))
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-follow",
                content="delta",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )

    update = await pending
    assert update.type == "update"
    assert update.update.messages.upserts[0].content == "delta"
    retained = update.update.messages.upserts[0]
    assert retained.agui is not None
    wire = update.model_dump(mode="json", by_alias=True)
    assert (
        wire["update"]["messages"]["upserts"][0]["agui"]["messageId"]
        == retained.agui.message_id
    )
    assert update.task_trace is None
    await events.aclose()
    await _finish_trace(context, trace_session)


async def test_live_failure_and_snapshot_have_identical_results(session):
    tracer = Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection())
    )
    repository = ConversationRepository(session)
    await _register(repository, user_id=1, thread_id="failure-live", run_id="run-live")
    context, source = await _open_trace(
        tracer, thread_id="failure-live", run_id="run-live"
    )
    service = _service(repository, tracer=tracer)
    stream = await service.follow_trace("failure-live", include_task_trace=False)
    first = await anext(stream)
    assert first.type == "snapshot" and first.snapshot.run_failures == ()
    await source.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="failed",
            code="runtime_initialization_error",
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await source.force(ObservationBoundary.TERMINAL)
    try:
        update = await anext(stream)
        assert update.type == "update"
        assert len(update.run_failures) == 1
        detail = await service.get_detail("failure-live", include_task_trace=False)
        assert update.run_failures == detail.run_failures
    finally:
        await stream.aclose()
        await source.aclose()


async def test_tool_review_uses_same_reference_in_history_graph_and_follow(
    session,
) -> None:
    """审批与工具在历史、链路分页和订阅快照中保留同一可恢复关联"""
    tracer = Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection())
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-review-reference",
        run_id="run-review-reference",
    )
    context, trace_session = await _open_trace(
        tracer, thread_id=thread.thread_id, run_id="run-review-reference"
    )
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="report-proposal",
                content="",
                tool_calls=(
                    NativeToolCall(
                        id="save-report",
                        name="write_file",
                        arguments={"file_path": "/report.md", "content": "报告"},
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await trace_session.observe(
        NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={},
            messages=(
                NativeMessageRecord(
                    message_type="assistant",
                    id="report-proposal",
                    content="",
                    tool_calls=(
                        NativeToolCall(
                            id="save-report",
                            name="write_file",
                            arguments={"file_path": "/report.md", "content": "报告"},
                        ),
                    ),
                ),
            ),
            interrupts=(
                NativeInterruptRecord(
                    id="review-report",
                    value={
                        "action_requests": [
                            {
                                "name": "write_file",
                                "args": {"file_path": "/report.md", "content": "报告"},
                            }
                        ],
                        "review_configs": [
                            {
                                "action_name": "write_file",
                                "allowed_decisions": ["approve", "reject"],
                            }
                        ],
                    },
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await _finish_trace(context, trace_session, outcome="interrupted")
    service = _service(repository, tracer=tracer)
    detail = await service.get_detail(thread.thread_id, include_task_trace=False)
    tool = next(node for node in detail.graph.nodes if node.kind == "tool")
    assert tool.agui is not None and tool.agui.kind == "tool"
    assert tool.source_id == "save-report"
    assert tool.agui.tool_call_id != tool.source_id
    review = detail.interactions[0]
    assert review.agui is not None and len(review.agui) == 1
    assert review.agui[0].tool_call_id == tool.agui.tool_call_id
    assert review.agui[0].id == "review-report"
    assert detail.model_dump(mode="json", by_alias=True)["interactionAvailability"] == [
        {
            "interruptId": "review-report",
            "state": "available",
            "submissionRunId": None,
        }
    ]
    await repository.create_interrupt_claims(
        thread_pk=thread.id,
        source_run_id="run-review-reference",
        claimed_run_id="run-review-submit",
        interrupt_ids=("review-report",),
    )
    await repository.commit()
    claimed_detail = await service.get_detail(
        thread.thread_id, include_task_trace=False
    )
    assert claimed_detail.model_dump(mode="json", by_alias=True)[
        "interactionAvailability"
    ] == [
        {
            "interruptId": "review-report",
            "state": "confirming",
            "submissionRunId": "run-review-submit",
        }
    ]
    page = await service.query_trace_graph(
        thread.thread_id,
        where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
        cursor=None,
        limit=1,
    )
    assert next(node for node in page.nodes if node.id == tool.id).agui == tool.agui
    for events in (
        await service.follow_trace(thread.thread_id, include_task_trace=False),
        await service.follow_trace_graph(
            thread.thread_id,
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
            limit=1,
        ),
    ):
        try:
            event = await anext(events)
            assert event.type == "snapshot"
            wire = event.model_dump(mode="json", by_alias=True)["snapshot"]
            nodes = wire["graph"]["nodes"] if "graph" in wire else wire["nodes"]
            assert (
                next(node for node in nodes if node["id"] == tool.id)["agui"][
                    "toolCallId"
                ]
                == tool.agui.tool_call_id
            )
        finally:
            await events.aclose()


async def test_live_replay_authorizes_run_and_keeps_sse_cursor_separate(
    session,
) -> None:
    from ag_ui.core import (
        RunFinishedEvent,
        RunStartedEvent,
        TextMessageContentEvent,
        TextMessageStartEvent,
    )

    from tinkerfin_messaging import AgUiCodec, Messaging

    tracer = Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection())
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository, user_id=1, thread_id="live-thread", run_id="live-run"
    )
    context, trace_session = await _open_trace(
        tracer, thread_id=thread.thread_id, run_id="live-run"
    )
    release = asyncio.Event()

    async def events():
        yield RunStartedEvent(thread_id=thread.thread_id, run_id="live-run")
        yield TextMessageStartEvent(message_id="answer", role="assistant")
        yield TextMessageContentEvent(message_id="answer", delta="首段")
        await release.wait()
        yield TextMessageContentEvent(message_id="answer", delta="后续")
        yield RunFinishedEvent(thread_id=thread.thread_id, run_id="live-run")

    async with Messaging() as messaging:
        channel = messaging.agui_channel(name="live")
        owner = await messaging.channel(name="live", codec=AgUiCodec()).open_sse(
            events(), identity=context.identity, after=0
        )
        service = ConversationHistoryService(
            repository,
            user_id=1,
            tracer=tracer,
            history_queries=HistoryQueryAdmission(),
            conversation_channel=channel,
        )
        try:
            # 未授权的会话和未登记运行均不得打开订阅
            other_user = ConversationHistoryService(
                repository,
                user_id=2,
                tracer=tracer,
                history_queries=HistoryQueryAdmission(),
                conversation_channel=channel,
            )
            with pytest.raises(BusinessException) as missing_thread:
                await other_user.follow_live(
                    thread.thread_id, run_id="live-run", last_event_id=None
                )
            assert missing_thread.value.error_code == ConversationErrorCode.NOT_FOUND
            with pytest.raises(BusinessException) as missing_run:
                await service.follow_live(
                    thread.thread_id, run_id="other", last_event_id=None
                )
            assert missing_run.value.error_code == ConversationErrorCode.RUN_NOT_FOUND
            body = await service.follow_live(
                thread.thread_id, run_id="live-run", last_event_id=None
            )
            try:
                snapshot = json.loads(
                    (await anext(body)).decode().split("data: ", 1)[1]
                )
                assert snapshot["type"] == "snapshot" and snapshot["replay"] is True
                assert (
                    snapshot["snapshot"]["messages"][0]["content"] == "request live-run"
                )
                frames = [await anext(body) for _ in range(3)]
                assert [frame.splitlines()[0] for frame in frames] == [
                    b"id: 1",
                    b"id: 2",
                    b"id: 3",
                ]
                assert "首段" in frames[-1].decode()
                assert not release.is_set()
            finally:
                await body.aclose()
            with pytest.raises(BusinessException) as invalid:
                await service.follow_live(
                    thread.thread_id, run_id="live-run", last_event_id="999"
                )
            assert (
                invalid.value.error_code == ConversationErrorCode.INVALID_LAST_EVENT_ID
            )
            tail = await service.follow_live(
                thread.thread_id, run_id="live-run", last_event_id="3"
            )
            try:
                release.set()
                frames = [frame async for frame in tail]
                assert [frame.splitlines()[0] for frame in frames] == [
                    b"id: 4",
                    b"id: 5",
                ]
                assert "后续" in frames[0].decode()
            finally:
                await tail.aclose()
        finally:
            release.set()
            await owner.aclose()
            await _finish_trace(context, trace_session)


pytestmark = pytest.mark.usefixtures("projects")


async def test_saved_plan_answers_remain_readable_before_and_after_continuation(
    session,
):
    tracer = Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection())
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository, user_id=1, thread_id="plan-results", run_id="plan-paused"
    )
    context, source = await _open_trace(
        tracer, thread_id=thread.thread_id, run_id="plan-paused"
    )
    await source.observe(
        NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={},
            interrupts=(
                NativeInterruptRecord(
                    id="plan-question",
                    value={
                        "schema": "tinkerfin.runtime-interrupt",
                        "kind": "tinkerfin:plan_clarification",
                        "responseSchema": {},
                        "metadata": {
                            "origin": "plan",
                            "clarification": {
                                "form": {
                                    "title": "文字",
                                    "description": "确认文案",
                                    "questions": [
                                        {
                                            "id": "text",
                                            "answerType": "text",
                                            "prompt": "添加什么文字",
                                            "required": True,
                                        },
                                    ],
                                }
                            },
                        },
                    },
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish_trace(context, source, outcome="interrupted")
    service = _service(repository, tracer=tracer)
    initial = await _get_detail(service, thread.thread_id)
    assert initial.plan_results == ()
    interrupt_id = initial.interaction_availability[0].interrupt_id
    await repository.create_run_registration(
        thread_id=thread.id,
        run_id="plan-submit",
        parent_run_id="plan-paused",
        model_id="main",
        input_json={
            "resume": [
                {
                    "interruptId": interrupt_id,
                    "status": "resolved",
                    "payload": {
                        "type": "respond",
                        "answers": {
                            "text": {
                                "status": "answered",
                                "answerType": "text",
                                "answer": "今天也要开心呀",
                            }
                        },
                    },
                }
            ]
        },
    )
    await repository.create_interrupt_claims(
        thread_pk=thread.id,
        source_run_id="plan-paused",
        claimed_run_id="plan-submit",
        interrupt_ids=(interrupt_id,),
    )
    await repository.commit()
    assert (await _get_detail(service, thread.thread_id)).plan_results == ()
    unknown = await service.get_detail(
        thread.thread_id, submission_run_id="not-registered"
    )
    assert unknown.submission_result is None
    await repository.settle_claims(
        thread_pk=thread.id,
        receipt=AgUiResumeReceipt(
            identity=RunIdentity(
                namespace="ns_1", thread_id=thread.thread_id, run_id="plan-submit"
            ),
            parent_run_id="plan-paused",
            receipt_id="plan-saved",
            responses=(AgUiResumeResponse(interrupt_id, "resolved"),),
        ),
    )
    await repository.commit()
    saved = await _get_detail(service, thread.thread_id)
    assert saved.head_run_id == "plan-paused"
    assert saved.plan_results[0].model_dump(mode="json", by_alias=True)["answers"] == {
        "text": {
            "status": "answered",
            "answerType": "text",
            "answer": "今天也要开心呀",
        },
    }
    assert saved.plan_results[0].submission_run_id == "plan-submit"
    resumed_context = RunSourceContext(
        identity=RunIdentity(
            namespace="ns_1", thread_id=thread.thread_id, run_id="plan-submit"
        ),
        runtime_profile="deepagents-v2",
        input_kind="resume",
        parent_run_id="plan-paused",
        input={},
        config={},
        resume=(RunResumeSummary(interrupt_id="plan-question", status="resolved"),),
    )
    resumed_source = await tracer.open_run(resumed_context)
    try:
        await resumed_source.observe(
            RunStartedObservation(
                identity=resumed_context.identity,
                observed_at=datetime.now(UTC),
                monotonic_ns=1,
            )
        )
        await resumed_source.observe(
            RunInputObservation(
                identity=resumed_context.identity,
                source=resumed_context,
                observed_at=datetime.now(UTC),
                monotonic_ns=2,
            )
        )
        await resumed_source.observe(
            RunResumeCheckpointedObservation(
                identity=resumed_context.identity,
                marker_id="plan-saved",
                native_interrupt_ids=("plan-question",),
                observed_at=datetime.now(UTC),
                monotonic_ns=3,
            )
        )
        await _finish_trace(resumed_context, resumed_source)
    finally:
        await resumed_source.aclose()
    thread.last_run_id = "plan-submit"
    await repository.commit()
    reloaded = await _get_detail(_service(repository, tracer=tracer), thread.thread_id)
    assert reloaded.head_run_id == "plan-submit"
    assert reloaded.interactions[0].status == "resolved"
    assert reloaded.interactions[0].agui == initial.interactions[0].agui
    assert reloaded.plan_results == saved.plan_results
    assert all(item.state == "resolved" for item in reloaded.interaction_availability)
