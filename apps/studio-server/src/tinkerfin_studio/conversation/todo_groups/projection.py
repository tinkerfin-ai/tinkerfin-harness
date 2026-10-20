"""从公共 Trace 事实重建可缓存的会话任务分组"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tinkerfin_contracts import RunInputKind, RunTerminalOutcome
from tinkerfin_contracts.media import attachment_from_block
from tinkerfin_tracing import (
    MessageFact,
    RunFact,
    StateRevisionFact,
    ToolFact,
    TraceCompleteness,
    TraceSemanticFact,
    TraceStatus,
    TurnFact,
)

from .contracts import (
    TaskTraceErrorCode,
    TaskTraceSnapshot,
    TodoGroup,
    TodoGroupStatus,
    TodoTraceItem,
    TodoTraceItemStatus,
)

TODO_PROJECTION: Final = "studio.todo_groups"
_INPUT_TODO_STATUSES = frozenset({"pending", "in_progress", "completed"})


class _ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TodoValue(_ProjectionModel):
    id: str | None
    content: str
    status: Literal["pending", "in_progress", "completed"]


class _ToolCallState(_ProjectionModel):
    """保存根任务工具的轮次、开始时的任务修订及确认结果"""

    turn_id: str
    tool_call_id: str
    source_tool_call_id: str
    started_todo_revision: int
    created_at: datetime
    result: Literal["pending", "succeeded", "failed"] = "pending"


class _GroupState(_ProjectionModel):
    """一个提问已确认的任务组；在轮次中保留唯一记录"""

    tool_call_id: str
    created_at: datetime
    created_order: int
    todos: tuple[_TodoValue, ...]
    status: TodoGroupStatus = "running"


class _TurnState(_ProjectionModel):
    """通过稳定身份关联用户提问、候选工具与已确认任务组"""

    turn_id: str
    origin_run_id: str
    expected_user_message_source_id: str | None
    user_message_id: str | None = None
    user_message_preview: str | None = None
    message_error: bool = False
    root_todo_calls: list[str] = Field(default_factory=list)
    selected_candidate_tool_call_id: str | None = None
    group: _GroupState | None = None


class _RunState(_ProjectionModel):
    """保存运行归属及审批恢复是否已经进入执行的事实"""

    input_kind: RunInputKind
    parent_run_id: str | None
    turn_id: str | None
    resume_checkpointed: bool = False
    outcome: RunTerminalOutcome | None = None
    outcome_applied: bool = False


class TodoGroupProjectionState(_ProjectionModel):
    """供 Trace 投影保存的业务状态，不包含事件序号或传输生命周期

    工具开始时记录根任务修订号，后续根任务修订递增；这能区分工具前后的
    权威任务状态。工具和轮次使用身份关联，序列化后不依赖对象共享引用。
    """

    error_code: TaskTraceErrorCode | None = None
    runs: dict[str, _RunState] = Field(default_factory=dict)
    run_heads: set[str] = Field(default_factory=set)
    turns: dict[str, _TurnState] = Field(default_factory=dict)
    tool_calls: dict[str, _ToolCallState] = Field(default_factory=dict)
    group_count: int = 0
    latest_root_todos: tuple[_TodoValue, ...] = ()
    root_todo_revision: int = 0


class _GroupResult(_ProjectionModel):
    """保留任务原始状态，供同批运行状态校准展示结果"""

    id: str
    turn_id: str
    user_message_id: str
    user_message_preview: str
    group_tool_call_id: str
    created_at: datetime
    status: TodoGroupStatus
    todos: tuple[_TodoValue, ...]


class TodoGroupProjectionResult(_ProjectionModel):
    """仅由事实决定的任务分组，运行所有权状态由展示边界另行校准"""

    error_code: TaskTraceErrorCode | None = None
    groups: tuple[_GroupResult, ...] = ()
    run_turns: dict[str, str] = Field(default_factory=dict)
    uncheckpointed_failed_resumes: set[str] = Field(default_factory=set)


class TodoGroupProjection:
    """关联根任务工具、用户提问与运行结果，无 I/O 或关闭职责

    框架负责选定事实前缀、顺序、去重与缓存隔离。本投影只折叠业务事实，
    不把当前执行所有权状态写入可复用结果；调用方通过 render_task_trace
    使用同批 Trace 汇总构造展示结果。
    """

    name = TODO_PROJECTION
    state_type = TodoGroupProjectionState
    result_type = TodoGroupProjectionResult

    def initial_state(self) -> TodoGroupProjectionState:
        """返回一个尚未消费事实的独立状态"""

        return TodoGroupProjectionState()

    def apply(
        self, state: TodoGroupProjectionState, fact: TraceSemanticFact
    ) -> TodoGroupProjectionState:
        """消费一条业务相关事实并保留输入状态可独立使用"""

        if state.error_code is not None or not _affects_todos(state, fact):
            return state
        updated = state.model_copy(deep=True)
        if isinstance(fact, RunFact):
            _consume_run(updated, fact)
        elif isinstance(fact, TurnFact):
            _consume_turn(updated, fact)
        elif isinstance(fact, MessageFact):
            _consume_message(updated, fact)
        elif isinstance(fact, ToolFact):
            _consume_tool(updated, fact)
        elif isinstance(fact, StateRevisionFact):
            _consume_state(updated, fact)
        return updated

    def finish(self, state: TodoGroupProjectionState) -> TodoGroupProjectionResult:
        """构造不依赖当前运行所有权的独立任务分组结果"""

        if state.error_code is not None:
            return TodoGroupProjectionResult(error_code=state.error_code)
        grouped_turns = (
            (turn, turn.group)
            for turn in state.turns.values()
            if turn.group is not None
        )
        groups: list[_GroupResult] = []
        for turn, group in sorted(
            grouped_turns,
            key=lambda item: (item[1].created_at, item[1].created_order),
            reverse=True,
        ):
            if (
                turn.message_error
                or turn.user_message_id is None
                or turn.user_message_preview is None
            ):
                return TodoGroupProjectionResult(error_code="trace_incomplete")
            groups.append(
                _GroupResult(
                    id=f"todo-group:{turn.origin_run_id}",
                    turn_id=turn.turn_id,
                    user_message_id=turn.user_message_id,
                    user_message_preview=turn.user_message_preview,
                    group_tool_call_id=group.tool_call_id,
                    created_at=group.created_at,
                    status=group.status,
                    todos=tuple(todo.model_copy() for todo in group.todos),
                )
            )
        return TodoGroupProjectionResult(
            groups=tuple(groups),
            run_turns={
                run_id: run.turn_id
                for run_id, run in state.runs.items()
                if run.turn_id is not None
            },
            uncheckpointed_failed_resumes={
                run_id
                for run_id, run in state.runs.items()
                if run.input_kind == "resume"
                and run.outcome == "failed"
                and not run.resume_checkpointed
            },
        )


def render_task_trace(
    result: TodoGroupProjectionResult,
    *,
    status: TraceStatus,
    completeness: TraceCompleteness,
) -> TaskTraceSnapshot:
    """使用同批 Trace 状态校准任务展示，不修改事实投影

    Args:
        result: 与状态来自同一个固定事实前缀的业务投影结果
        status: 选定末端运行的当前执行状态，包括未增加事实的所有权丢失
        completeness: 同一事实前缀的结构与载荷完整性

    Returns:
        完整任务分组，或不含部分分组的不可用结果
    """

    if completeness.missing_prefix:
        return _unavailable("trace_incomplete")
    if result.error_code is not None:
        return _unavailable(result.error_code)
    head_turn_id = result.run_turns.get(status.head_run_id)
    skip_head_calibration = status.head_run_id in result.uncheckpointed_failed_resumes
    calibrated_status = _calibrated_group_status(status.execution)
    groups: list[TodoGroup] = []
    for group in result.groups:
        group_status = group.status
        if not skip_head_calibration and head_turn_id == group.turn_id:
            group_status = calibrated_status
        if group_status == "completed" and any(
            todo.status != "completed" for todo in group.todos
        ):
            group_status = "incomplete"
        groups.append(
            TodoGroup(
                id=group.id,
                user_message_id=group.user_message_id,
                user_message_preview=group.user_message_preview,
                group_tool_call_id=group.group_tool_call_id,
                created_at=group.created_at,
                status=group_status,
                todos=_public_todos(
                    group.todos, group_id=group.id, terminal_status=group_status
                ),
            )
        )
    return TaskTraceSnapshot(status="ready", todo_groups=tuple(groups))


def _affects_todos(state: TodoGroupProjectionState, fact: TraceSemanticFact) -> bool:
    if isinstance(fact, RunFact):
        return fact.identity.run_id not in state.runs or fact.phase in {
            "started",
            "resume_checkpointed",
            "terminal",
            "closed",
        }
    if isinstance(fact, TurnFact):
        return not fact.graph_namespace
    if isinstance(fact, ToolFact):
        return (
            not fact.graph_namespace
            and fact.tool_name == "write_todos"
            and fact.phase in {"started", "result"}
        )
    if isinstance(fact, StateRevisionFact):
        return not fact.graph_namespace and (
            fact.changes.disposition == "omitted"
            or not isinstance(fact.changes.value, dict)
            or "todos" in fact.changes.value
            or "todos" in fact.removed_keys
        )
    if not isinstance(fact, MessageFact) or fact.graph_namespace:
        return False
    turn = _turn_for_run(state, fact.identity.run_id)
    if turn is None:
        return False
    if fact.role == "assistant":
        return (
            fact.phase != "removed"
            and turn.group is None
            and turn.selected_candidate_tool_call_id is not None
            and bool(state.latest_root_todos)
        )
    return (
        fact.role == "user"
        and turn.expected_user_message_source_id is not None
        and fact.source_message_id == turn.expected_user_message_source_id
        and fact.phase in {"removed", "content", "reconciled"}
    )


def _consume_run(state: TodoGroupProjectionState, fact: RunFact) -> None:
    run_id = fact.identity.run_id
    if fact.phase == "started":
        _start_run(state, fact)
        return
    run = state.runs.get(run_id)
    if run is None:
        _fail(state, "trace_incomplete")
        return
    if fact.phase == "resume_checkpointed":
        if run.input_kind != "resume":
            _fail(state, "trace_incomplete")
            return
        run.resume_checkpointed = True
        return
    outcome = fact.outcome
    if outcome is None or (run.outcome is not None and run.outcome != outcome):
        _fail(state, "trace_incomplete")
        return
    run.outcome = outcome
    if run.outcome_applied:
        return
    run.outcome_applied = True
    turn = _turn_for_run(state, run_id)
    if turn is None:
        if run.input_kind in {"ordinary", "branch", "compaction"}:
            _fail(state, "trace_incomplete")
        return
    if (
        run.input_kind == "resume"
        and outcome == "failed"
        and not run.resume_checkpointed
    ):
        return
    if outcome in {"succeeded", "interrupted"}:
        _sync_candidate(state, turn)
    if outcome == "interrupted":
        return
    if turn.group is None:
        if outcome in {"failed", "cancelled", "abandoned"}:
            turn.selected_candidate_tool_call_id = None
        return
    if outcome == "succeeded":
        turn.group.status = "completed"
    elif outcome == "failed":
        turn.group.status = "failed"
    elif outcome in {"cancelled", "abandoned"}:
        turn.group.status = "cancelled"


def _start_run(state: TodoGroupProjectionState, fact: RunFact) -> None:
    run_id = fact.identity.run_id
    if run_id in state.runs or fact.input_kind is None:
        _fail(state, "trace_incomplete")
        return
    input_kind = fact.input_kind
    explicit_parent = fact.parent_run_id
    if explicit_parent is not None:
        parent = state.runs.get(explicit_parent)
        if parent is None or parent.outcome is None:
            _fail(state, "trace_incomplete")
            return
        resolved_parent = explicit_parent
    elif not state.runs:
        resolved_parent = None
    else:
        candidates = tuple(
            head for head in state.run_heads if state.runs[head].outcome is not None
        )
        if len(candidates) != 1:
            _fail(state, "trace_incomplete")
            return
        resolved_parent = candidates[0]
    inherited_turn = (
        _turn_for_run(state, resolved_parent)
        if input_kind in {"resume", "abandon", "continuation"}
        and resolved_parent is not None
        else None
    )
    if input_kind in {"resume", "abandon", "continuation"} and inherited_turn is None:
        _fail(state, "trace_incomplete")
        return
    state.runs[run_id] = _RunState(
        input_kind=input_kind,
        parent_run_id=resolved_parent,
        turn_id=inherited_turn.turn_id if inherited_turn is not None else None,
    )
    if resolved_parent is not None:
        state.run_heads.discard(resolved_parent)
    state.run_heads.add(run_id)


def _consume_turn(state: TodoGroupProjectionState, fact: TurnFact) -> None:
    run_id = fact.identity.run_id
    run = state.runs.get(run_id)
    if run is None or run.input_kind not in {"ordinary", "branch", "compaction"}:
        _fail(state, "trace_incomplete")
        return
    if run.turn_id is not None or fact.turn_id in state.turns:
        _fail(state, "trace_incomplete")
        return
    if fact.parent_run_id is not None and fact.parent_run_id != run.parent_run_id:
        _fail(state, "trace_incomplete")
        return
    # 压缩保留自己的轮次，不沿用上一轮任务组
    run.turn_id = fact.turn_id
    state.turns[fact.turn_id] = _TurnState(
        turn_id=fact.turn_id,
        origin_run_id=run_id,
        expected_user_message_source_id=fact.user_message_id,
    )


def _consume_message(state: TodoGroupProjectionState, fact: MessageFact) -> None:
    turn = _turn_for_run(state, fact.identity.run_id)
    if turn is None:
        return
    if fact.role == "assistant":
        _sync_candidate(state, turn)
        return
    if fact.phase == "removed":
        turn.message_error = True
        return
    if fact.content is None or fact.content.disposition != "inline":
        turn.message_error = True
        return
    preview = _message_preview(fact.content.value)
    if preview is None or (
        turn.user_message_id is not None
        and (
            turn.user_message_id != fact.message_id
            or turn.user_message_preview != preview
        )
    ):
        turn.message_error = True
        return
    turn.user_message_id = fact.message_id
    turn.user_message_preview = preview


def _consume_tool(state: TodoGroupProjectionState, fact: ToolFact) -> None:
    if fact.phase == "started":
        turn = _turn_for_run(state, fact.identity.run_id)
        if turn is None or fact.tool_call_id in state.tool_calls:
            _fail(state, "trace_incomplete")
            return
        call = _ToolCallState(
            turn_id=turn.turn_id,
            tool_call_id=fact.tool_call_id,
            source_tool_call_id=fact.source_tool_call_id,
            started_todo_revision=state.root_todo_revision,
            created_at=fact.occurred_at,
        )
        turn.root_todo_calls.append(call.tool_call_id)
        state.tool_calls[call.tool_call_id] = call
        return
    call = state.tool_calls.get(fact.tool_call_id)
    run = state.runs.get(fact.identity.run_id)
    if (
        call is None
        or call.source_tool_call_id != fact.source_tool_call_id
        or run is None
        or run.turn_id != call.turn_id
        or fact.result_status is None
    ):
        _fail(state, "trace_incomplete")
        return
    resolved = "succeeded" if fact.result_status == "success" else "failed"
    if call.result != "pending" and call.result != resolved:
        _fail(state, "trace_incomplete")
        return
    call.result = resolved
    _select_candidate(state, state.turns[call.turn_id])


def _consume_state(state: TodoGroupProjectionState, fact: StateRevisionFact) -> None:
    if fact.changes.disposition == "omitted":
        _fail(state, "todo_state_omitted")
        return
    changes = fact.changes.value
    if not isinstance(changes, dict):
        _fail(state, "todo_state_invalid")
        return
    has_todos = "todos" in changes
    removed_todos = "todos" in fact.removed_keys
    if has_todos and removed_todos:
        _fail(state, "todo_state_invalid")
        return
    todos = _parse_todos([] if removed_todos else changes["todos"])
    if todos is None:
        _fail(state, "todo_state_invalid")
        return
    state.latest_root_todos = todos
    state.root_todo_revision += 1
    turn = _turn_for_run(state, fact.identity.run_id)
    if turn is None:
        _fail(state, "trace_incomplete")
        return
    if turn.group is not None:
        turn.group.todos = todos
        return
    candidate = _selected_call(state, turn)
    if (
        candidate is not None
        and state.root_todo_revision > candidate.started_todo_revision
        and todos
    ):
        _create_group(state, turn, candidate, todos)


def _select_candidate(state: TodoGroupProjectionState, turn: _TurnState) -> None:
    if turn.selected_candidate_tool_call_id is not None:
        return
    for call_id in turn.root_todo_calls:
        call = state.tool_calls[call_id]
        if call.result == "failed":
            continue
        if call.result == "pending":
            return
        turn.selected_candidate_tool_call_id = call.tool_call_id
        if (
            state.root_todo_revision > call.started_todo_revision
            and state.latest_root_todos
        ):
            _create_group(state, turn, call, state.latest_root_todos)
        return


def _sync_candidate(state: TodoGroupProjectionState, turn: _TurnState) -> None:
    if turn.group is not None:
        return
    candidate = _selected_call(state, turn)
    # 相同任务写入不会产生状态修订，在助手消息或成功终态处使用最近权威根状态
    if candidate is not None and state.latest_root_todos:
        _create_group(state, turn, candidate, state.latest_root_todos)


def _selected_call(
    state: TodoGroupProjectionState, turn: _TurnState
) -> _ToolCallState | None:
    selected = turn.selected_candidate_tool_call_id
    return state.tool_calls.get(selected) if selected is not None else None


def _create_group(
    state: TodoGroupProjectionState,
    turn: _TurnState,
    call: _ToolCallState,
    todos: tuple[_TodoValue, ...],
) -> None:
    if turn.group is not None:
        turn.group.todos = todos
        return
    turn.group = _GroupState(
        tool_call_id=call.tool_call_id,
        created_at=call.created_at,
        created_order=state.group_count,
        todos=todos,
    )
    state.group_count += 1


def _is_input_todo_status(
    value: JsonValue,
) -> TypeGuard[Literal["pending", "in_progress", "completed"]]:
    return isinstance(value, str) and value in _INPUT_TODO_STATUSES


def _parse_todos(value: JsonValue) -> tuple[_TodoValue, ...] | None:
    if not isinstance(value, list):
        return None
    parsed: list[_TodoValue] = []
    identifiers: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            return None
        content = item.get("content")
        status = item.get("status")
        identifier = item.get("id")
        if (
            not isinstance(content, str)
            or not content.strip()
            or not _is_input_todo_status(status)
            or (
                identifier is not None
                and (
                    not isinstance(identifier, str)
                    or not identifier
                    or identifier != identifier.strip()
                )
            )
        ):
            return None
        if identifier is not None:
            if identifier in identifiers:
                return None
            identifiers.add(identifier)
        parsed.append(_TodoValue(id=identifier, content=content, status=status))
    return tuple(parsed)


def _turn_for_run(state: TodoGroupProjectionState, run_id: str) -> _TurnState | None:
    run = state.runs.get(run_id)
    return (
        state.turns.get(run.turn_id)
        if run is not None and run.turn_id is not None
        else None
    )


def _message_preview(content: JsonValue) -> str | None:
    if isinstance(content, str):
        visible = content
    elif isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif not isinstance(block, dict) or not isinstance(block.get("type"), str):
                return None
            elif block["type"] == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    return None
                texts.append(text)
        visible = "".join(texts)
        if not visible.strip():
            # 纯附件提问使用真实文件名作为任务组标题，不生成用户未发送的正文
            names: list[str] = []
            for block in content:
                try:
                    attachment = attachment_from_block(block)
                except ValueError:
                    return None
                if attachment is not None:
                    names.append(attachment.name)
            visible = ", ".join(names)
    else:
        return None
    normalized = " ".join(visible.split())
    if not normalized:
        return None
    return normalized if len(normalized) <= 160 else f"{normalized[:160]}…"


def _calibrated_group_status(
    execution: Literal[
        "running", "waiting", "succeeded", "failed", "cancelled", "abandoned", "unknown"
    ],
) -> TodoGroupStatus:
    if execution in {"running", "waiting"}:
        return "running"
    if execution == "succeeded":
        return "completed"
    if execution in {"failed", "unknown"}:
        return "failed"
    return "cancelled"


def _public_todos(
    todos: tuple[_TodoValue, ...],
    *,
    group_id: str,
    terminal_status: TodoGroupStatus,
) -> tuple[TodoTraceItem, ...]:
    result: list[TodoTraceItem] = []
    for index, todo in enumerate(todos):
        status: TodoTraceItemStatus = (
            "running" if todo.status == "in_progress" else todo.status
        )
        if status == "running" and terminal_status in {
            "failed",
            "cancelled",
            "incomplete",
        }:
            status = terminal_status
        result.append(
            TodoTraceItem(
                id=todo.id or f"{group_id}:todo:{index}",
                content=todo.content,
                status=status,
            )
        )
    return tuple(result)


def _fail(state: TodoGroupProjectionState, error_code: TaskTraceErrorCode) -> None:
    if state.error_code is None:
        state.error_code = error_code


def _unavailable(error_code: TaskTraceErrorCode) -> TaskTraceSnapshot:
    return TaskTraceSnapshot(
        status="unavailable", todo_groups=(), error_code=error_code
    )


__all__ = [
    "TODO_PROJECTION",
    "TodoGroupProjection",
    "TodoGroupProjectionResult",
    "TodoGroupProjectionState",
    "render_task_trace",
]
