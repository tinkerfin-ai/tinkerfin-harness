"""会话任务轨迹边界与查询期投影测试"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import JsonValue, ValidationError

from tinkerfin_contracts import RunIdentity
from tinkerfin_contracts.media import Attachment
from tinkerfin_studio.conversation.todo_groups import (
    TaskTraceSnapshot,
    TodoGroup,
    TodoGroupProjection,
    TodoGroupProjectionResult,
    TodoGroupProjectionState,
    TodoTraceItem,
    render_task_trace,
)
from tinkerfin_tracing import (
    CapturedValue,
    MessageFact,
    RunFact,
    StateRevisionFact,
    ToolFact,
    TraceCompleteness,
    TraceEvent,
    TraceSemanticFact,
    TraceStatus,
    TurnFact,
)

_IDENTITY = RunIdentity(
    namespace="test", thread_id="thread:first-success", run_id="run-1"
)
_OCCURRED_AT = datetime(2026, 8, 30, 12, tzinfo=UTC)
_PROJECTION = TodoGroupProjection()


def _apply(
    state: TodoGroupProjectionState, fact: TraceSemanticFact
) -> TodoGroupProjectionState:
    updated = _PROJECTION.apply(state, fact)
    return TodoGroupProjectionState.model_validate_json(updated.model_dump_json())


def _snapshot_for(
    state: TodoGroupProjectionState,
    execution: Literal[
        "running", "waiting", "succeeded", "failed", "cancelled", "abandoned", "unknown"
    ] = "running",
    *,
    head_run_id: str = "run-1",
) -> TaskTraceSnapshot:
    return render_task_trace(
        _PROJECTION.finish(state),
        status=TraceStatus(execution=execution, head_run_id=head_run_id),
        completeness=TraceCompleteness(
            missing_prefix=False, missing_tail=False, payload_omitted=False
        ),
    )


def _capture(value: JsonValue) -> CapturedValue:
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(
            json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()
        ),
        value=value,
    )


def _confirmed_todo_facts() -> tuple[
    RunFact, TurnFact, MessageFact, ToolFact, ToolFact, StateRevisionFact
]:

    def occurred(seconds: int) -> datetime:
        return _OCCURRED_AT + timedelta(seconds=seconds)

    return (
        RunFact(
            source_observation_id="observation:run-started",
            identity=_IDENTITY,
            occurred_at=occurred(0),
            monotonic_ns=1,
            phase="started",
            input_kind="ordinary",
        ),
        TurnFact(
            source_observation_id="observation:turn-started",
            identity=_IDENTITY,
            occurred_at=occurred(1),
            monotonic_ns=2,
            turn_id="turn:run-1",
            user_message_id="source-user:run-1",
        ),
        MessageFact(
            source_observation_id="observation:user-message",
            identity=_IDENTITY,
            occurred_at=occurred(2),
            monotonic_ns=3,
            phase="reconciled",
            message_id="message:run-1",
            source_message_id="source-user:run-1",
            role="user",
            content=_capture("实现任务轨迹"),
        ),
        ToolFact(
            source_observation_id="observation:tool-started",
            identity=_IDENTITY,
            occurred_at=occurred(3),
            monotonic_ns=4,
            phase="started",
            tool_call_id="tool:root:call-1",
            source_tool_call_id="call-1",
            tool_name="write_todos",
        ),
        ToolFact(
            source_observation_id="observation:tool-result",
            identity=_IDENTITY,
            occurred_at=occurred(5),
            monotonic_ns=5,
            phase="result",
            tool_call_id="tool:root:call-1",
            source_tool_call_id="call-1",
            tool_name="write_todos",
            content=_capture("ok"),
            result_status="success",
        ),
        StateRevisionFact(
            source_observation_id="observation:state",
            identity=_IDENTITY,
            occurred_at=occurred(7),
            monotonic_ns=6,
            revision_id="revision:run-1",
            changes=_capture(
                {
                    "todos": [
                        {
                            "id": "todo-1",
                            "content": "实现 Server",
                            "status": "in_progress",
                        },
                        {"id": "todo-2", "content": "实现 Web", "status": "pending"},
                    ]
                }
            ),
        ),
    )


def _expected_projected_snapshot() -> TaskTraceSnapshot:
    return TaskTraceSnapshot.model_validate_json(
        json.dumps(
            {
                "status": "ready",
                "todoGroups": [
                    {
                        "id": "todo-group:run-1",
                        "userMessageId": "message:run-1",
                        "userMessagePreview": "实现任务轨迹",
                        "groupToolCallId": "tool:root:call-1",
                        "createdAt": "2026-08-30T12:00:03.000Z",
                        "status": "running",
                        "todos": [
                            {
                                "id": "todo-1",
                                "content": "实现 Server",
                                "status": "running",
                            },
                            {
                                "id": "todo-2",
                                "content": "实现 Web",
                                "status": "pending",
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )


def _group() -> TodoGroup:
    return TodoGroup(
        id="todo-group:run-1",
        user_message_id="message:user-1",
        user_message_preview="实现任务轨迹",
        group_tool_call_id="tool:write-todos-1",
        created_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        status="running",
        todos=(
            TodoTraceItem(id="todo-1", content="实现 Server 投影", status="running"),
        ),
    )


def test_task_trace_contract_uses_one_strict_camel_case_shape() -> None:
    snapshot = TaskTraceSnapshot(status="ready", todo_groups=(_group(),))
    assert snapshot.model_dump(mode="json", by_alias=True) == {
        "status": "ready",
        "todoGroups": [
            {
                "id": "todo-group:run-1",
                "userMessageId": "message:user-1",
                "userMessagePreview": "实现任务轨迹",
                "groupToolCallId": "tool:write-todos-1",
                "createdAt": "2026-08-30T12:00:00Z",
                "status": "running",
                "todos": [
                    {"id": "todo-1", "content": "实现 Server 投影", "status": "running"}
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ready", "todoGroups": [], "errorCode": "trace_incomplete"},
        {"status": "unavailable", "todoGroups": []},
        {
            "status": "unavailable",
            "todoGroups": [_group().model_dump(mode="json", by_alias=True)],
            "errorCode": "trace_incomplete",
        },
        {"status": "ready", "todoGroups": [], "version": 1},
    ],
)
def test_task_trace_contract_rejects_invalid_status_combinations(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TaskTraceSnapshot.model_validate(payload)


def test_unavailable_task_trace_is_distinct_from_an_empty_ready_trace() -> None:
    ready = TaskTraceSnapshot(status="ready", todo_groups=())
    unavailable = TaskTraceSnapshot(
        status="unavailable", todo_groups=(), error_code="todo_state_omitted"
    )
    assert ready != unavailable
    assert ready.error_code is None
    assert unavailable.error_code == "todo_state_omitted"


def test_projection_projects_confirmed_root_todos_deterministically() -> None:
    state = _PROJECTION.initial_state()
    repeated_state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        state = _apply(state, fact)
        repeated_state = _apply(repeated_state, fact.model_copy(deep=True))
    actual = _snapshot_for(state)
    repeated_actual = _snapshot_for(repeated_state)
    assert actual == _expected_projected_snapshot()
    assert actual.model_dump_json(by_alias=True) == repeated_actual.model_dump_json(
        by_alias=True
    )


def test_projection_updates_the_existing_group_without_changing_its_identity() -> None:
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        state = _apply(state, fact)
    state = _apply(
        state,
        StateRevisionFact(
            source_observation_id="observation:state-updated",
            identity=_IDENTITY,
            occurred_at=_OCCURRED_AT + timedelta(seconds=9),
            monotonic_ns=7,
            revision_id="revision:run-1:updated",
            changes=_capture(
                {
                    "todos": [
                        {
                            "id": "todo-1",
                            "content": "实现 Server",
                            "status": "completed",
                        },
                        {
                            "id": "todo-2",
                            "content": "实现 Web",
                            "status": "in_progress",
                        },
                    ]
                }
            ),
        ),
    )
    snapshot = _snapshot_for(state)
    assert len(snapshot.todo_groups) == 1
    group = snapshot.todo_groups[0]
    assert group.id == "todo-group:run-1"
    assert group.created_at == _OCCURRED_AT + timedelta(seconds=3)
    assert [todo.status for todo in group.todos] == ["completed", "running"]


@pytest.mark.parametrize(
    ("outcome", "execution", "group_status", "running_todo_status"),
    [
        ("succeeded", "succeeded", "incomplete", "incomplete"),
        ("failed", "failed", "failed", "failed"),
        ("cancelled", "cancelled", "cancelled", "cancelled"),
        ("abandoned", "abandoned", "cancelled", "cancelled"),
        ("interrupted", "waiting", "running", "running"),
    ],
)
def test_projection_maps_run_outcomes_without_losing_pending_todos(
    outcome: Literal["succeeded", "failed", "cancelled", "abandoned", "interrupted"],
    execution: Literal["succeeded", "failed", "cancelled", "abandoned", "waiting"],
    group_status: Literal["running", "incomplete", "failed", "cancelled"],
    running_todo_status: Literal["running", "incomplete", "failed", "cancelled"],
) -> None:
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        state = _apply(state, fact)
    state = _apply(
        state,
        RunFact(
            source_observation_id=f"observation:terminal:{outcome}",
            identity=_IDENTITY,
            occurred_at=_OCCURRED_AT + timedelta(seconds=9),
            monotonic_ns=7,
            phase="terminal",
            outcome=outcome,
        ),
    )
    group = _snapshot_for(state, execution).todo_groups[0]
    assert group.status == group_status
    assert [todo.status for todo in group.todos] == [running_todo_status, "pending"]


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
def test_compaction_preserves_prior_todos_and_allows_the_next_chat(
    outcome: Literal["succeeded", "failed", "cancelled"],
) -> None:
    state = _PROJECTION.initial_state()
    facts = _confirmed_todo_facts()
    for fact in facts:
        state = _apply(state, fact)
    state = _apply(
        state,
        RunFact(
            source_observation_id="chat-terminal",
            identity=_IDENTITY,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=7,
            phase="terminal",
            outcome="succeeded",
        ),
    )
    before = _snapshot_for(state, "succeeded")
    compact = _IDENTITY.model_copy(update={"run_id": "compact"})
    original_state = facts[-1]
    assert isinstance(original_state, StateRevisionFact)
    compact_facts = (
        RunFact(
            source_observation_id="compact-started",
            identity=compact,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=8,
            phase="started",
            input_kind="compaction",
        ),
        TurnFact(
            source_observation_id="compact-turn",
            identity=compact,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=9,
            turn_id="turn:compact",
        ),
        StateRevisionFact(
            source_observation_id="compact-state",
            identity=compact,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=10,
            revision_id="revision:compact",
            changes=original_state.changes,
        ),
    )
    for fact in compact_facts:
        state = _apply(state, fact)
    assert _snapshot_for(state, head_run_id="compact") == before
    state = _apply(
        state,
        RunFact(
            source_observation_id="compact-terminal",
            identity=compact,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=11,
            phase="terminal",
            outcome=outcome,
        ),
    )
    assert _snapshot_for(state, outcome, head_run_id="compact") == before
    next_run = _IDENTITY.model_copy(update={"run_id": "next"})
    next_facts = (
        RunFact(
            source_observation_id="next-started",
            identity=next_run,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=12,
            phase="started",
            input_kind="ordinary",
        ),
        TurnFact(
            source_observation_id="next-turn",
            identity=next_run,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=13,
            turn_id="turn:next",
            user_message_id="source-user:next",
        ),
        MessageFact(
            source_observation_id="next-user",
            identity=next_run,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=14,
            phase="reconciled",
            message_id="message:next",
            source_message_id="source-user:next",
            role="user",
            content=_capture("继续讨论实现"),
        ),
        RunFact(
            source_observation_id="next-terminal",
            identity=next_run,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=15,
            phase="terminal",
            outcome="succeeded",
        ),
    )
    for fact in next_facts:
        state = _apply(state, fact)
    assert _snapshot_for(state, "succeeded", head_run_id="next") == before


def test_projection_fails_closed_for_root_omission_invalid_todo_and_message_gap() -> (
    None
):
    facts = _confirmed_todo_facts()
    state_index = next(
        index
        for (index, fact) in enumerate(facts)
        if isinstance(fact, StateRevisionFact)
    )
    message_index = next(
        index
        for (index, fact) in enumerate(facts)
        if isinstance(fact, MessageFact) and fact.role == "user"
    )
    cases: list[tuple[int, CapturedValue, str]] = [
        (
            state_index,
            CapturedValue(
                disposition="omitted", safe_size_bytes=0, reason="capture_limit"
            ),
            "todo_state_omitted",
        ),
        (
            state_index,
            _capture(
                {
                    "todos": [
                        {"id": "todo-invalid", "content": "非法", "status": "failed"}
                    ]
                }
            ),
            "todo_state_invalid",
        ),
        (
            message_index,
            CapturedValue(
                disposition="omitted", safe_size_bytes=0, reason="capture_limit"
            ),
            "trace_incomplete",
        ),
    ]
    for index, replacement, expected_code in cases:
        state = _PROJECTION.initial_state()
        for fact_index, fact in enumerate(facts):
            if fact_index != index:
                state = _apply(state, fact)
                continue
            if isinstance(fact, StateRevisionFact):
                fact = fact.model_copy(update={"changes": replacement})
            else:
                assert isinstance(fact, MessageFact)
                fact = fact.model_copy(update={"content": replacement})
            state = _apply(state, fact)
        result = _snapshot_for(state)
        assert result.status == "unavailable"
        assert result.todo_groups == ()
        assert result.error_code == expected_code


def test_projection_ignores_omitted_subgraph_state() -> None:
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        state = _apply(state, fact)
    state = _apply(
        state,
        StateRevisionFact(
            source_observation_id="observation:subgraph-state",
            identity=_IDENTITY,
            graph_namespace=("tools:subagent",),
            occurred_at=_OCCURRED_AT + timedelta(seconds=8),
            monotonic_ns=7,
            revision_id="revision:subgraph",
            changes=CapturedValue(
                disposition="omitted", safe_size_bytes=0, reason="capture_limit"
            ),
        ),
    )
    assert _snapshot_for(state) == _expected_projected_snapshot()


@pytest.mark.parametrize(
    "outcome,execution,expected",
    [
        ("succeeded", "succeeded", "incomplete"),
        ("failed", "failed", "failed"),
        ("cancelled", "cancelled", "cancelled"),
        ("interrupted", "waiting", "running"),
    ],
)
def test_continuation_preserves_todo_group_and_applies_execution_outcome(
    outcome: Literal["succeeded", "failed", "cancelled", "interrupted"],
    execution: Literal["succeeded", "failed", "cancelled", "waiting"],
    expected: Literal["incomplete", "failed", "cancelled", "running"],
) -> None:
    """无新提问的续跑沿用原任务组，失败也不能被误当审批初始化失败忽略"""
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        state = _apply(state, fact)
    original = _snapshot_for(state).todo_groups[0]
    state = _apply(
        state,
        RunFact(
            source_observation_id="first-terminal",
            identity=_IDENTITY,
            occurred_at=_OCCURRED_AT + timedelta(seconds=8),
            monotonic_ns=7,
            phase="terminal",
            outcome="failed",
        ),
    )
    continued = _IDENTITY.model_copy(update={"run_id": "continued"})
    state = _apply(
        state,
        RunFact(
            source_observation_id="continued-start",
            identity=continued,
            occurred_at=_OCCURRED_AT + timedelta(seconds=9),
            monotonic_ns=8,
            phase="started",
            input_kind="continuation",
            parent_run_id=_IDENTITY.run_id,
        ),
    )
    state = _apply(
        state,
        RunFact(
            source_observation_id="continued-input",
            identity=continued,
            occurred_at=_OCCURRED_AT + timedelta(seconds=10),
            monotonic_ns=9,
            phase="resumed",
            input_kind="continuation",
            parent_run_id=_IDENTITY.run_id,
            input=_capture(None),
            config=_capture({}),
        ),
    )
    state = _apply(
        state,
        RunFact(
            source_observation_id="continued-terminal",
            identity=continued,
            occurred_at=_OCCURRED_AT + timedelta(seconds=11),
            monotonic_ns=10,
            phase="terminal",
            outcome=outcome,
        ),
    )
    view = render_task_trace(
        _PROJECTION.finish(state),
        status=TraceStatus(execution=execution, head_run_id="continued"),
        completeness=TraceCompleteness(
            missing_prefix=False, missing_tail=False, payload_omitted=False
        ),
    )
    assert view.status == "ready"
    assert len(view.todo_groups) == 1
    group = view.todo_groups[0]
    assert (group.id, group.user_message_id, group.group_tool_call_id) == (
        original.id,
        original.user_message_id,
        original.group_tool_call_id,
    )
    assert group.status == expected
    assert group.todos[1].status == "pending"


@pytest.mark.parametrize("batch_size", [1, 4, 1000])
@pytest.mark.parametrize("message_form", ["plain", "multimodal", "attachments"])
def test_multimodal_user_turn_keeps_completed_todo_group(
    message_form: str, batch_size: int
) -> None:
    values = json.loads(
        (Path(__file__).parent / "fixtures" / "todo-multimodal.json").read_text()
    )
    state = _PROJECTION.initial_state()
    for index, value in enumerate(values, start=1):
        fact = TraceEvent.model_validate_json(json.dumps(value)).fact
        if isinstance(fact, MessageFact) and fact.role == "user":
            content = fact.content
            assert content is not None and isinstance(content.value, list)
            if message_form == "plain":
                replacement = _capture("核对销售与库存，整理交付清单")
            elif message_form == "attachments":
                replacement = _capture(content.value[1:])
            else:
                replacement = content
            fact = fact.model_copy(update={"content": replacement})
        state = _apply(state, fact)
        if index % batch_size == 0 and index < len(values):
            prefix = render_task_trace(
                _PROJECTION.finish(state),
                status=TraceStatus(execution="running", head_run_id="run:multimodal"),
                completeness=TraceCompleteness(
                    missing_prefix=False, missing_tail=False, payload_omitted=False
                ),
            )
            assert prefix.status == "ready"
    result = render_task_trace(
        _PROJECTION.finish(state),
        status=TraceStatus(execution="succeeded", head_run_id="run:multimodal"),
        completeness=TraceCompleteness(
            missing_prefix=False, missing_tail=False, payload_omitted=False
        ),
    )
    assert result.status == "ready"
    assert len(result.todo_groups) == 1
    group = result.todo_groups[0]
    assert group.user_message_preview == (
        "销售表.xlsx, 库存.pdf, 说明.docx"
        if message_form == "attachments"
        else "核对销售与库存，整理交付清单"
    )
    assert group.id == "todo-group:run:multimodal"
    assert group.user_message_id == "message:user"
    assert group.group_tool_call_id == "tool:todos:1"
    assert group.status == "completed"
    assert len(group.todos) == 6
    assert all(todo.status == "completed" for todo in group.todos)
    expected = json.loads(
        (
            Path(__file__).parent / "fixtures" / "todo-multimodal-expected.json"
        ).read_text()
    )
    expected["todoGroups"][0]["userMessagePreview"] = group.user_message_preview
    assert result == TaskTraceSnapshot.model_validate_json(json.dumps(expected))


@pytest.mark.parametrize(
    "content",
    [
        None,
        {},
        [],
        [5],
        [{"type": []}],
        [{"type": "text", "text": 4}],
        [{"type": "non_standard", "metadata": {"name": "不作为用户标题"}}],
    ],
)
def test_todo_preview_rejects_content_without_visible_text_or_attachment(
    content: JsonValue,
) -> None:
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        if isinstance(fact, MessageFact) and fact.role == "user":
            fact = fact.model_copy(update={"content": _capture(content)})
        state = _apply(state, fact)
    assert _snapshot_for(state).error_code == "trace_incomplete"


def test_todo_preview_preserves_message_identity_and_visible_text_constraints() -> None:
    attachment = Attachment(
        id="attachment-1",
        name="不替代正文.pdf",
        mime_type="application/pdf",
        size_bytes=64,
    )
    content: JsonValue = [
        {"type": "text", "text": "核对 "},
        attachment.content_block(),
        {"type": "text", "text": "营收"},
    ]
    state = _PROJECTION.initial_state()
    message: MessageFact | None = None
    for fact in _confirmed_todo_facts():
        if isinstance(fact, MessageFact) and fact.role == "user":
            message = fact.model_copy(update={"content": _capture(content)})
            fact = message
        state = _apply(state, fact)
    assert _snapshot_for(state).todo_groups[0].user_message_preview == "核对 营收"
    assert message is not None
    state = _apply(state, message.model_copy(update={"content": _capture("不同正文")}))
    assert _snapshot_for(state).error_code == "trace_incomplete"


def test_todo_preview_rejects_a_conflicting_attachment_source() -> None:
    block = Attachment(
        id="file-1", name="财务.pdf", mime_type="application/pdf", size_bytes=3
    ).content_block()
    block["file_id"] = "another-file"
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        if isinstance(fact, MessageFact) and fact.role == "user":
            fact = fact.model_copy(update={"content": _capture([block])})
        state = _apply(state, fact)
    assert _snapshot_for(state).error_code == "trace_incomplete"


def test_projection_and_render_preserve_independent_serializable_results() -> None:
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        before = state.model_dump_json()
        updated = _apply(state, fact)
        assert state.model_dump_json() == before
        state = updated

    result = _PROJECTION.finish(state)
    restored = TodoGroupProjectionResult.model_validate_json(result.model_dump_json())
    complete = TraceCompleteness(
        missing_prefix=False, missing_tail=False, payload_omitted=False
    )
    cases: tuple[tuple[Literal["running", "unknown", "succeeded"], str], ...] = (
        ("running", "running"),
        ("unknown", "failed"),
        ("succeeded", "incomplete"),
        ("running", "running"),
    )
    for execution, expected_status in cases:
        rendered = render_task_trace(
            restored,
            status=TraceStatus(execution=execution, head_run_id="run-1"),
            completeness=complete,
        )
        assert rendered.todo_groups[0].status == expected_status
    assert restored == result

    result.groups[0].todos[0].content = "独立结果中的修改"
    assert _snapshot_for(state) == _expected_projected_snapshot()


@pytest.mark.parametrize("first_status", ["success", "error"])
def test_candidates_follow_tool_start_order_when_results_arrive_out_of_order(
    first_status: Literal["success", "error"],
) -> None:
    started, turn, message, first_call, first_result, todos = _confirmed_todo_facts()
    second_call = first_call.model_copy(
        update={"tool_call_id": "tool:root:call-2", "source_tool_call_id": "call-2"}
    )
    second_result = first_result.model_copy(
        update={"tool_call_id": "tool:root:call-2", "source_tool_call_id": "call-2"}
    )
    state = _PROJECTION.initial_state()
    for fact in (started, turn, message, first_call, second_call, todos, second_result):
        state = _apply(state, fact)
    assert _snapshot_for(state) == TaskTraceSnapshot(status="ready", todo_groups=())

    state = _apply(
        state, first_result.model_copy(update={"result_status": first_status})
    )
    group = _snapshot_for(state).todo_groups[0]
    assert group.group_tool_call_id == (
        first_call.tool_call_id
        if first_status == "success"
        else second_call.tool_call_id
    )


@pytest.mark.parametrize("boundary", ["assistant", "succeeded", "interrupted"])
def test_unchanged_todos_wait_for_a_confirmed_boundary(
    boundary: Literal["assistant", "succeeded", "interrupted"],
) -> None:
    started, turn, message, call, result, todos = _confirmed_todo_facts()
    state = _PROJECTION.initial_state()
    for fact in (started, turn, message, todos, call, result):
        state = _apply(state, fact)
    assert _snapshot_for(state).todo_groups == ()

    boundary_fact: TraceSemanticFact
    if boundary == "assistant":
        boundary_fact = message.model_copy(
            update={
                "role": "assistant",
                "message_id": "assistant",
                "source_message_id": "assistant",
            }
        )
    else:
        boundary_fact = RunFact(
            source_observation_id=f"terminal:{boundary}",
            identity=_IDENTITY,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=7,
            phase="terminal",
            outcome=boundary,
        )
    state = _apply(state, boundary_fact)
    assert _snapshot_for(state).todo_groups[0].group_tool_call_id == call.tool_call_id


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "abandoned"])
def test_failed_run_clears_unconfirmed_candidate(
    outcome: Literal["failed", "cancelled", "abandoned"],
) -> None:
    started, turn, message, call, result, todos = _confirmed_todo_facts()
    state = _PROJECTION.initial_state()
    for fact in (started, turn, message, todos, call, result):
        state = _apply(state, fact)
    state = _apply(
        state,
        RunFact(
            source_observation_id=f"terminal:{outcome}",
            identity=_IDENTITY,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=7,
            phase="terminal",
            outcome=outcome,
        ),
    )
    state = _apply(state, message.model_copy(update={"role": "assistant"}))
    assert _snapshot_for(state).todo_groups == ()


@pytest.mark.parametrize("removed", [False, True])
def test_empty_or_removed_todos_preserve_the_confirmed_group(removed: bool) -> None:
    facts = _confirmed_todo_facts()
    state = _PROJECTION.initial_state()
    for fact in facts:
        state = _apply(state, fact)
    state = _apply(
        state,
        facts[-1].model_copy(
            update={
                "changes": _capture({} if removed else {"todos": []}),
                "removed_keys": ("todos",) if removed else (),
            }
        ),
    )
    group = _snapshot_for(state, "succeeded").todo_groups[0]
    assert group.id == "todo-group:run-1"
    assert group.status == "completed"
    assert group.todos == ()


def test_empty_todos_do_not_create_a_group() -> None:
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        if isinstance(fact, StateRevisionFact):
            fact = fact.model_copy(update={"changes": _capture({"todos": []})})
        state = _apply(state, fact)
    assert _snapshot_for(state) == TaskTraceSnapshot(status="ready", todo_groups=())


@pytest.mark.parametrize("checkpointed", [False, True])
def test_resume_failure_only_ends_the_group_after_execution_is_checkpointed(
    checkpointed: bool,
) -> None:
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        state = _apply(state, fact)
    state = _apply(
        state,
        RunFact(
            source_observation_id="interrupted",
            identity=_IDENTITY,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=7,
            phase="terminal",
            outcome="interrupted",
        ),
    )
    resumed = _IDENTITY.model_copy(update={"run_id": "resumed"})
    state = _apply(
        state,
        RunFact(
            source_observation_id="resume-started",
            identity=resumed,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=8,
            phase="started",
            input_kind="resume",
            parent_run_id="run-1",
        ),
    )
    if checkpointed:
        state = _apply(
            state,
            RunFact(
                source_observation_id="resume-checkpointed",
                identity=resumed,
                occurred_at=_OCCURRED_AT,
                monotonic_ns=9,
                phase="resume_checkpointed",
                input_kind="resume",
                interrupt_ids=("review:todos",),
            ),
        )
    terminal = RunFact(
        source_observation_id="resume-failed",
        identity=resumed,
        occurred_at=_OCCURRED_AT,
        monotonic_ns=10,
        phase="terminal",
        outcome="failed",
    )
    state = _apply(state, terminal)
    state = _apply(state, terminal.model_copy(update={"phase": "closed"}))
    group = _snapshot_for(state, "failed", head_run_id="resumed").todo_groups[0]
    assert group.id == "todo-group:run-1"
    assert group.status == ("failed" if checkpointed else "running")
    assert group.todos[0].status == ("failed" if checkpointed else "running")


def test_missing_prefix_does_not_replace_the_fact_projection_result() -> None:
    state = _PROJECTION.initial_state()
    for fact in _confirmed_todo_facts():
        state = _apply(state, fact)
    result = _PROJECTION.finish(state)
    unavailable = render_task_trace(
        result,
        status=TraceStatus(execution="running", head_run_id="run-1"),
        completeness=TraceCompleteness(
            missing_prefix=True, missing_tail=False, payload_omitted=False
        ),
    )
    assert unavailable.error_code == "trace_incomplete"
    assert unavailable.todo_groups == ()
    assert _snapshot_for(state) == _expected_projected_snapshot()


@pytest.mark.parametrize(
    "todos",
    [
        None,
        {},
        [None],
        [{"content": " ", "status": "pending"}],
        [{"id": " bad-id ", "content": "任务", "status": "pending"}],
        [
            {"id": "duplicate", "content": "任务甲", "status": "pending"},
            {"id": "duplicate", "content": "任务乙", "status": "completed"},
        ],
    ],
)
def test_invalid_root_todos_keep_the_first_error(todos: JsonValue) -> None:
    facts = _confirmed_todo_facts()
    state = _PROJECTION.initial_state()
    for fact in facts[:-1]:
        state = _apply(state, fact)
    state = _apply(
        state, facts[-1].model_copy(update={"changes": _capture({"todos": todos})})
    )
    state = _apply(state, facts[-1])
    snapshot = _snapshot_for(state)
    assert snapshot.error_code == "todo_state_invalid"
    assert snapshot.todo_groups == ()


def test_todo_fact_requires_its_declared_run() -> None:
    state = _apply(_PROJECTION.initial_state(), _confirmed_todo_facts()[1])
    assert _snapshot_for(state).error_code == "trace_incomplete"


def test_candidate_requires_a_root_todo_revision_after_its_start() -> None:
    started, turn, message, call, result, todos = _confirmed_todo_facts()
    state = _PROJECTION.initial_state()
    for fact in (started, turn, message, todos, call, result):
        state = _apply(state, fact)
    unrelated = todos.model_copy(update={"changes": _capture({"files": {}})})
    nested = todos.model_copy(update={"graph_namespace": ("tools:subagent",)})
    for fact in (unrelated, nested):
        state = _apply(state, fact)
        assert _snapshot_for(state).todo_groups == ()
    state = _apply(state, todos)
    assert _snapshot_for(state).todo_groups[0].group_tool_call_id == call.tool_call_id


def test_branch_starts_its_own_group_and_preserves_parent_history() -> None:
    facts = _confirmed_todo_facts()
    state = _PROJECTION.initial_state()
    for fact in facts:
        state = _apply(state, fact)
    state = _apply(
        state,
        RunFact(
            source_observation_id="parent-terminal",
            identity=_IDENTITY,
            occurred_at=_OCCURRED_AT,
            monotonic_ns=7,
            phase="terminal",
            outcome="succeeded",
        ),
    )
    parent = _snapshot_for(state, "succeeded").todo_groups[0]
    branch = _IDENTITY.model_copy(update={"run_id": "branch"})
    branch_facts = (
        facts[0].model_copy(
            update={
                "identity": branch,
                "input_kind": "branch",
                "parent_run_id": "run-1",
            }
        ),
        facts[1].model_copy(
            update={
                "identity": branch,
                "turn_id": "turn:branch",
                "parent_run_id": "run-1",
                "user_message_id": "source:branch",
            }
        ),
        facts[2].model_copy(
            update={
                "identity": branch,
                "message_id": "message:branch",
                "source_message_id": "source:branch",
                "content": _capture("另一个任务"),
            }
        ),
        facts[3].model_copy(
            update={
                "identity": branch,
                "tool_call_id": "tool:branch",
                "source_tool_call_id": "call-branch",
                "occurred_at": _OCCURRED_AT + timedelta(seconds=20),
            }
        ),
        facts[4].model_copy(
            update={
                "identity": branch,
                "tool_call_id": "tool:branch",
                "source_tool_call_id": "call-branch",
            }
        ),
        facts[5].model_copy(update={"identity": branch}),
    )
    for fact in branch_facts:
        state = _apply(state, fact)
    groups = _snapshot_for(state, head_run_id="branch").todo_groups
    assert len(groups) == 2
    assert groups[0].id == "todo-group:branch"
    assert groups[0].user_message_preview == "另一个任务"
    assert groups[0].status == "running"
    assert groups[1] == parent
