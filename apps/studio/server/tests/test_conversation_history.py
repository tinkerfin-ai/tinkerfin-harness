from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

import pytest
from starlette.types import Message as AsgiMessage
from starlette.types import Scope

from tinkerfin_contracts import (
    ModelCallObservation,
    NativeInterruptRecord,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeStateObservation,
    NativeToolCall,
    NativeToolCallChunk,
    ObservationBoundary,
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunObservationSession,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RunTerminalOutcome,
)
from tinkerfin_studio.api.conversation_router import follow_trace, get_history
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.schemas import (
    ConversationHistoryDetail,
    ConversationTraceErrorEvent,
)
from tinkerfin_studio.conversation.todo_groups import (
    TodoGroupProjector,
    TodoGroupQueryExecutor,
)
from tinkerfin_tracing import (
    TraceCompleteness,
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    Tracer,
    TraceStatus,
    TraceThread,
)


def _service(
    repository: ConversationRepository,
    *,
    tracer: Tracer,
    todo_group_query: TodoGroupQueryExecutor | None = None,
) -> ConversationHistoryService:
    return ConversationHistoryService(
        repository,
        user_id=1,
        tracer=tracer,
        todo_group_query=todo_group_query or TodoGroupQueryExecutor(),
    )


class _CapturingTodoGroupQuery(TodoGroupQueryExecutor):
    projector: TodoGroupProjector | None = None

    async def project(self, trace: TraceThread) -> TodoGroupProjector:
        self.projector = await super().project(trace)
        return self.projector


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


async def test_history_reads_fixed_trace_view_without_agui_event_tail(
    session,
) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-history",
        run_id="run-history",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-history",
    )
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-history",
                content="answer",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish_trace(context, trace_session)

    detail = await _get_detail(_service(repository, tracer=tracer), thread.thread_id)
    payload = detail.model_dump(mode="json", by_alias=True)

    assert detail.head_run_id == "run-history"
    assert detail.status.execution == "succeeded"
    assert [(item.role, item.content) for item in detail.messages] == [
        ("user", "request run-history"),
        ("assistant", "answer"),
    ]
    assert detail.message_count == 2
    assistant = detail.messages[1]
    assert assistant.agui is not None
    assert assistant.agui.kind == "message"
    assert assistant.agui.message_id != assistant.id
    assert payload["messages"][1]["agui"]["messageId"] == assistant.agui.message_id
    assert "snapshot" not in payload
    assert "events" not in payload


async def test_trace_graph_query_returns_the_final_model_request(session) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-entry-history",
        run_id="run-entry-history",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-entry-history",
    )
    now = datetime.now(UTC)
    await trace_session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="started",
            call_id="model-entry",
            provider="openai",
            model="gpt-test",
            messages=(
                NativeMessageRecord(message_type="system", content="final-system"),
                NativeMessageRecord(message_type="human", content="final-user"),
            ),
            invocation={"model": "gpt-test"},
            options={"temperature": 0.2},
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await trace_session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="completed",
            call_id="model-entry",
            usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await _finish_trace(context, trace_session)

    page = await _service(repository, tracer=tracer).query_trace_graph(
        thread.thread_id,
        where=TraceGraphFilter(
            kinds={TraceGraphNodeKind.MODEL},
        ),
        cursor=None,
        limit=100,
    )

    assert len(page.nodes) == 1
    assert len(page.turns) == 1
    assert page.turns[0].ordinal == 1
    assert page.nodes[0].turn_id == page.turns[0].id
    assert page.nodes[0].request == {
        "messages": [
            {
                "messageType": "system",
                "id": None,
                "name": None,
                "content": "final-system",
                "toolCalls": [],
                "toolCallChunks": [],
                "toolCallId": None,
                "toolStatus": None,
                "responseMetadata": {},
                "usageMetadata": None,
            },
            {
                "messageType": "human",
                "id": None,
                "name": None,
                "content": "final-user",
                "toolCalls": [],
                "toolCallChunks": [],
                "toolCallId": None,
                "toolStatus": None,
                "responseMetadata": {},
                "usageMetadata": None,
            },
        ],
        "invocation": {"model": "gpt-test"},
        "options": {"temperature": 0.2},
    }
    assert page.nodes[0].usage == {
        "input_tokens": 2,
        "output_tokens": 1,
        "total_tokens": 3,
    }
    with pytest.raises(BusinessException) as invalid_cursor:
        await _service(repository, tracer=tracer).query_trace_graph(
            thread.thread_id,
            where=TraceGraphFilter(),
            cursor="not-a-trace-graph-cursor",
            limit=100,
        )
    assert invalid_cursor.value.error_code is ConversationErrorCode.INVALID_CURSOR


