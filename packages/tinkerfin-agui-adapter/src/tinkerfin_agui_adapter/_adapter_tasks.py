"""Task provenance, state synchronization, and safe checkpoint projection."""

from __future__ import annotations

__all__ = [
    "_emit_values_part",
    "_merge_message_snapshots",
    "_process_extra_part",
    "_process_task_result",
    "_process_task_start",
    "_process_tasks_part",
    "_process_values_part",
    "_task_raw_event",
]

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal, cast

from ag_ui.core import (
    BaseEvent,
    RawEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
)
from ag_ui.core.types import Message
from pydantic import JsonValue

from tinkerfin_native_stream import (
    NativeExtraStreamPart as ExtraStreamPart,
)
from tinkerfin_native_stream import (
    NativeTaskResultPayload as TaskResultPayload,
)
from tinkerfin_native_stream import (
    NativeTasksStreamPart as TasksStreamPart,
)
from tinkerfin_native_stream import (
    NativeTaskStartPayload as TaskStartPayload,
)
from tinkerfin_native_stream import (
    NativeUpdatesStreamPart as UpdatesStreamPart,
)
from tinkerfin_native_stream import (
    NativeValuesStreamPart as ValuesStreamPart,
)

from ._adapter_contracts import (
    _MESSAGE_STATE_KEY,
    AgentSource,
    GraphScope,
    NativeToolCall,
    NativeToolCallList,
    SubagentInvocation,
    TaskResultFingerprint,
    TaskStartFingerprint,
    _to_json_value,
)
from ._adapter_messages import _json_patch, _remember_message_baseline
from .media_events import AttachmentMessagesSnapshotEvent
from .reasoning import normalize_operational_data, sanitize_public_data
from .subagent import SubagentTaskInput, create_subagent_provenance

if TYPE_CHECKING:
    from .adapter import DeepAgentAgUiAdapter


def _without_private_state_keys(
    value: object,
    private_state_keys: frozenset[str],
) -> object:
    """Remove reserved channels only from a known state-mapping boundary."""

    if not isinstance(value, Mapping):
        return value
    mapping = cast(Mapping[object, object], value)
    return {
        key: item
        for key, item in mapping.items()
        if not isinstance(key, str) or key not in private_state_keys
    }


def _public_task_error(error: object | None) -> object | None:
    """Project a native task error into a stable public shape without traceback."""

    if isinstance(error, BaseException):
        return {"type": type(error).__name__, "message": str(error)}
    return error


def _safe_checkpoint_task(
    value: object,
    private_state_keys: frozenset[str],
) -> dict[str, JsonValue]:
    """Project one checkpoint task without its resumable runtime state."""

    if not isinstance(value, Mapping):
        raise TypeError("checkpoint tasks must be JSON objects")
    mapping = cast(Mapping[object, object], value)
    allowed = ("id", "name", "result", "interrupts")
    projected: dict[str, object] = {
        key: mapping[key] for key in allowed if key in mapping
    }
    if "result" in projected:
        projected["result"] = _without_private_state_keys(
            projected["result"],
            private_state_keys,
        )
    if "error" in mapping:
        projected["error"] = _public_task_error(mapping["error"])
    normalized = sanitize_public_data(projected)
    if not isinstance(normalized, dict):
        raise TypeError("checkpoint task projection must be a JSON object")
    return normalized


def _safe_debug_task_start(
    value: object,
    private_state_keys: frozenset[str],
) -> dict[str, JsonValue]:
    """Project a debug task start without runtime configuration metadata."""

    if not isinstance(value, Mapping):
        raise TypeError("debug task data must be a JSON object")
    mapping = cast(Mapping[object, object], value)
    projected: dict[str, object] = {
        key: mapping[key]
        for key in ("id", "name", "input", "triggers")
        if key in mapping
    }
    if "input" in projected:
        projected["input"] = _without_private_state_keys(
            projected["input"],
            private_state_keys,
        )
    normalized = sanitize_public_data(projected)
    if not isinstance(normalized, dict):
        raise TypeError("debug task projection must be a JSON object")
    return normalized


