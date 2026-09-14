"""Validated stream contracts, correlation state, and HITL preparation."""

from __future__ import annotations

__all__ = [
    "_buffer_child_interrupts",
    "_group_prepared_interrupts",
    "_interrupt_value_json",
    "_prepare_ag_ui_interrupts",
    "_prepare_root_interrupts",
    "_record_interrupts",
    "_tool_call_id_groups_for_actions",
    "_validate_prepared_interrupts",
]

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from ag_ui.core import AssistantMessage as AgUiAssistantMessage
from ag_ui.core import Interrupt as AgUiInterrupt
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    ValidationError,
)
from pydantic.alias_generators import to_camel

from tinkerfin_native_stream import NativeValuesStreamPart

from .errors import HitlCorrelationError, HitlNoMatchError
from .hitl import (
    HitlActionRequest,
    HitlRequest,
    HitlToolCallCandidate,
    match_hitl_action_groups,
    match_hitl_tool_call_id_groups,
    relevant_malformed_hitl_candidate,
)
from .interrupt_projection import project_interrupt
from .models import AgentRuntimeInterrupt, JsonObject, to_json_value
from .reasoning import (
    json_values_equal,
    normalize_operational_data,
)
from .runtime_interrupts import (
    RuntimeInterruptEnvelope,
    parse_runtime_interrupt,
)
from .subagent import SubagentProvenance

if TYPE_CHECKING:
    from .adapter import DeepAgentAgUiAdapter

_MESSAGE_STATE_KEY = "messages"
StreamMode = Literal[
    "values",
    "updates",
    "checkpoints",
    "tasks",
    "debug",
    "messages",
    "custom",
]


