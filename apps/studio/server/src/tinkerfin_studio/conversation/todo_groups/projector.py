"""从公共 Trace 事实查询期折叠会话任务轨迹"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypeGuard

from pydantic import JsonValue

from tinkerfin_contracts import RunInputKind
from tinkerfin_contracts.media import attachment_from_block
from tinkerfin_tracing import (
    MessageFact,
    RunFact,
    StateRevisionFact,
    ToolFact,
    TraceCompleteness,
    TraceEvent,
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

_INPUT_TODO_STATUSES = frozenset({"pending", "in_progress", "completed"})


def _is_input_todo_status(
    value: object,
) -> TypeGuard[Literal["pending", "in_progress", "completed"]]:
    return isinstance(value, str) and value in _INPUT_TODO_STATUSES


@dataclass(frozen=True, slots=True)
class _TodoValue:
    id: str | None
    content: str
    status: Literal["pending", "in_progress", "completed"]


@dataclass(slots=True, kw_only=True, eq=False)
class _ToolCallState:
    """一个工具调用的关联身份及结果"""

    tool_call_id: str
    source_tool_call_id: str
    started_trace_seq: int
    created_at: datetime
    result: Literal["pending", "succeeded", "failed"] = field(
        default="pending", init=False
    )


@dataclass(slots=True, kw_only=True, eq=False)
class _GroupState:
    """一次提问的任务组及最近任务状态"""

    turn_id: str
    tool_call_id: str
    created_at: datetime
    created_order: int
    todos: tuple[_TodoValue, ...]
    status: TodoGroupStatus = field(default="running", init=False)


@dataclass(slots=True, kw_only=True, eq=False)
class _TurnState:
    """用户提问与候选任务工具之间的关联状态"""

    turn_id: str
    origin_run_id: str
    expected_user_message_source_id: str | None
    user_message_id: str | None = field(default=None, init=False)
    user_message_preview: str | None = field(default=None, init=False)
    message_error: bool = field(default=False, init=False)
    root_todo_calls: list[_ToolCallState] = field(default_factory=list, init=False)
    selected_candidate_tool_call_id: str | None = field(default=None, init=False)
    group: _GroupState | None = field(default=None, init=False)


@dataclass(slots=True, kw_only=True, eq=False)
class _RunState:
    """普通提问或恢复运行在所属提问中的状态"""

    run_id: str
    input_kind: RunInputKind
    parent_run_id: str | None
    turn_id: str | None
    resume_checkpointed: bool = field(default=False, init=False)
    outcome: (
        Literal["succeeded", "interrupted", "failed", "cancelled", "abandoned"] | None
    ) = field(default=None, init=False)
    outcome_applied: bool = field(default=False, init=False)


class TodoGroupProjector:
    """按 Trace 顺序维护一个可丢弃的 Studio Todo Group 视图

    Projector 不拥有 Store，也不执行 I/O。调用方必须为一个固定 generation、
    fixed-as-of、已选 lineage 的事件序列创建独立实例，并在结束后调用
    :meth:`close` 释放中间关联状态。
    """

    def __init__(self) -> None:
        self._generation: str | None = None
        self._last_trace_seq = 0
        self._last_event: TraceEvent | None = None
        self._revision = 0
        self._error_code: TaskTraceErrorCode | None = None
        self._runs: dict[str, _RunState] = {}
        self._run_heads: set[str] = set()
        self._turns: dict[str, _TurnState] = {}
        self._run_to_turn: dict[str, str] = {}
        self._tool_calls: dict[str, tuple[str, _ToolCallState]] = {}
        self._groups: list[_GroupState] = []
        self._latest_root_todos: tuple[_TodoValue, ...] = ()
        self._latest_root_todo_revision_seq: int | None = None
        self._closed = False

    @property
    def last_trace_seq(self) -> int:
        """返回已消费的最后一个全局 Trace 序号"""

        return self._last_trace_seq

    @property
    def revision(self) -> int:
        """返回会改变公开任务轨迹结果的单调修订号"""

        return self._revision

    def consume(self, event: TraceEvent) -> None:
        """消费一个升序公共 TraceEvent

        Args:
            event: 固定 generation 与选定 lineage 中的下一条事件

        Raises:
            RuntimeError: Projector 已关闭
            TypeError: 输入不是公共 TraceEvent
        """

        if self._closed:
            raise RuntimeError("TodoGroupProjector 已关闭")
        if not isinstance(event, TraceEvent):
            raise TypeError("event 必须是 TraceEvent")
        if self._last_event is not None and event.trace_seq == self._last_trace_seq:
            if event == self._last_event:
                return
            self._fail("trace_incomplete")
            return
        if event.trace_seq < self._last_trace_seq:
            self._fail("trace_incomplete")
            return
        if self._generation is None:
            self._generation = event.generation
        elif event.generation != self._generation:
            self._fail("trace_incomplete")
            return

        self._last_trace_seq = event.trace_seq
        self._last_event = event
        if self._error_code is not None:
            return

        fact = event.fact
        if isinstance(fact, RunFact):
            self._consume_run(fact)
        elif isinstance(fact, TurnFact):
            self._consume_turn(fact)
        elif isinstance(fact, MessageFact):
            self._consume_message(fact)
        elif isinstance(fact, ToolFact):
            self._consume_tool(event, fact)
        elif isinstance(fact, StateRevisionFact):
            self._consume_state(event, fact)

    def snapshot(
        self,
        *,
        status: TraceStatus,
        completeness: TraceCompleteness,
    ) -> TaskTraceSnapshot:
        """构造当前完整结果并使用 Trace 汇总校准 head Turn

        Args:
            status: 同一 fixed-as-of 选定 head 的执行状态
            completeness: 同一 Trace 前缀的结构与载荷完整性

        Returns:
            完整 ready 结果或不包含部分分组的 unavailable 结果

        Raises:
            RuntimeError: Projector 已关闭
            TypeError: 汇总模型类型错误
        """

        if self._closed:
            raise RuntimeError("TodoGroupProjector 已关闭")
        if not isinstance(status, TraceStatus):
            raise TypeError("status 必须是 TraceStatus")
        if not isinstance(completeness, TraceCompleteness):
            raise TypeError("completeness 必须是 TraceCompleteness")
        if completeness.missing_prefix:
            return self._unavailable("trace_incomplete")
        if self._error_code is not None:
            return self._unavailable(self._error_code)

        head_turn_id = self._run_to_turn.get(status.head_run_id)
        head_run = self._runs.get(status.head_run_id)
        skip_head_calibration = bool(
            head_run is not None
            and head_run.input_kind == "resume"
            and head_run.outcome == "failed"
            and not head_run.resume_checkpointed
        )
        calibrated_status = self._calibrated_group_status(status.execution)
        groups: list[TodoGroup] = []
        for group_state in sorted(
            self._groups,
            key=lambda group: (group.created_at, group.created_order),
            reverse=True,
        ):
            turn = self._turns[group_state.turn_id]
            if (
                turn.message_error
                or turn.user_message_id is None
                or turn.user_message_preview is None
            ):
                return self._unavailable("trace_incomplete")
            group_status = group_state.status
            if (
                not skip_head_calibration
                and head_turn_id == group_state.turn_id
                and calibrated_status is not None
            ):
                group_status = calibrated_status
            if group_status == "completed" and any(
                todo.status != "completed" for todo in group_state.todos
            ):
                group_status = "incomplete"
            groups.append(
                TodoGroup(
                    id=self._group_id(turn),
                    user_message_id=turn.user_message_id,
                    user_message_preview=turn.user_message_preview,
                    group_tool_call_id=group_state.tool_call_id,
                    created_at=group_state.created_at,
                    status=group_status,
                    todos=self._public_todos(
                        group_state.todos,
                        group_id=self._group_id(turn),
                        terminal_status=group_status,
                    ),
                )
            )
        return TaskTraceSnapshot(status="ready", todo_groups=tuple(groups))

    def close(self) -> None:
        """释放查询完成后不应进入响应编码阶段的关联状态"""

        if self._closed:
            return
        self._runs.clear()
        self._run_heads.clear()
        self._turns.clear()
        self._run_to_turn.clear()
        self._tool_calls.clear()
        self._groups.clear()
        self._latest_root_todos = ()
        self._latest_root_todo_revision_seq = None
        self._last_event = None
        self._closed = True

    def _consume_run(self, fact: RunFact) -> None:
        run_id = fact.identity.run_id
        if fact.phase == "started":
            self._start_run(fact)
            return
        run = self._runs.get(run_id)
        if run is None:
            self._fail("trace_incomplete")
            return
        if fact.phase == "resume_checkpointed":
            if run.input_kind != "resume":
                self._fail("trace_incomplete")
                return
            run.resume_checkpointed = True
            return
        if fact.phase not in {"terminal", "closed"}:
            return
        outcome = fact.outcome
        if outcome is None:
            self._fail("trace_incomplete")
            return
        if run.outcome is not None and run.outcome != outcome:
            self._fail("trace_incomplete")
            return
        run.outcome = outcome
        if run.outcome_applied:
            return
        run.outcome_applied = True
        turn = self._turn_for_run(run_id)
        if turn is None:
            if run.input_kind in {"ordinary", "branch"}:
                self._fail("trace_incomplete")
            return
        initialization_failed = (
            run.input_kind == "resume"
            and outcome == "failed"
            and not run.resume_checkpointed
        )
        if initialization_failed:
            return
        if outcome in {"succeeded", "interrupted"}:
            self._sync_candidate(turn)
        if outcome == "interrupted":
            return
        if turn.group is None:
            if outcome in {"failed", "cancelled", "abandoned"}:
                turn.selected_candidate_tool_call_id = None
            return
        if outcome == "succeeded":
            self._set_group_status(turn.group, "completed")
        elif outcome == "failed":
            self._set_group_status(turn.group, "failed")
        elif outcome in {"cancelled", "abandoned"}:
            self._set_group_status(turn.group, "cancelled")

    def _start_run(self, fact: RunFact) -> None:
        run_id = fact.identity.run_id
        if run_id in self._runs or fact.input_kind is None:
            self._fail("trace_incomplete")
            return
        input_kind = fact.input_kind
        explicit_parent = fact.parent_run_id
        if explicit_parent is not None:
            parent = self._runs.get(explicit_parent)
            if parent is None or parent.outcome is None:
                self._fail("trace_incomplete")
                return
            resolved_parent = explicit_parent
        elif not self._runs:
            resolved_parent = None
        else:
            candidates = tuple(
                run_id
                for run_id in self._run_heads
                if self._runs[run_id].outcome is not None
            )
            if len(candidates) != 1:
                self._fail("trace_incomplete")
                return
            resolved_parent = candidates[0]
        if (
            input_kind in {"resume", "abandon", "continuation"}
            and resolved_parent is None
        ):
            self._fail("trace_incomplete")
            return
        inherited_turn_id = (
            self._run_to_turn.get(resolved_parent)
            if input_kind in {"resume", "abandon", "continuation"}
            and resolved_parent is not None
            else None
        )
        if (
            input_kind in {"resume", "abandon", "continuation"}
            and inherited_turn_id is None
        ):
            self._fail("trace_incomplete")
            return
        run = _RunState(
            run_id=run_id,
            input_kind=input_kind,
            parent_run_id=resolved_parent,
            turn_id=inherited_turn_id,
        )
        self._runs[run_id] = run
        if inherited_turn_id is not None:
            self._run_to_turn[run_id] = inherited_turn_id
        if resolved_parent is not None:
            self._run_heads.discard(resolved_parent)
        self._run_heads.add(run_id)

    def _consume_turn(self, fact: TurnFact) -> None:
        if fact.graph_namespace:
            return
        run_id = fact.identity.run_id
        run = self._runs.get(run_id)
        if run is None or run.input_kind not in {"ordinary", "branch"}:
            self._fail("trace_incomplete")
            return
        if run.turn_id is not None or fact.turn_id in self._turns:
            self._fail("trace_incomplete")
            return
        if fact.parent_run_id is not None and fact.parent_run_id != run.parent_run_id:
            self._fail("trace_incomplete")
            return
        turn = _TurnState(
            turn_id=fact.turn_id,
            origin_run_id=run_id,
            expected_user_message_source_id=fact.user_message_id,
        )
        run.turn_id = fact.turn_id
        self._turns[fact.turn_id] = turn
        self._run_to_turn[run_id] = fact.turn_id

    def _consume_message(self, fact: MessageFact) -> None:
        if fact.graph_namespace:
            return
        turn = self._turn_for_run(fact.identity.run_id)
        if turn is None:
            return
        if fact.role == "assistant" and fact.phase != "removed":
            self._sync_candidate(turn)
        if fact.role != "user":
            return
        expected_source = turn.expected_user_message_source_id
        if expected_source is None or fact.source_message_id != expected_source:
            return
        if fact.phase == "removed":
            turn.message_error = True
            return
        if fact.phase not in {"content", "reconciled"}:
            return
        if fact.content is None or fact.content.disposition != "inline":
            turn.message_error = True
            return
        preview = self._message_preview(fact.content.value)
        if preview is None:
            turn.message_error = True
            return
        if turn.user_message_id is not None and (
            turn.user_message_id != fact.message_id
            or turn.user_message_preview != preview
        ):
            turn.message_error = True
            return
        turn.user_message_id = fact.message_id
        turn.user_message_preview = preview

    def _consume_tool(self, event: TraceEvent, fact: ToolFact) -> None:
        if fact.graph_namespace or fact.tool_name != "write_todos":
            return
        if fact.phase == "started":
            turn = self._turn_for_run(fact.identity.run_id)
            if turn is None or fact.tool_call_id in self._tool_calls:
                self._fail("trace_incomplete")
                return
            call = _ToolCallState(
                tool_call_id=fact.tool_call_id,
                source_tool_call_id=fact.source_tool_call_id,
                started_trace_seq=event.trace_seq,
                created_at=fact.occurred_at,
            )
            turn.root_todo_calls.append(call)
            self._tool_calls[fact.tool_call_id] = (turn.turn_id, call)
            return
        if fact.phase != "result":
            return
        registered = self._tool_calls.get(fact.tool_call_id)
        if registered is None:
            self._fail("trace_incomplete")
            return
        turn_id, call = registered
        if (
            call.source_tool_call_id != fact.source_tool_call_id
            or self._run_to_turn.get(fact.identity.run_id) != turn_id
            or fact.result_status is None
        ):
            self._fail("trace_incomplete")
            return
        resolved = "succeeded" if fact.result_status == "success" else "failed"
        if call.result != "pending" and call.result != resolved:
            self._fail("trace_incomplete")
            return
        call.result = resolved
        self._select_candidate(self._turns[turn_id])

    def _consume_state(self, event: TraceEvent, fact: StateRevisionFact) -> None:
        if fact.graph_namespace:
            return
        if fact.changes.disposition == "omitted":
            self._fail("todo_state_omitted")
            return
        changes = fact.changes.value
        if not isinstance(changes, dict):
            self._fail("todo_state_invalid")
            return
        has_todos = "todos" in changes
        removed_todos = "todos" in fact.removed_keys
        if has_todos and removed_todos:
            self._fail("todo_state_invalid")
            return
        if not has_todos and not removed_todos:
            return
        todos = self._parse_todos([] if removed_todos else changes["todos"])
        if todos is None:
            return
        self._latest_root_todos = todos
        self._latest_root_todo_revision_seq = event.trace_seq
        turn = self._turn_for_run(fact.identity.run_id)
        if turn is None:
            self._fail("trace_incomplete")
            return
        if turn.group is not None:
            if turn.group.todos != todos:
                turn.group.todos = todos
                self._revision += 1
            return
        candidate = self._selected_call(turn)
        if (
            candidate is not None
            and event.trace_seq > candidate.started_trace_seq
            and todos
        ):
            self._create_group(turn, candidate, todos)

    def _select_candidate(self, turn: _TurnState) -> None:
        if turn.selected_candidate_tool_call_id is not None:
            return
        for call in turn.root_todo_calls:
            if call.result == "failed":
                continue
            if call.result == "pending":
                return
            turn.selected_candidate_tool_call_id = call.tool_call_id
            if (
                self._latest_root_todo_revision_seq is not None
                and self._latest_root_todo_revision_seq > call.started_trace_seq
                and self._latest_root_todos
            ):
                self._create_group(turn, call, self._latest_root_todos)
            return

    def _sync_candidate(self, turn: _TurnState) -> None:
        if turn.group is not None:
            return
        candidate = self._selected_call(turn)
        # 成功写入与当前 todos 完全相同时不会产生 StateRevision；安全边界使用最近权威根状态
        if candidate is not None and self._latest_root_todos:
            self._create_group(turn, candidate, self._latest_root_todos)

    def _selected_call(self, turn: _TurnState) -> _ToolCallState | None:
        selected = turn.selected_candidate_tool_call_id
        if selected is None:
            return None
        registered = self._tool_calls.get(selected)
        return registered[1] if registered is not None else None

    def _create_group(
        self,
        turn: _TurnState,
        call: _ToolCallState,
        todos: tuple[_TodoValue, ...],
    ) -> None:
        if turn.group is not None:
            if turn.group.todos != todos:
                turn.group.todos = todos
                self._revision += 1
            return
        group = _GroupState(
            turn_id=turn.turn_id,
            tool_call_id=call.tool_call_id,
            created_at=call.created_at,
            created_order=len(self._groups),
            todos=todos,
        )
        turn.group = group
        self._groups.append(group)
        self._revision += 1

    def _parse_todos(self, value: object) -> tuple[_TodoValue, ...] | None:
        if not isinstance(value, list):
            self._fail("todo_state_invalid")
            return None
        parsed: list[_TodoValue] = []
        identifiers: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                self._fail("todo_state_invalid")
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
                self._fail("todo_state_invalid")
                return None
            if identifier is not None:
                if identifier in identifiers:
                    self._fail("todo_state_invalid")
                    return None
                identifiers.add(identifier)
            parsed.append(
                _TodoValue(
                    id=identifier,
                    content=content,
                    status=status,
                )
            )
        return tuple(parsed)

    def _turn_for_run(self, run_id: str) -> _TurnState | None:
        turn_id = self._run_to_turn.get(run_id)
        return self._turns.get(turn_id) if turn_id is not None else None

    @staticmethod
    def _message_preview(content: JsonValue) -> str | None:
        if isinstance(content, str):
            visible = content
        elif isinstance(content, list):
            texts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    texts.append(block)
                elif not isinstance(block, dict) or not isinstance(
                    block.get("type"), str
                ):
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

    @staticmethod
    def _group_id(turn: _TurnState) -> str:
        return f"todo-group:{turn.origin_run_id}"

    @staticmethod
    def _calibrated_group_status(
        execution: Literal[
            "running",
            "waiting",
            "succeeded",
            "failed",
            "cancelled",
            "abandoned",
            "unknown",
        ],
    ) -> TodoGroupStatus | None:
        if execution in {"running", "waiting"}:
            return "running"
        if execution == "succeeded":
            return "completed"
        if execution in {"failed", "unknown"}:
            return "failed"
        if execution in {"cancelled", "abandoned"}:
            return "cancelled"
        return None

    @staticmethod
    def _public_todos(
        todos: tuple[_TodoValue, ...],
        *,
        group_id: str,
        terminal_status: TodoGroupStatus,
    ) -> tuple[TodoTraceItem, ...]:
        result: list[TodoTraceItem] = []
        for index, todo in enumerate(todos):
            status: TodoTraceItemStatus
            if todo.status == "in_progress":
                status = "running"
            else:
                status = todo.status
            if status == "running" and terminal_status == "failed":
                status = "failed"
            elif status == "running" and terminal_status == "cancelled":
                status = "cancelled"
            elif status == "running" and terminal_status == "incomplete":
                status = "incomplete"
            result.append(
                TodoTraceItem(
                    id=todo.id or f"{group_id}:todo:{index}",
                    content=todo.content,
                    status=status,
                )
            )
        return tuple(result)

    def _fail(self, error_code: TaskTraceErrorCode) -> None:
        if self._error_code is None:
            self._error_code = error_code
            self._revision += 1

    def _set_group_status(
        self,
        group: _GroupState,
        status: TodoGroupStatus,
    ) -> None:
        if group.status == status:
            return
        group.status = status
        self._revision += 1

    @staticmethod
    def _unavailable(error_code: TaskTraceErrorCode) -> TaskTraceSnapshot:
        return TaskTraceSnapshot(
            status="unavailable",
            todo_groups=(),
            error_code=error_code,
        )


__all__ = ["TodoGroupProjector"]