def _safe_checkpoint_payload(
    value: object,
    private_state_keys: frozenset[str],
) -> JsonValue:
    """Remove checkpoint configuration from checkpoint and debug payloads."""

    if not isinstance(value, Mapping):
        raise TypeError("checkpoint and debug stream data must be JSON objects")
    mapping = cast(Mapping[object, object], value)
    if "type" in mapping or "payload" in mapping:
        debug_type = mapping.get("type")
        if debug_type not in {"checkpoint", "task", "task_result"}:
            raise ValueError("debug stream data has an unsupported event type")
        payload = mapping.get("payload")
        if debug_type == "checkpoint":
            safe_payload = _safe_checkpoint_snapshot(payload, private_state_keys)
        elif debug_type == "task":
            safe_payload = _safe_debug_task_start(payload, private_state_keys)
        else:
            safe_payload = _safe_checkpoint_task(payload, private_state_keys)
        normalized = sanitize_public_data(
            {
                key: mapping[key]
                for key in ("step", "timestamp", "type")
                if key in mapping
            }
            | {"payload": safe_payload}
        )
        if not isinstance(normalized, dict):
            raise TypeError("debug stream projection must be a JSON object")
        return normalized
    return _safe_checkpoint_snapshot(mapping, private_state_keys)


def _safe_checkpoint_snapshot(
    value: object,
    private_state_keys: frozenset[str],
) -> dict[str, JsonValue]:
    """Whitelist stable checkpoint fields and sanitize nested task results."""

    if not isinstance(value, Mapping):
        raise TypeError("checkpoint stream data must be a JSON object")
    mapping = cast(Mapping[object, object], value)
    projected: dict[str, object] = {
        key: mapping[key] for key in ("values", "next") if key in mapping
    }
    if "values" in projected:
        projected["values"] = _without_private_state_keys(
            projected["values"],
            private_state_keys,
        )
    metadata = mapping.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise TypeError("checkpoint metadata must be a JSON object")
        metadata_mapping = cast(Mapping[object, object], metadata)
        if "step" in metadata_mapping:
            projected["step"] = metadata_mapping["step"]
    if "tasks" in mapping:
        tasks = mapping["tasks"]
        if not isinstance(tasks, Sequence) or isinstance(
            tasks, (str, bytes, bytearray)
        ):
            raise TypeError("checkpoint tasks must be a sequence")
        projected["tasks"] = [
            _safe_checkpoint_task(task, private_state_keys)
            for task in cast(Sequence[object], tasks)
        ]
    normalized = sanitize_public_data(projected)
    if not isinstance(normalized, dict):
        raise TypeError("checkpoint stream projection must be a JSON object")
    return normalized


def _process_tasks_part(
    self: DeepAgentAgUiAdapter, part: TasksStreamPart
) -> list[BaseEvent]:
    """Publish task provenance and establish subgraph correlation on start."""

    if isinstance(part.data, TaskStartPayload):
        return self._process_task_start(part.ns, part.data)
    return self._process_task_result(part.ns, part.data)