class _ProtocolModel(BaseModel):
    """Provide shared validation behavior at the protocol boundary."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
    )


def _to_json_value(value: object) -> JsonValue:
    """Convert framework objects into values accepted by Pydantic `JsonValue`."""

    return to_json_value(value)


class NativeToolCall(_ProtocolModel):
    """Native Tool call projected from `tasks.data.input`."""

    name: str = Field(min_length=1, description="Tool name")
    args: JsonObject = Field(
        description="Tool arguments aggregated and validated as a JSON object"
    )
    id: str = Field(min_length=1, description="Model-generated Tool call ID")
    type: Literal["tool_call"] = Field(description="LangChain Tool call type")


class NativeToolCallList(RootModel[list[NativeToolCall]]):
    """Tool calls carried by one ToolNode task-start payload."""


class AgentSource(_ProtocolModel):
    """Graph-scope identity attached to converted AG-UI events."""

    kind: Literal["root", "compiled_subgraph", "deep_agent_subagent"] = Field(
        description="Verified source kind for the complete graph namespace"
    )
    graph_namespace: tuple[str, ...] = Field(
        description="Complete root or subgraph namespace"
    )
    parent_graph_namespace: tuple[str, ...] | None = Field(
        default=None,
        description="Parent namespace that started this compiled graph task",
    )
    graph_task_id: str | None = Field(
        default=None, description="Full LangGraph task ID that opened this scope"
    )
    node_name: str | None = Field(
        default=None, description="Graph node whose task opened this scope"
    )
    agent_type: Literal["main", "subagent"] | None = Field(
        default=None,
        description="Agent role only for the root or a verified Deep Agents delegate",
    )
    agent_name: str | None = Field(
        default=None,
        min_length=1,
        description="Agent name only for the root or a verified Deep Agents delegate",
    )
    parent_tool_call_id: str | None = Field(
        default=None,
        min_length=1,
        description="Scoped parent Tool call ID for a Deep Agents delegate",
    )
    subagent_input: str | None = Field(
        default=None,
        description="Complete task description supplied to a Deep Agents delegate",
    )
    subagent_invocation_id: str | None = Field(
        default=None,
        min_length=1,
        description="Logical subagent invocation ID stable across resume",
    )


class EventContext(_ProtocolModel):
    """Serializable graph provenance written to AG-UI `rawEvent`."""

    stream_mode: StreamMode = Field(description="Native stream mode for this event")
    source: AgentSource = Field(description="Event source identity")
    run_id: str = Field(min_length=1, description="Main AG-UI run ID for the request")
    related_graph_namespace: tuple[str, ...] | None = Field(
        default=None, description="Complete native namespace related to the event"
    )
    related_subagent_invocation_id: str | None = Field(
        default=None,
        min_length=1,
        description="Logical subagent invocation completed by this event",
    )
    parent_tool_call_id: str | None = Field(
        default=None,
        min_length=1,
        description="Parent task Tool call ID that started the current subagent",
    )
    langgraph_node: str | None = Field(
        default=None, description="LangGraph node associated with the event"
    )
    interrupt_id: str | None = Field(
        default=None, description="Interrupt ID when the event concerns a review"
    )
    tool_result_status: Literal["success", "error"] | None = Field(
        default=None,
        description="Explicit Tool execution status from ToolMessage",
    )


@dataclass(slots=True)
class ActiveToolCall:
    """Track Tool identity and proposal order independently of provider indices.

    Unindexed calls retain first-seen proposal order for checkpoint-free approval
    matching. Their identities always come from Tool IDs, never this ordering.
    """

    tool_call_id: str
    tool_name: str
    parent_message_id: str | None
    namespace: tuple[str, ...]
    index: int | None
    order: int
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class ActiveReasoning:
    """Correlation state for one visible reasoning stream."""

    run_id: str
    source_message_id: str
    reasoning_id: str
    message_id: str
    namespace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubagentInvocation:
    """Subagent correlation established by a native task-start part."""

    parent_namespace: tuple[str, ...]
    child_namespace: tuple[str, ...]
    graph_task_id: str
    parent_tool_call_id: str
    subagent_input: str
    agent_name: str
    provenance: SubagentProvenance


@dataclass(frozen=True, slots=True)
class GraphScope:
    """Native compiled-graph scope established by a parent task start."""

    namespace: tuple[str, ...]
    parent_namespace: tuple[str, ...]
    graph_task_id: str
    node_name: str


@dataclass(frozen=True, slots=True)
class BufferedChildInterrupt:
    """Validated child interrupt awaiting an identical root propagation."""

    native_id: str
    value_json: str
    prepared: tuple[AgUiInterrupt, ...]
    namespaces: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class TaskStartFingerprint:
    """Normalized identity of one native task-start within its graph scope."""

    name: str
    input_json: str
    triggers: tuple[str, ...]
    metadata_json: str


@dataclass(frozen=True, slots=True)
class TaskResultFingerprint:
    """Canonical identity of one native task-result within its graph scope."""

    name: str
    error_json: str
    interrupts_json: str
    result_json: str


@dataclass(frozen=True, slots=True)
class ToolResultFingerprint:
    """Stable deduplication fingerprint for a published Tool result."""

    message_id: str
    tool_name: str
    content: str
    status: Literal["success", "error"]


class JsonPatchOperation(_ProtocolModel):
    """One RFC 6902 operation emitted in an AG-UI `STATE_DELTA`."""

    op: Literal["add", "remove", "replace"] = Field(description="Patch operation")
    path: str = Field(description="JSON Pointer path")
    value: JsonValue | None = Field(
        default=None, description="New value for add or replace operations"
    )


def _interrupt_value_json(interrupt: AgentRuntimeInterrupt) -> str:
    """Return a strict finite fingerprint for propagation comparisons."""

    return json.dumps(
        _to_json_value(interrupt.value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _group_prepared_interrupts(
    native_interrupts: Sequence[AgentRuntimeInterrupt],
    prepared: Sequence[AgUiInterrupt],
) -> tuple[tuple[AgUiInterrupt, ...], ...]:
    """Restore native interrupt groups after public multi-action expansion."""

    grouped: list[tuple[AgUiInterrupt, ...]] = []
    offset = 0
    for native in native_interrupts:
        try:
            request = HitlRequest.model_validate(native.value)
        except ValidationError:
            size = 1
        else:
            size = len(request.action_requests)
        group = tuple(prepared[offset : offset + size])
        if len(group) != size:
            raise RuntimeError("prepared interrupt grouping is inconsistent")
        grouped.append(group)
        offset += size
    if offset != len(prepared):
        raise RuntimeError("prepared interrupt grouping is inconsistent")
    return tuple(grouped)


def _buffer_child_interrupts(
    self: DeepAgentAgUiAdapter,
    part: NativeValuesStreamPart,
    source: AgentSource,
    raw_messages: object,
) -> None:
    """Validate and stage child interrupts until root propagation arrives."""

    native_by_id: dict[str, AgentRuntimeInterrupt] = {}
    new_interrupts: list[AgentRuntimeInterrupt] = []
    for native in part.interrupts:
        if native.id in native_by_id:
            raise HitlCorrelationError(f"duplicate child interrupt ID: {native.id}")
        native_by_id[native.id] = native
        existing = self._child_interrupts.get(native.id)
        if existing is None:
            new_interrupts.append(native)
            continue
        if existing.value_json != self._interrupt_value_json(native):
            raise HitlCorrelationError(
                f"conflicting child interrupt propagation for ID: {native.id}"
            )
        if part.ns not in existing.namespaces and any(
            not (
                namespace == part.ns[: len(namespace)]
                or part.ns == namespace[: len(part.ns)]
            )
            for namespace in existing.namespaces
        ):
            raise HitlCorrelationError(
                "the same interrupt ID appeared in unrelated child namespaces: "
                f"{native.id}"
            )

    prepared_new = self._prepare_ag_ui_interrupts(
        tuple(new_interrupts),
        source,
        raw_messages,
    )
    grouped_new = self._group_prepared_interrupts(
        new_interrupts,
        prepared_new,
    )
    prepared_by_native_id = {
        native.id: group
        for native, group in zip(new_interrupts, grouped_new, strict=True)
    }
    converted_messages = tuple(self._convert_messages(raw_messages, part.ns))

    staged_interrupts = dict(self._child_interrupts)
    for native in part.interrupts:
        existing = staged_interrupts.get(native.id)
        if existing is None:
            staged_interrupts[native.id] = BufferedChildInterrupt(
                native_id=native.id,
                value_json=self._interrupt_value_json(native),
                prepared=prepared_by_native_id[native.id],
                namespaces=(part.ns,),
            )
        elif part.ns not in existing.namespaces:
            staged_interrupts[native.id] = BufferedChildInterrupt(
                native_id=existing.native_id,
                value_json=existing.value_json,
                prepared=existing.prepared,
                namespaces=(*existing.namespaces, part.ns),
            )

    self._validate_prepared_interrupts(
        [
            public
            for buffered in staged_interrupts.values()
            for public in buffered.prepared
        ]
    )
    native_ids = tuple(native_by_id)
    previous_ids = self._child_interrupt_ids_by_namespace.get(part.ns, ())
    previous_messages = self._child_message_snapshots.get(part.ns)
    if previous_messages is not None:
        # Parallel tasks may add messages and report separate interrupt batches
        # in one Graph. Previously correlated proposals must remain identical;
        # unrelated message additions do not alter those pending decisions.
        previous_calls = {
            call.id: (message.id, call)
            for message in previous_messages
            if isinstance(message, AgUiAssistantMessage)
            for call in message.tool_calls or ()
        }
        current_calls = {
            call.id: (message.id, call)
            for message in converted_messages
            if isinstance(message, AgUiAssistantMessage)
            for call in message.tool_calls or ()
        }
        pending_calls = {
            public.tool_call_id
            for native_id in previous_ids
            for public in self._child_interrupts[native_id].prepared
            if public.tool_call_id is not None
        }
        if any(
            previous_calls[call_id] != current_calls.get(call_id)
            for call_id in pending_calls & previous_calls.keys()
        ):
            raise HitlCorrelationError(
                f"conflicting child message snapshot for namespace: {part.ns!r}"
            )

    staged_ids_by_namespace = dict(self._child_interrupt_ids_by_namespace)
    staged_ids_by_namespace[part.ns] = tuple(
        dict.fromkeys((*previous_ids, *native_ids))
    )
    staged_messages = dict(self._child_message_snapshots)
    staged_messages[part.ns] = converted_messages
    self._child_interrupts = staged_interrupts
    self._child_interrupt_ids_by_namespace = staged_ids_by_namespace
    self._child_message_snapshots = staged_messages


def _prepare_root_interrupts(
    self: DeepAgentAgUiAdapter,
    native_interrupts: Sequence[AgentRuntimeInterrupt],
    source: AgentSource,
    raw_messages: object,
) -> tuple[list[AgUiInterrupt], set[str], tuple[tuple[str, ...], ...]]:
    """Match root interrupts to buffered children and prepare root-local ones."""

    native_by_id: dict[str, AgentRuntimeInterrupt] = {}
    for native in native_interrupts:
        if native.id in native_by_id:
            raise HitlCorrelationError(f"duplicate root interrupt ID: {native.id}")
        native_by_id[native.id] = native
        buffered = self._child_interrupts.get(native.id)
        if buffered is not None and buffered.value_json != self._interrupt_value_json(
            native
        ):
            raise HitlCorrelationError(
                f"conflicting root propagation for child interrupt ID: {native.id}"
            )

    # LangGraph 1.2.10 emits root values for each interrupted task separately.
    # finish() verifies propagation across the complete stream; this boundary
    # validates only the groups present in this part (test_compiled_subgraphs).

    root_local = tuple(
        native
        for native in native_interrupts
        if native.id not in self._child_interrupts
    )
    prepared_local = self._prepare_ag_ui_interrupts(
        root_local,
        source,
        raw_messages,
    )
    grouped_local = self._group_prepared_interrupts(
        root_local,
        prepared_local,
    )
    local_by_id = {
        native.id: group
        for native, group in zip(root_local, grouped_local, strict=True)
    }
    prepared: list[AgUiInterrupt] = []
    propagated_child_ids: set[str] = set()
    for native in native_interrupts:
        buffered = self._child_interrupts.get(native.id)
        if buffered is None:
            prepared.extend(local_by_id[native.id])
        else:
            prepared.extend(buffered.prepared)
            propagated_child_ids.add(native.id)
    self._validate_prepared_interrupts(prepared)

    visible_child_ids = self._resolved_child_interrupt_ids | propagated_child_ids
    child_namespaces = tuple(
        namespace
        for namespace in self._graph_scopes
        if visible_child_ids.intersection(
            self._child_interrupt_ids_by_namespace.get(namespace, ())
        )
    )
    return prepared, propagated_child_ids, child_namespaces


def _record_interrupts(
    self: DeepAgentAgUiAdapter,
    interrupts: Sequence[AgUiInterrupt],
) -> None:
    """Record terminal interrupts in first-seen order and ignore replayed frames."""

    for interrupt in interrupts:
        if interrupt.id in self._interrupts_by_id:
            continue
        self._interrupts_by_id[interrupt.id] = interrupt
        self._interrupts_in_order.append(interrupt)


def _prepare_ag_ui_interrupts(
    self: DeepAgentAgUiAdapter,
    native_interrupts: Sequence[AgentRuntimeInterrupt],
    source: AgentSource,
    raw_messages: object,
) -> list[AgUiInterrupt]:
    """Validate all HITL Tool correlations before mutating adapter state."""

    if not native_interrupts:
        return []
    parsed: list[
        tuple[
            AgentRuntimeInterrupt,
            HitlRequest | None,
            RuntimeInterruptEnvelope | None,
        ]
    ] = []
    action_groups: list[Sequence[HitlActionRequest]] = []
    for interrupt in native_interrupts:
        try:
            runtime_envelope = parse_runtime_interrupt(interrupt.value)
        except ValidationError as error:
            raise HitlCorrelationError(
                f"invalid TinkerFin runtime interrupt: {interrupt.id}"
            ) from error
        if runtime_envelope is not None:
            parsed.append((interrupt, None, runtime_envelope))
            continue
        try:
            request = HitlRequest.model_validate(interrupt.value)
        except ValidationError as error:
            value = interrupt.value
            if isinstance(value, dict) and (
                "action_requests" in value or "review_configs" in value
            ):
                raise HitlCorrelationError(
                    f"invalid Deep Agents HITL interrupt: {interrupt.id}"
                ) from error
            request = None
        parsed.append((interrupt, request, None))
        if request is not None:
            action_groups.append(request.action_requests)

    if any(request is not None for _, request, _ in parsed) and any(
        runtime is not None for _, _, runtime in parsed
    ):
        raise HitlCorrelationError(
            "runtime and Deep Agents Tool interrupts cannot share one batch"
        )

    matched_id_groups = self._tool_call_id_groups_for_actions(
        source.graph_namespace,
        action_groups,
        raw_messages,
    )
    matched_group_index = 0
    prepared: list[AgUiInterrupt] = []
    for interrupt, request, runtime_envelope in parsed:
        group_ids: Sequence[str] = ()
        if request is not None:
            group_ids = matched_id_groups[matched_group_index]
            matched_group_index += 1
        prepared.extend(
            project_interrupt(
                interrupt,
                tool_call_ids=group_ids,
                source=source.model_dump(mode="json", by_alias=True),
            )
        )
    self._validate_prepared_interrupts(prepared)
    return prepared


def _validate_prepared_interrupts(
    self: DeepAgentAgUiAdapter,
    prepared: Sequence[AgUiInterrupt],
) -> None:
    """Reject public-ID collisions and conflicting committed replays."""

    prepared_by_id: dict[str, AgUiInterrupt] = {}
    for interrupt in prepared:
        if interrupt.id in prepared_by_id:
            raise ValueError(f"duplicate public interrupt ID: {interrupt.id}")
        prepared_by_id[interrupt.id] = interrupt
        previous = self._interrupts_by_id.get(interrupt.id)
        if previous is not None and not json_values_equal(
            _to_json_value(
                previous.model_dump(
                    mode="python",
                    by_alias=True,
                    exclude_none=False,
                )
            ),
            _to_json_value(
                interrupt.model_dump(
                    mode="python",
                    by_alias=True,
                    exclude_none=False,
                )
            ),
        ):
            raise ValueError(f"conflicting interrupt replay for ID: {interrupt.id}")


def _tool_call_id_groups_for_actions(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    action_groups: Sequence[Sequence[HitlActionRequest]],
    raw_messages: object,
) -> list[list[str]]:
    """Require each action group to have one unique Tool-call assignment."""

    if not action_groups:
        return []

    if isinstance(raw_messages, Sequence) and not isinstance(
        raw_messages,
        (str, bytes),
    ):
        messages = cast(Sequence[object], raw_messages)
        checkpoint_messages = tuple(
            message for message in messages if isinstance(message, BaseMessage)
        )
        if any(
            isinstance(message, AIMessage) and message.tool_calls
            for message in checkpoint_messages
        ):
            raw_groups = match_hitl_tool_call_id_groups(
                action_groups,
                checkpoint_messages,
            )
            return [
                [self._tool_call_id(namespace, raw_id) for raw_id in raw_ids]
                for raw_ids in raw_groups
            ]

    calls_by_message: dict[str, list[ActiveToolCall]] = {}
    seen_tool_ids: set[str] = set()
    for history in self._tool_history.values():
        for call in history:
            if (
                call.namespace != namespace
                or call.tool_call_id in seen_tool_ids
                or call.parent_message_id is None
                or call.tool_call_id in self._result_fingerprints
            ):
                continue
            seen_tool_ids.add(call.tool_call_id)
            calls_by_message.setdefault(call.parent_message_id, []).append(call)
    candidate_messages: list[list[HitlToolCallCandidate]] = []
    for calls in calls_by_message.values():
        if any(call.index is None for call in calls) and any(
            call.index is not None for call in calls
        ):
            # Provider positions cannot locate unindexed calls among indexed ones.
            # A complete checkpoint message (handled above) supplies that ordering.
            raise HitlCorrelationError(
                "mixed indexed and unindexed Tool history requires checkpoint message order"
            )
        candidates: list[HitlToolCallCandidate] = []
        for position, call in enumerate(
            sorted(
                calls,
                key=lambda item: (
                    item.index is None,
                    item.index if item.index is not None else item.order,
                ),
            )
        ):
            if call.parent_message_id is None:
                raise HitlCorrelationError(
                    "streamed Tool call history requires message positions"
                )
            arguments: JsonValue | None
            arguments_error: json.JSONDecodeError | None
            try:
                arguments = normalize_operational_data(
                    json.loads(call.arguments or "{}")
                )
            except json.JSONDecodeError as error:
                arguments = None
                arguments_error = error
            else:
                arguments_error = None
            candidates.append(
                HitlToolCallCandidate(
                    graph_namespace=call.namespace,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    parent_message_id=call.parent_message_id,
                    position=position,
                    arguments=arguments,
                    arguments_error=arguments_error,
                )
            )
        candidate_messages.append(candidates)
    try:
        matched_groups = match_hitl_action_groups(
            action_groups,
            candidate_messages,
        )
    except HitlNoMatchError:
        malformed = relevant_malformed_hitl_candidate(
            action_groups,
            candidate_messages,
        )
        if malformed is None or malformed.arguments_error is None:
            raise
        raise HitlCorrelationError(
            "streamed Tool call history contains invalid JSON arguments"
        ) from malformed.arguments_error
    malformed = relevant_malformed_hitl_candidate(
        action_groups,
        candidate_messages,
    )
    if malformed is not None and malformed.arguments_error is not None:
        raise HitlCorrelationError(
            "streamed Tool call history contains invalid JSON arguments"
        ) from malformed.arguments_error
    return [list(group) for group in matched_groups]
