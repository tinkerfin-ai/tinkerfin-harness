"""会话任务轨迹边界与查询期投影测试"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.conversation.todo_groups import (
    TaskTraceSnapshot,
    TodoGroupProjection,
    TodoGroupProjectionState,
    render_task_trace,
)
from tinkerfin_tracing import (
    CapturedValue,
    MessageFact,
    RunFact,
    StateRevisionFact,
    ToolFact,
    TraceCompleteness,
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