def _process_task_start(
    self: DeepAgentAgUiAdapter,
    parent_namespace: tuple[str, ...],
    payload: TaskStartPayload,
) -> list[BaseEvent]:
    """Stage one task start and commit all correlation state atomically.

    Runtime task IDs establish graph scopes; only validated ``tools`` inputs containing
    Deep Agents ``task`` calls establish subagent identity. Every duplicate and
    cross-map conflict is checked against staged copies before the Adapter mutates live
    state, so a rejected part can be retried without partial provenance.
    """

    parent_source = self._source(parent_namespace)
    self._require_started_source(parent_source)
    tool_calls = (
        NativeToolCallList.model_validate(payload.input).root
        if payload.name == "tools"
        else []
    )
    fingerprint = TaskStartFingerprint(
        name=payload.name,
        input_json=json.dumps(
            _to_json_value(
                [
                    tool_call.model_dump(mode="json", by_alias=True)
                    for tool_call in tool_calls
                ]
                if payload.name == "tools"
                else payload.input
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        triggers=payload.triggers,
        metadata_json=json.dumps(
            (
                None
                if payload.metadata is None
                else _to_json_value(
                    payload.metadata.model_dump(
                        mode="python",
                        by_alias=False,
                        exclude_none=False,
                    )
                )
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    fingerprint_key = (parent_namespace, payload.id)
    existing_fingerprint = self._task_start_fingerprints.get(fingerprint_key)
    if existing_fingerprint is not None and existing_fingerprint != fingerprint:
        raise ValueError(
            "conflicting task start for "
            f"namespace={parent_namespace!r} graph_task_id={payload.id!r}"
        )
    subagents: list[dict[str, JsonValue]] = []
    staged_native_tool_calls: dict[
        tuple[tuple[str, ...], str], list[NativeToolCall]
    ] = {key: list(calls) for key, calls in self._native_tool_calls.items()}
    staged_graph_scopes = dict(self._graph_scopes)
    staged_subagent_invocations = dict(self._subagent_invocations)
    staged_sub_namespaces = dict(self._sub_namespaces_by_parent_tool_call)
    staged_agent_names = dict(self._namespace_agent_names)
    staged_tool_names = dict(self._tool_names_by_id)
    child_namespace = (*parent_namespace, f"{payload.name}:{payload.id}")
    graph_scope = GraphScope(
        namespace=child_namespace,
        parent_namespace=parent_namespace,
        graph_task_id=payload.id,
        node_name=payload.name,
    )
    existing_scope = staged_graph_scopes.get(child_namespace)
    if existing_scope is not None and existing_scope != graph_scope:
        raise ValueError(f"conflicting task start for namespace={child_namespace!r}")
    staged_graph_scopes[child_namespace] = graph_scope
    if payload.name == "tools":
        payload_tool_ids: set[str] = set()
        for tool_call in tool_calls:
            scoped_id = self._tool_call_id(parent_namespace, tool_call.id)
            if scoped_id in payload_tool_ids:
                if tool_call.name == "task":
                    raise ValueError(f"duplicate parent tool call ID: {tool_call.id}")
                raise ValueError(f"duplicate native tool call ID: {tool_call.id}")
            payload_tool_ids.add(scoped_id)
        if existing_fingerprint is None:
            existing_native_ids = {
                self._tool_call_id(namespace, call.id)
                for (namespace, _), calls in self._native_tool_calls.items()
                for call in calls
            }
            for tool_call in tool_calls:
                scoped_id = self._tool_call_id(parent_namespace, tool_call.id)
                if scoped_id in existing_native_ids:
                    raise ValueError(f"duplicate native tool call ID: {tool_call.id}")
        for tool_call in tool_calls:
            scoped_id = self._tool_call_id(parent_namespace, tool_call.id)
            known_name = staged_tool_names.get(scoped_id)
            if known_name is not None and known_name != tool_call.name:
                raise ValueError(f"duplicate native tool call ID: {tool_call.id}")
            staged_tool_names[scoped_id] = tool_call.name
        for tool_call in tool_calls:
            calls = staged_native_tool_calls.setdefault(
                (parent_namespace, tool_call.name), []
            )
            if all(existing.id != tool_call.id for existing in calls):
                calls.append(tool_call)
        task_calls = [call for call in tool_calls if call.name == "task"]
        parsed_calls = [
            (task_call, SubagentTaskInput.model_validate(task_call.args.root))
            for task_call in task_calls
        ]
        multiple = len(parsed_calls) > 1
        for index, (task_call, descriptor) in enumerate(parsed_calls):
            suffix = f"{payload.id}:{index}" if multiple else payload.id
            child_namespace = (*parent_namespace, f"tools:{suffix}")
            graph_scope = GraphScope(
                namespace=child_namespace,
                parent_namespace=parent_namespace,
                graph_task_id=payload.id,
                node_name=payload.name,
            )
            existing_scope = staged_graph_scopes.get(child_namespace)
            if existing_scope is not None and existing_scope != graph_scope:
                raise ValueError(
                    f"conflicting task start for namespace={child_namespace!r}"
                )
            staged_graph_scopes[child_namespace] = graph_scope
            parent_tool_call_id = self._tool_call_id(
                parent_namespace,
                task_call.id,
            )
            provenance = create_subagent_provenance(
                identity=self._identity,
                graph_namespace=child_namespace,
                parent_graph_namespace=parent_namespace,
                graph_task_id=payload.id,
                agent_name=descriptor.subagent_type,
                parent_tool_call_id=parent_tool_call_id,
                description=descriptor.description,
            )
            invocation = SubagentInvocation(
                parent_namespace=parent_namespace,
                child_namespace=child_namespace,
                graph_task_id=payload.id,
                parent_tool_call_id=parent_tool_call_id,
                subagent_input=descriptor.description,
                agent_name=descriptor.subagent_type,
                provenance=provenance,
            )
            existing = staged_subagent_invocations.get(child_namespace)
            if existing is not None:
                if existing != invocation:
                    raise ValueError(
                        f"conflicting task start for namespace={child_namespace!r}"
                    )
            else:
                parent_key = (parent_namespace, task_call.id)
                related = staged_sub_namespaces.get(parent_key)
                if related is not None and related != child_namespace:
                    raise ValueError(f"duplicate parent tool call ID: {task_call.id}")
                staged_subagent_invocations[child_namespace] = invocation
                staged_sub_namespaces[parent_key] = child_namespace
                staged_agent_names[child_namespace] = descriptor.subagent_type
            subagents.append(
                provenance.model_dump(
                    mode="json",
                    by_alias=True,
                )
            )
    event = _task_raw_event(
        namespace=parent_namespace,
        source=parent_source,
        phase="start",
        data={
            "id": payload.id,
            "name": payload.name,
            "input": _without_private_state_keys(
                payload.input,
                self._private_state_keys,
            ),
            "triggers": payload.triggers,
            **(
                {
                    "metadata": payload.metadata.model_dump(
                        mode="python",
                        by_alias=False,
                        exclude_none=True,
                        exclude_defaults=True,
                    )
                }
                if payload.metadata is not None
                else {}
            ),
        },
        subagents=subagents,
    )
    staged_task_start_fingerprints = dict(self._task_start_fingerprints)
    staged_task_start_fingerprints[fingerprint_key] = fingerprint
    (
        self._native_tool_calls,
        self._graph_scopes,
        self._subagent_invocations,
        self._sub_namespaces_by_parent_tool_call,
        self._namespace_agent_names,
        self._tool_names_by_id,
        self._task_start_fingerprints,
    ) = (
        staged_native_tool_calls,
        staged_graph_scopes,
        staged_subagent_invocations,
        staged_sub_namespaces,
        staged_agent_names,
        staged_tool_names,
        staged_task_start_fingerprints,
    )
    return [event]


def _process_task_result(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    payload: TaskResultPayload,
) -> list[BaseEvent]:
    """Complete a previously started runtime task with exact replay semantics.

    Results correlate by full namespace and task ID, never completion order. An exact
    replay is ignored, while any change to name, error, interrupts, or result fails
    before the stored fingerprint is updated.
    """

    source = self._source(namespace)
    self._require_started_source(source)
    key = (namespace, payload.id)
    start = self._task_start_fingerprints.get(key)
    if start is None:
        raise ValueError(
            "task result has no matching task start: "
            f"namespace={namespace!r} graph_task_id={payload.id!r}"
        )
    if start.name != payload.name:
        raise ValueError(
            "conflicting task result for "
            f"namespace={namespace!r} graph_task_id={payload.id!r}"
        )
    public_error = _public_task_error(payload.error)
    fingerprint = TaskResultFingerprint(
        name=payload.name,
        error_json=json.dumps(
            _to_json_value(public_error),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        interrupts_json=json.dumps(
            _to_json_value(payload.interrupts),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        result_json=json.dumps(
            _to_json_value(payload.result),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    previous = self._task_result_fingerprints.get(key)
    if previous is not None:
        if previous == fingerprint:
            return []
        raise ValueError(
            "conflicting task result for "
            f"namespace={namespace!r} graph_task_id={payload.id!r}"
        )
    event = _task_raw_event(
        namespace=namespace,
        source=source,
        phase="result",
        data={
            "id": payload.id,
            "name": payload.name,
            "error": public_error,
            "interrupts": payload.interrupts,
            "result": _without_private_state_keys(
                payload.result,
                self._private_state_keys,
            ),
        },
    )
    self._task_result_fingerprints[key] = fingerprint
    return [event]


def _task_raw_event(
    *,
    namespace: tuple[str, ...],
    source: AgentSource,
    phase: Literal["start", "result"],
    data: Mapping[str, object],
    subagents: Sequence[dict[str, JsonValue]] = (),
) -> RawEvent:
    """Build a task RAW event after JSON and provider-privacy filtering."""

    provenance = source.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    if subagents:
        provenance["subagents"] = list(subagents)
    raw_data = dict(data)
    operational_input: JsonValue | None = None
    if phase == "start" and raw_data.get("name") == "tools":
        operational_input = normalize_operational_data(raw_data.pop("input"))
    public_event = sanitize_public_data({"data": raw_data, "provenance": provenance})
    if not isinstance(public_event, dict):
        raise TypeError("task RAW event must be a JSON object")
    public_data = public_event.get("data")
    if operational_input is not None:
        if not isinstance(public_data, dict):
            raise TypeError("task RAW data must be a JSON object")
        public_data["input"] = operational_input
    return RawEvent(
        source="langgraph.tasks",
        raw_event={"type": "tasks", "phase": phase, "ns": list(namespace)},
        event=public_event,
    )


def _process_values_part(
    self: DeepAgentAgUiAdapter, part: ValuesStreamPart
) -> list[BaseEvent]:
    source = self._source(part.ns)
    self._require_started_source(source)
    return self._emit_values_part(part)


def _process_extra_part(
    self: DeepAgentAgUiAdapter, part: ExtraStreamPart | UpdatesStreamPart
) -> list[BaseEvent]:
    """Project an additional native mode without exposing runtime config."""

    source = self._source(part.ns)
    self._require_started_source(source)
    if isinstance(part, UpdatesStreamPart):
        data = sanitize_public_data(
            {
                node: _without_private_state_keys(update, self._private_state_keys)
                for node, update in part.data.items()
            }
        )
    elif part.type in {"checkpoints", "debug"}:
        data = _safe_checkpoint_payload(part.data, self._private_state_keys)
    else:
        data = sanitize_public_data(part.data)
    public_event = sanitize_public_data(
        {
            "data": data,
            "provenance": source.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
        }
    )
    if not isinstance(public_event, dict):
        raise TypeError("RAW stream event must be a JSON object")
    return [
        RawEvent(
            source=f"langgraph.{part.type}",
            raw_event={"type": part.type, "ns": list(part.ns)},
            event=public_event,
        )
    ]


def _emit_values_part(
    self: DeepAgentAgUiAdapter, part: ValuesStreamPart
) -> list[BaseEvent]:
    """Synchronize root state or retain a non-root snapshot as provenance.

    Subgraph values never overwrite root application state. Root interrupts first close
    every open child lifecycle, then emit final state and message snapshots before the
    interrupt terminal can be created. Ordinary root updates use RFC 6902 operations
    against only a baseline the client actually received.
    """

    source = self._source(part.ns)
    raw_event = self._event_context("values", source)
    raw_messages = part.data.get(_MESSAGE_STATE_KEY, [])
    _remember_message_baseline(self, part.ns, raw_messages)
    # Messages have a dedicated AG-UI channel. Keeping LangChain messages in
    # `STATE_*` would duplicate data, so state events contain non-message state.
    raw_current = {
        key: value
        for key, value in part.data.items()
        if key != _MESSAGE_STATE_KEY and key not in self._private_state_keys
    }
    current_value = sanitize_public_data(raw_current)
    if not isinstance(current_value, dict):
        raise TypeError("values state without messages must be a JSON object")
    current = current_value
    events: list[BaseEvent] = []
    if source.kind != "root":
        event = RawEvent(
            source="langgraph.values",
            raw_event={"type": "values", "ns": list(part.ns)},
            event={
                "state": current,
                "provenance": source.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                ),
            },
        )
        if part.interrupts:
            self._buffer_child_interrupts(part, source, raw_messages)
        return [event]

    prepared_interrupts, propagated_child_ids, child_namespaces = (
        self._prepare_root_interrupts(part.interrupts, source, raw_messages)
    )
    converted_messages = (
        self._merge_message_snapshots(
            self._convert_messages(raw_messages, part.ns),
            child_namespaces,
        )
        if prepared_interrupts
        else None
    )

    previous = self._previous_root_state
    # Root state is an authoritative synchronization boundary. Providers may
    # omit a final chunk marker, so close every child channel before publishing
    # either a snapshot or a delta.
    events.extend(self._close_all_reasoning())
    events.extend(self._close_all_messages())
    events.extend(self._close_all_tools())
    if prepared_interrupts:
        events.append(StateSnapshotEvent(snapshot=current, raw_event=raw_event))
        events.append(
            AttachmentMessagesSnapshotEvent(
                messages=converted_messages or [],
                raw_event=raw_event,
            )
        )
        self._previous_root_state = current
        self._record_interrupts(prepared_interrupts)
        self._resolved_child_interrupt_ids.update(propagated_child_ids)
        return events

    if previous is None and not current:
        # A messages-only values frame has no visible state after filtering, so it
        # cannot establish a patch baseline the client never received
        return events
    if previous is None:
        events.append(StateSnapshotEvent(snapshot=current, raw_event=raw_event))
    else:
        operations = _json_patch(previous, current)
        if operations:
            events.append(
                StateDeltaEvent(
                    delta=[
                        operation.model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_unset=True,
                        )
                        for operation in operations
                    ],
                    raw_event=raw_event,
                )
            )
    self._previous_root_state = current

    return events


def _merge_message_snapshots(
    self: DeepAgentAgUiAdapter,
    root_messages: Sequence[Message],
    child_namespaces: Sequence[tuple[str, ...]],
) -> list[Message]:
    """Merge root-first scoped snapshots and reject conflicting duplicate IDs."""

    merged: list[Message] = []
    fingerprints: dict[str, str] = {}
    groups = [
        tuple(root_messages),
        *(
            self._child_message_snapshots.get(namespace, ())
            for namespace in child_namespaces
        ),
    ]
    for messages in groups:
        for message in messages:
            fingerprint = json.dumps(
                _to_json_value(
                    message.model_dump(
                        mode="python",
                        by_alias=True,
                        exclude_none=False,
                    )
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            previous = fingerprints.get(message.id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError(f"conflicting snapshot message ID: {message.id}")
                continue
            fingerprints[message.id] = fingerprint
            merged.append(message)
    return merged