async def test_trace_graph_follow_sends_snapshot_update_and_closes(session) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-entry-follow",
        run_id="run-entry-follow",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-entry-follow",
    )
    events = await _service(repository, tracer=tracer).follow_trace_graph(
        thread.thread_id,
        where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
        limit=100,
    )

    snapshot = await anext(events)
    assert snapshot.type == "snapshot"
    assert snapshot.snapshot.nodes == ()
    assert snapshot.snapshot.turns == ()
    assert session.in_transaction() is False
    pending = asyncio.create_task(anext(events))
    await trace_session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="started",
            call_id="model-entry-follow",
            messages=(NativeMessageRecord(message_type="human", content="follow"),),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )

    update = await asyncio.wait_for(pending, timeout=2)
    assert update.type == "update"
    assert any(
        node.kind is TraceGraphNodeKind.MODEL
        and node.status is TraceGraphNodeStatus.RUNNING
        for node in update.update.node_upserts
    )
    assert len(update.update.turn_upserts) == 1
    model_node = next(
        node
        for node in update.update.node_upserts
        if node.kind is TraceGraphNodeKind.MODEL
    )
    assert model_node.turn_id == update.update.turn_upserts[0].id
    assert model_node.parent_subagent_id is None
    assert model_node.id in update.update.ordered_node_ids
    await events.aclose()
    await _finish_trace(context, trace_session)


async def test_history_cursor_keeps_original_as_of_after_new_turn(session) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
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


async def test_history_keeps_pending_interactions_outside_the_visible_turn(
    session,
) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-pending-history",
        run_id="run-pending-first",
    )
    first_context, first_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-pending-first",
    )
    await first_session.observe(
        NativeStateObservation(
            identity=first_context.identity,
            graph_namespace=(),
            state={},
            interrupts=(
                NativeInterruptRecord(
                    id="pending-outside-window",
                    value={"kind": "input_required", "message": "Wait"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish_trace(first_context, first_session, outcome="interrupted")
    await _register(
        repository,
        user_id=1,
        thread_id=thread.thread_id,
        run_id="run-latest-turn",
    )
    latest_context, latest_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-latest-turn",
    )
    await _finish_trace(latest_context, latest_session)

    detail = await _get_detail(
        _service(repository, tracer=tracer),
        thread.thread_id,
        limit=1,
    )

    assert detail.status.execution == "waiting"
    assert [
        item.source_id for item in detail.interactions if item.status == "pending"
    ] == ["pending-outside-window"]


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
            projections=(ConversationFailureProjection(),),
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
        projections=(ConversationFailureProjection(),),
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

    update = await asyncio.wait_for(pending, timeout=2)
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


async def test_trace_follow_closes_projector_when_disconnected_after_snapshot(
    session,
) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-follow-snapshot-close",
        run_id="run-follow-snapshot-close",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-follow-snapshot-close",
    )
    query = _CapturingTodoGroupQuery()
    events = await _service(
        repository,
        tracer=tracer,
        todo_group_query=query,
    ).follow_trace(thread.thread_id)

    assert (await anext(events)).type == "snapshot"
    await events.aclose()

    assert query.projector is not None
    with pytest.raises(RuntimeError, match="已关闭"):
        query.projector.snapshot(
            status=TraceStatus(
                execution="running", head_run_id=context.identity.run_id
            ),
            completeness=TraceCompleteness(),
        )
    await _finish_trace(context, trace_session)


async def test_trace_follow_replaces_task_trace_only_after_authoritative_state(
    session,
) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-follow-todos",
        run_id="run-follow-todos",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-follow-todos",
    )
    events = await _service(repository, tracer=tracer).follow_trace(thread.thread_id)
    initial = await anext(events)
    assert initial.type == "snapshot"
    assert initial.snapshot.task_trace is not None
    assert initial.snapshot.task_trace.todo_groups == ()

    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="assistant_chunk",
                id="assistant-todos",
                content="",
                tool_call_chunks=(
                    NativeToolCallChunk(
                        index=0,
                        id="call-write-todos",
                        name="write_todos",
                        arguments=(
                            '{"todos":[{"content":"实现投影","status":"in_progress"}]}'
                        ),
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="tool",
                id="tool-message-todos",
                name="write_todos",
                content="Updated todo list",
                tool_call_id="call-write-todos",
                tool_status="success",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await trace_session.observe(
        NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={"todos": [{"content": "实现投影", "status": "in_progress"}]},
            interrupts=(),
            observed_at=datetime.now(UTC),
            monotonic_ns=5,
        )
    )
    await trace_session.force(ObservationBoundary.TERMINAL)

    replacement = None
    for _index in range(5):
        update = await asyncio.wait_for(anext(events), timeout=2)
        assert update.type == "update"
        if update.task_trace is not None:
            replacement = update.task_trace
            break
    assert replacement is not None
    assert len(replacement.todo_groups) == 1
    assert replacement.todo_groups[0].todos[0].content == "实现投影"
    assert replacement.todo_groups[0].todos[0].status == "running"

    await trace_session.observe(
        NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={"todos": [{"content": "实现投影", "status": "completed"}]},
            interrupts=(),
            observed_at=datetime.now(UTC),
            monotonic_ns=6,
        )
    )
    await trace_session.force(ObservationBoundary.TERMINAL)
    completed = None
    for _index in range(5):
        update = await asyncio.wait_for(anext(events), timeout=2)
        assert update.type == "update"
        if update.task_trace is not None:
            completed = update.task_trace
            break
    assert completed is not None
    assert completed.todo_groups[0].todos[0].status == "completed"

    await events.aclose()
    await _finish_trace(context, trace_session)


async def test_detached_follow_can_skip_task_trace_without_losing_base_updates(
    session,
) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-follow-without-todos",
        run_id="run-follow-without-todos",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-follow-without-todos",
    )
    events = await _service(repository, tracer=tracer).follow_trace(
        thread.thread_id,
        include_task_trace=False,
    )

    initial = await anext(events)
    assert initial.type == "snapshot"
    assert initial.snapshot.task_trace is None
    pending = asyncio.create_task(anext(events))
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-without-todos",
                content="后台恢复",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    update = await asyncio.wait_for(pending, timeout=2)

    assert update.type == "update"
    assert update.update.messages.upserts[0].content == "后台恢复"
    assert update.task_trace is None
    await events.aclose()
    await _finish_trace(context, trace_session)


async def test_history_follow_publishes_ownership_without_fabricating_graph_events(
    session,
) -> None:
    """失活更新同时抵达公开摘要与任务视图，事件和 Graph 保持原有事实"""
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-follow-owner",
        run_id="run-follow-owner",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-follow-owner",
    )
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="assistant_chunk",
                id="assistant-owner",
                content="",
                tool_call_chunks=(
                    NativeToolCallChunk(
                        index=0,
                        id="todos-owner",
                        name="write_todos",
                        arguments='{"todos":[{"content":"等待任务","status":"in_progress"}]}',
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="tool",
                id="todos-result-owner",
                name="write_todos",
                tool_call_id="todos-owner",
                content="Updated todo list",
                tool_status="success",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await trace_session.observe(
        NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={"todos": [{"content": "等待任务", "status": "in_progress"}]},
            interrupts=(),
            observed_at=datetime.now(UTC),
            monotonic_ns=5,
        )
    )
    await trace_session.force(ObservationBoundary.TERMINAL)
    events = await _service(repository, tracer=tracer).follow_trace(
        thread.thread_id,
        include_task_trace=True,
    )
    try:
        initial = await anext(events)
        assert initial.type == "snapshot"
        await trace_session.aclose()
        update = await asyncio.wait_for(anext(events), 2)
        assert update.type == "update"
        assert update.update.as_of_seq == initial.snapshot.as_of_seq
        assert update.update.generation == initial.snapshot.generation
        assert update.update.observed_at >= initial.snapshot.observed_at
        assert update.update.status.execution == "unknown"
        assert update.update.events == ()
        assert update.update.graph.node_upserts == ()
        assert update.update.graph.turn_upserts == ()
        assert update.task_trace is not None
        assert update.task_trace.todo_groups[0].status == "failed"
        assert update.task_trace.todo_groups[0].todos[0].status == "failed"
    finally:
        await events.aclose()
        await trace_session.aclose()


async def test_history_route_returns_one_validated_json_body_with_task_trace(
    session,
) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-history-response",
        run_id="run-history-response",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-history-response",
    )
    await _finish_trace(context, trace_session)
    response = await get_history(
        thread.thread_id,
        _service(repository, tracer=tracer),
        history_cursor=None,
        limit=100,
        include_task_trace=True,
    )
    sent: list[AsgiMessage] = []

    async def receive() -> AsgiMessage:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: AsgiMessage) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/conversation/{thread.thread_id}/history",
        "raw_path": b"/api/conversation/thread/history",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }
    await response(
        scope,
        receive,
        send,
    )

    payload = json.loads(bytes(response.body))
    assert payload["code"] == 0
    assert payload["data"]["taskTrace"] == {
        "status": "ready",
        "todoGroups": [],
    }
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]


class _TraceRouteService:
    async def follow_trace(
        self,
        _thread_id: str,
        *,
        include_task_trace: bool,
    ):
        assert include_task_trace is True

        async def events():
            yield ConversationTraceErrorEvent()

        return events()


async def test_trace_route_serializes_one_complete_sse_frame() -> None:
    response = await follow_trace(
        "thread-trace-response",
        cast(ConversationHistoryService, _TraceRouteService()),
        include_task_trace=True,
    )

    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else bytes(chunk))

    assert b"".join(chunks) == (
        b'event: trace\ndata: {"type":"error","code":"trace_unavailable"}\n\n'
    )


async def test_failures_follow_the_history_window_and_fixed_prefix(session):
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    repository = ConversationRepository(session)
    for run_id in ("old-failed", "latest-failed"):
        await _register(
            repository, user_id=1, thread_id="failure-history", run_id=run_id
        )
        context, source = await _open_trace(
            tracer, thread_id="failure-history", run_id=run_id
        )
        await source.observe(
            RunTerminalObservation(
                identity=context.identity,
                outcome="failed",
                code="runtime_initialization_error",
                error_type="builtins.RuntimeError",
                observed_at=datetime.now(UTC),
                monotonic_ns=3,
            )
        )
        await source.aclose()
    service = _service(repository, tracer=tracer)
    current = await service.get_detail(
        "failure-history", limit=1, include_task_trace=False
    )
    assert [item.run_id for item in current.run_failures] == ["latest-failed"]
    assert current.run_failures[0].retryable
    assert current.history_cursor
    older = await service.get_detail(
        "failure-history",
        history_cursor=current.history_cursor,
        limit=1,
        include_task_trace=False,
    )
    assert {item.run_id for item in older.run_failures} == {
        "old-failed",
        "latest-failed",
    }
    assert all(message.role != "assistant" for message in older.messages)


async def test_live_failure_and_snapshot_have_identical_results(session):
    tracer = Tracer(projections=(ConversationFailureProjection(),))
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
    tracer = Tracer(projections=(ConversationFailureProjection(),))
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

    tracer = Tracer(projections=(ConversationFailureProjection(),))
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
            todo_group_query=TodoGroupQueryExecutor(),
            conversation_channel=channel,
        )
        try:
            # 未授权的会话和未登记运行均不得打开订阅
            other_user = ConversationHistoryService(
                repository,
                user_id=2,
                tracer=tracer,
                todo_group_query=TodoGroupQueryExecutor(),
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
