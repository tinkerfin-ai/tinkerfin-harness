"""Message, reasoning, Tool call, and snapshot conversion operations."""

from __future__ import annotations

__all__ = [
    "_close_all_messages",
    "_close_all_reasoning",
    "_close_all_tools",
    "_close_message",
    "_close_reasoning",
    "_close_tool",
    "_close_tools",
    "_convert_messages",
    "_convert_reasoning_events",
    "_emit_reasoning",
    "_event_context",
    "_message_id",
    "_process_ai_chunk",
    "_process_message_part",
    "_process_tool_chunk",
    "_process_tool_result",
    "_reasoning_deltas",
    "_record_agent_name",
    "_remember_message_baseline",
    "_require_started_source",
    "_source",
    "_stable_message_id",
    "_tool_call_id",
]

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal, cast

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCall,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UserMessage,
)
from ag_ui.core import SystemMessage as AgUiSystemMessage
from ag_ui.core.types import FunctionCall, Message
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCallChunk,
    ToolMessage,
)
from pydantic import JsonValue

from tinkerfin_contracts.media import Attachment, attachment_from_block
from tinkerfin_native_stream import NativeMessageStreamPart as MessageStreamPart
from tinkerfin_native_stream import NativeStreamMode as StreamMode

from ._adapter_contracts import (
    ActiveReasoning,
    ActiveToolCall,
    AgentSource,
    EventContext,
    JsonPatchOperation,
    ToolResultFingerprint,
    _to_json_value,
)
from .media import MessageAttachments, content_attachments, user_content_to_agui
from .media_events import (
    AttachmentAssistantMessage,
    AttachmentToolCallResultEvent,
    AttachmentToolMessage,
)
from .reasoning import (
    json_values_equal,
    normalize_operational_data,
    sanitize_public_data,
)

if TYPE_CHECKING:
    from .adapter import DeepAgentAgUiAdapter


def _message_fingerprint(message: AIMessage | ToolMessage) -> bytes:
    public = normalize_operational_data(message)
    return hashlib.sha256(
        json.dumps(public, sort_keys=True, ensure_ascii=False).encode()
    ).digest()


def _remember_message_baseline(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    messages: object,
) -> None:
    """Keep existing complete messages separate from new streamed content.

    The first values frame is the only evidence of preexisting messages. Native
    middleware can emit those same objects when repairing message history; they
    must not append their text again. Retain only fingerprints and known Tool IDs,
    scoped to this adapter's lifetime, and never suppress model chunks.
    """
    if namespace in self._message_baseline_namespaces:
        return
    if not isinstance(messages, (tuple, list)):
        return
    self._message_baseline_namespaces.add(namespace)
    for message in cast(Sequence[object], messages):
        if not isinstance(message, (AIMessage, ToolMessage)) or isinstance(
            message, AIMessageChunk
        ):
            continue
        if isinstance(message.id, str) and message.id:
            self._message_baselines[(namespace, message.id)] = _message_fingerprint(
                message
            )
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                raw_id = call.get("id")
                if not isinstance(raw_id, str) or not raw_id:
                    continue
                tool_id = self._tool_call_id(namespace, raw_id)
                if tool_id in self._started_tool_ids:
                    continue
                if isinstance(message.id, str) and message.id:
                    self._baseline_tool_parent_ids[tool_id] = self._message_id(
                        namespace, message.id
                    )
                self._prior_tool_call_ids.add(tool_id)
                self._started_tool_ids.add(tool_id)
                self._ended_tool_ids.add(tool_id)
                self._tool_names_by_id[tool_id] = call["name"]


def _escape_json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _json_patch(
    previous: JsonValue,
    current: JsonValue,
    path: str = "",
) -> list[JsonPatchOperation]:
    """Build a deterministically ordered RFC 6902 patch between two JSON values."""

    if json_values_equal(previous, current):
        return []
    if isinstance(previous, dict) and isinstance(current, dict):
        operations: list[JsonPatchOperation] = []
        for key in sorted(previous.keys() - current.keys()):
            operations.append(
                JsonPatchOperation(
                    op="remove",
                    path=f"{path}/{_escape_json_pointer(key)}",
                )
            )
        for key in sorted(current.keys() - previous.keys()):
            operations.append(
                JsonPatchOperation(
                    op="add",
                    path=f"{path}/{_escape_json_pointer(key)}",
                    value=current[key],
                )
            )
        for key in sorted(previous.keys() & current.keys()):
            operations.extend(
                _json_patch(
                    previous[key],
                    current[key],
                    f"{path}/{_escape_json_pointer(key)}",
                )
            )
        return operations
    return [JsonPatchOperation(op="replace", path=path, value=current)]


def _validate_text_content_blocks(content: object) -> None:
    """Reject text blocks whose required payload is absent or not a string."""

    if not isinstance(content, list):
        return
    for block in cast(list[object], content):
        if not isinstance(block, Mapping):
            continue
        mapping = cast(Mapping[object, object], block)
        if mapping.get("type") != "text":
            continue
        if not isinstance(mapping.get("text"), str):
            raise TypeError("text content blocks require a string 'text' field")


def _normalize_tool_content(content: object) -> str:
    """Normalize Tool output according to the AG-UI adapter contract."""

    _validate_text_content_blocks(content)

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in cast(list[object], content):
            if attachment_from_block(block) is not None:
                continue
            if isinstance(block, str):
                parts.append(block)
            elif (
                isinstance(block, Mapping)
                and cast(Mapping[object, object], block).get("type") == "text"
            ):
                mapping = cast(Mapping[object, object], block)
                parts.append(cast(str, mapping["text"]))
            else:
                parts.append(
                    json.dumps(_to_json_value(cast(object, block)), ensure_ascii=False)
                )
        return "".join(parts)
    return json.dumps(_to_json_value(content), ensure_ascii=False)


def _visible_ai_content(message: AIMessage) -> str:
    """Serialize AI content without inferring provider privacy from domain fields.

    LangChain's ordinary text projection intentionally flattens text blocks, while
    AG-UI message snapshots have only a string content field. Preserve a non-text
    content graph as one JSON value so domain blocks are not silently discarded or
    unwrapped merely because their ``type`` resembles provider reasoning.
    """

    if isinstance(message.content, list) and content_attachments(message.content):
        message = message.model_copy(
            update={
                "content": [
                    block
                    for block in message.content
                    if attachment_from_block(block) is None
                ]
            }
        )
    if isinstance(message.content, list):
        has_non_text_block = any(
            not (
                isinstance(block, str)
                or (isinstance(block, Mapping) and block.get("type") == "text")
            )
            for block in message.content
        )
        if has_non_text_block:
            _validate_text_content_blocks(message.content)
            return json.dumps(
                sanitize_public_data(message.content),
                ensure_ascii=False,
            )
    return _normalize_tool_content(message.content)


def _complete_ai_message_to_chunk(message: AIMessage) -> AIMessageChunk:
    """Normalize a complete non-streaming AIMessage into one final chunk."""

    tool_call_chunks = [
        ToolCallChunk(
            name=tool_call["name"],
            args=json.dumps(_to_json_value(tool_call["args"]), ensure_ascii=False),
            id=tool_call["id"],
            index=index,
            type="tool_call_chunk",
        )
        for index, tool_call in enumerate(message.tool_calls)
    ]
    return AIMessageChunk(
        content=message.content,
        additional_kwargs=message.additional_kwargs,
        response_metadata=message.response_metadata,
        name=message.name,
        id=message.id,
        usage_metadata=message.usage_metadata,
        tool_call_chunks=tool_call_chunks,
        chunk_position="last",
    )


def _process_message_part(
    self: DeepAgentAgUiAdapter, part: MessageStreamPart
) -> list[BaseEvent]:
    """Route one validated message through its namespace-aware lifecycle.

    Native task-start evidence must establish every non-root source before message
    delivery. Tool results also recover their verified subagent invocation so public
    provenance never guesses parentage from arrival order or ``parentRunId``.
    """

    metadata = part.data.metadata
    agent_name = metadata.lc_agent_name
    message = part.data.message
    source_namespace = part.ns
    source = self._source(source_namespace)
    self._require_started_source(source)
    prior = (
        self._message_baselines.get((source_namespace, message.id))
        if isinstance(message.id, str)
        else None
    )
    if (
        prior is not None
        and isinstance(message, (AIMessage, ToolMessage))
        and not isinstance(message, AIMessageChunk)
        and prior == _message_fingerprint(message)
    ):
        return []
    self._record_agent_name(source_namespace, agent_name)
    source = self._source(source_namespace)
    events: list[BaseEvent] = []
    related_namespace: tuple[str, ...] | None = None
    related_subagent_invocation_id: str | None = None
    if isinstance(message, ToolMessage):
        scoped_tool_call_id = self._tool_call_id(
            part.ns,
            str(message.tool_call_id),
        )
        correlated_name = message.name or self._tool_names_by_id.get(
            scoped_tool_call_id
        )
        if correlated_name == "task":
            related_namespace = self._sub_namespaces_by_parent_tool_call.get(
                (part.ns, str(message.tool_call_id))
            )
            invocation = (
                None
                if related_namespace is None
                else self._subagent_invocations.get(related_namespace)
            )
            if invocation is not None:
                related_subagent_invocation_id = (
                    invocation.provenance.subagent_invocation_id
                )
    raw_event = self._event_context(
        "messages",
        source,
        langgraph_node=metadata.langgraph_node,
        related_namespace=related_namespace,
        related_subagent_invocation_id=related_subagent_invocation_id,
        tool_result_status=(
            message.status if isinstance(message, ToolMessage) else None
        ),
    )
    if isinstance(message, AIMessage):
        chunk = (
            message
            if isinstance(message, AIMessageChunk)
            else _complete_ai_message_to_chunk(message)
        )
        events.extend(self._process_ai_chunk(chunk, source, raw_event))
    elif isinstance(message, ToolMessage):
        events.extend(self._process_tool_result(message, source, raw_event))
    return events


def _process_ai_chunk(
    self: DeepAgentAgUiAdapter,
    chunk: AIMessageChunk,
    source: AgentSource,
    raw_event: dict[str, JsonValue],
) -> list[BaseEvent]:
    """Emit balanced reasoning, text, and Tool events for one AI message frame.

    Provider-final frames are hard boundaries even when empty. Tool fragments may
    interleave with text from the same message without ending it. AG-UI 0.1.19 marks
    TextMessageEnd as final; test_interleaved_public_stream.py covers these sequences.
    """

    events: list[BaseEvent] = []
    visible_content = _visible_ai_content(chunk)
    attachments = content_attachments(chunk.content)
    reasoning_events = self._convert_reasoning_events(
        chunk,
        source,
        raw_event,
    )
    # An empty final frame is a provider-declared hard boundary. Only an empty
    # non-final frame is a heartbeat that leaves active lifecycles unchanged.
    if (
        not visible_content
        and not attachments
        and not chunk.tool_call_chunks
        and not reasoning_events
    ):
        if chunk.chunk_position == "last":
            events.extend(self._close_tools(source.graph_namespace, raw_event))
            events.extend(self._close_reasoning(source.graph_namespace, raw_event))
            events.extend(self._close_message(source.graph_namespace, raw_event))
        return events

    active_message_id = self._active_messages.get(source.graph_namespace)
    if (
        chunk.id is not None
        and active_message_id is not None
        and self._message_id(source.graph_namespace, chunk.id) != active_message_id
    ):
        events.extend(self._close_message(source.graph_namespace, raw_event))

    events.extend(reasoning_events)

    text = visible_content
    if text:
        # Visible answer text closes reasoning for this message before text starts.
        events.extend(self._close_reasoning(source.graph_namespace, raw_event))
        raw_message_id = self._stable_message_id(chunk)
        message_id = self._message_id(source.graph_namespace, raw_message_id)
        active_message_id = self._active_messages.get(source.graph_namespace)
        if active_message_id != message_id:
            if active_message_id is not None:
                events.append(
                    TextMessageEndEvent(
                        message_id=active_message_id,
                        raw_event=raw_event,
                    )
                )
            self._active_messages[source.graph_namespace] = message_id
            events.append(
                TextMessageStartEvent(
                    message_id=message_id,
                    role="assistant",
                    name=source.agent_name,
                    raw_event=raw_event,
                )
            )
        events.append(
            TextMessageContentEvent(
                message_id=message_id,
                delta=text,
                raw_event=raw_event,
            )
        )

    if attachments:
        message_id = self._message_id(
            source.graph_namespace, self._stable_message_id(chunk)
        )
        events.extend(self._close_reasoning(source.graph_namespace, raw_event))
        if self._active_messages.get(source.graph_namespace) != message_id:
            events.extend(self._close_message(source.graph_namespace, raw_event))
            self._active_messages[source.graph_namespace] = message_id
            events.append(
                TextMessageStartEvent(
                    message_id=message_id,
                    role="assistant",
                    name=source.agent_name,
                    raw_event=raw_event,
                )
            )
        events.append(
            CustomEvent(
                name="tinkerfin.message.attachments",
                value=MessageAttachments.model_validate(
                    {"messageId": message_id, "attachments": attachments}
                ).model_dump(mode="json", by_alias=True),
                raw_event=raw_event,
            )
        )

    if chunk.tool_call_chunks:
        events.extend(self._close_reasoning(source.graph_namespace, raw_event))
        for tool_chunk in chunk.tool_call_chunks:
            events.extend(
                self._process_tool_chunk(
                    tool_chunk,
                    chunk,
                    source,
                    raw_event,
                )
            )
    if chunk.chunk_position == "last":
        events.extend(self._close_tools(source.graph_namespace, raw_event))
        events.extend(self._close_reasoning(source.graph_namespace, raw_event))
        events.extend(self._close_message(source.graph_namespace, raw_event))
    return events


def _process_tool_chunk(
    self: DeepAgentAgUiAdapter,
    tool_chunk: ToolCallChunk,
    chunk: AIMessageChunk,
    source: AgentSource,
    raw_event: dict[str, JsonValue],
) -> list[BaseEvent]:
    """Correlate arguments by full scope, message ID, and provider index or Tool ID.

    Later provider fragments may omit both Tool name and ID, so the previously opened
    scoped slot is authoritative. Every non-empty argument fragment, including the
    first, is appended exactly once; a conflicting ID closes the old slot first.
    """

    index = tool_chunk.get("index")
    raw_source_message_id = self._stable_message_id(chunk)
    source_message_id = self._message_id(
        source.graph_namespace,
        raw_source_message_id,
    )
    chunk_id = tool_chunk.get("id")
    index_key = (
        source.graph_namespace,
        source_message_id,
        index
        if index is not None
        else self._tool_call_id(source.graph_namespace, str(chunk_id)),
    )
    tool_call_id = (
        self._tool_call_id(source.graph_namespace, str(chunk_id))
        if chunk_id
        else self._tool_ids_by_slot.get(index_key)
    )
    name_value = tool_chunk.get("name")
    tool_name = name_value if name_value else None
    events: list[BaseEvent] = []

    if tool_call_id is None:
        raise RuntimeError("validated tool fragment lost its scoped start")

    if index_key in self._tool_ids_by_slot:
        previous_id = self._tool_ids_by_slot[index_key]
        if previous_id and previous_id != tool_call_id:
            events.extend(self._close_tool(str(previous_id), raw_event))

    active = self._active_tools.get(tool_call_id)
    if active is None and tool_call_id not in self._ended_tool_ids:
        if not tool_name:
            return events
        active = ActiveToolCall(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            parent_message_id=source_message_id,
            namespace=source.graph_namespace,
            index=index,
            order=len(self._started_tool_ids),
        )
        self._active_tools[tool_call_id] = active
        self._tool_names_by_id[tool_call_id] = tool_name
        self._tool_history.setdefault((source.graph_namespace, tool_name), []).append(
            active
        )
        self._tool_ids_by_slot[index_key] = tool_call_id
        self._tool_id_history_by_slot[index_key] = tool_call_id
        self._started_tool_ids.add(tool_call_id)
        events.append(
            ToolCallStartEvent(
                tool_call_id=tool_call_id,
                tool_call_name=tool_name,
                parent_message_id=active.parent_message_id,
                raw_event=raw_event,
            )
        )

    args_value = tool_chunk.get("args")
    if active is not None and isinstance(args_value, str) and args_value:
        active.arguments += args_value
        events.append(
            ToolCallArgsEvent(
                tool_call_id=tool_call_id,
                delta=args_value,
                raw_event=raw_event,
            )
        )
    return events


def _process_tool_result(
    self: DeepAgentAgUiAdapter,
    message: ToolMessage,
    source: AgentSource,
    raw_event: dict[str, JsonValue],
) -> list[BaseEvent]:
    """Complete one scoped Tool call with replay-safe result idempotency.

    Exact result replays emit nothing and conflicting replays fail closed. A resumed
    result may legitimately arrive after its proposal lifecycle was emitted by an
    earlier request; unknown result-only sources receive one complete synthetic start
    and end before the result rather than an orphan event.
    """

    tool_call_id = self._tool_call_id(
        source.graph_namespace,
        str(message.tool_call_id),
    )
    message_id = self._message_id(
        source.graph_namespace,
        str(message.id or f"tool-result:{message.tool_call_id}"),
    )
    normalized_content = _normalize_tool_content(
        normalize_operational_data(message.content)
    )
    known_tool_name = self._tool_names_by_id.get(tool_call_id)
    if (
        message.name is not None
        and known_tool_name is not None
        and known_tool_name != message.name
    ):
        raise ValueError(
            f"ToolMessage name does not match its scoped tool call ID: {tool_call_id}"
        )
    effective_tool_name = known_tool_name or message.name or "tool"
    fingerprint = ToolResultFingerprint(
        message_id=message_id,
        tool_name=effective_tool_name,
        content=json.dumps(
            [normalized_content, content_attachments(message.content)],
            ensure_ascii=False,
            sort_keys=True,
        ),
        status=message.status,
    )
    previous = self._result_fingerprints.get(tool_call_id)
    if previous is not None:
        if previous == fingerprint:
            return []
        raise ValueError(
            f"conflicting ToolMessage for scoped tool call ID: {tool_call_id}"
        )
    events: list[BaseEvent] = []
    # A Tool result is a hard message boundary, including for providers that omit
    # the `last` marker on their final AI chunk.
    events.extend(self._close_reasoning(source.graph_namespace, raw_event))
    events.extend(self._close_message(source.graph_namespace, raw_event))
    if tool_call_id not in self._started_tool_ids:
        self._tool_names_by_id[tool_call_id] = effective_tool_name
        events.append(
            ToolCallStartEvent(
                tool_call_id=tool_call_id,
                tool_call_name=effective_tool_name,
                # A result-only replay cannot recover the proposing assistant message.
                parent_message_id=None,
                raw_event=raw_event,
            )
        )
        self._started_tool_ids.add(tool_call_id)
    if tool_call_id not in self._ended_tool_ids and (
        tool_call_id in self._active_tools or tool_call_id in self._started_tool_ids
    ):
        events.extend(self._close_tool(tool_call_id, raw_event))
        if tool_call_id not in self._ended_tool_ids:
            self._ended_tool_ids.add(tool_call_id)
            events.append(
                ToolCallEndEvent(
                    tool_call_id=tool_call_id,
                    raw_event=raw_event,
                )
            )
    self._result_fingerprints[tool_call_id] = fingerprint
    self._prior_tool_call_ids.discard(tool_call_id)
    events.append(
        AttachmentToolCallResultEvent(
            message_id=message_id,
            tool_call_id=tool_call_id,
            content=normalized_content,
            role="tool",
            raw_event=raw_event,
            attachments=[
                Attachment.model_validate(item)
                for item in content_attachments(message.content)
            ],
        )
    )
    return events


def _convert_reasoning_events(
    self: DeepAgentAgUiAdapter,
    chunk: AIMessageChunk,
    source: AgentSource,
    raw_event: dict[str, JsonValue],
) -> list[BaseEvent]:
    """Convert model reasoning events only when the client enables them."""

    if not self._expose_reasoning_events:
        return []

    events: list[BaseEvent] = []
    for delta in self._reasoning_deltas(chunk):
        events.extend(self._emit_reasoning(delta, chunk, source, raw_event))
    return events


def _reasoning_deltas(chunk: AIMessageChunk) -> list[str]:
    """Read reasoning deltas only from the locked provider metadata path."""
    reasoning = chunk.additional_kwargs.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return [reasoning]
    return []


def _emit_reasoning(
    self: DeepAgentAgUiAdapter,
    delta: str,
    chunk: AIMessageChunk,
    source: AgentSource,
    raw_event: dict[str, JsonValue],
) -> list[BaseEvent]:
    """Emit one opt-in reasoning delta under a message-scoped balanced lifecycle.

    A new source message closes the prior reasoning stream in the same namespace.
    Reasoning IDs are separate from visible message IDs, while raw provider metadata is
    still removed independently by the public sanitizer.
    """

    raw_source_message_id = self._stable_message_id(chunk)
    source_message_id = self._message_id(
        source.graph_namespace,
        raw_source_message_id,
    )
    run_id = self._identity.run_id
    key = (run_id, source_message_id)
    events = self._close_message(source.graph_namespace, raw_event)
    active = self._active_reasoning.get(key)
    if active is None:
        # Model calls are sequential within a run, so a new source message closes
        # the previous reasoning stream.
        events.extend(
            self._close_reasoning(
                source.graph_namespace,
                raw_event,
            )
        )
        active = ActiveReasoning(
            run_id=run_id,
            source_message_id=source_message_id,
            reasoning_id=self._ids.encode(
                "reasoning",
                source.graph_namespace,
                raw_source_message_id,
            ),
            message_id=self._ids.encode(
                "reasoning-message",
                source.graph_namespace,
                raw_source_message_id,
            ),
            namespace=source.graph_namespace,
        )
        self._active_reasoning[key] = active
        events.append(
            ReasoningStartEvent(
                message_id=active.reasoning_id,
                raw_event=raw_event,
            )
        )
        events.append(
            ReasoningMessageStartEvent(
                message_id=active.message_id,
                role="reasoning",
                raw_event=raw_event,
            )
        )
    events.append(
        ReasoningMessageContentEvent(
            message_id=active.message_id,
            delta=delta,
            raw_event=raw_event,
        )
    )
    return events


def _close_reasoning(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    raw_event: dict[str, JsonValue],
) -> list[BaseEvent]:
    events: list[BaseEvent] = []
    for key, active in list(self._active_reasoning.items()):
        if active.namespace != namespace:
            continue
        self._active_reasoning.pop(key)
        events.extend(
            [
                ReasoningMessageEndEvent(
                    message_id=active.message_id,
                    raw_event=raw_event,
                ),
                ReasoningEndEvent(
                    message_id=active.reasoning_id,
                    raw_event=raw_event,
                ),
            ]
        )
    return events


def _close_message(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    raw_event: dict[str, JsonValue],
) -> list[BaseEvent]:
    message_id = self._active_messages.pop(namespace, None)
    if message_id is None:
        return []
    return [TextMessageEndEvent(message_id=message_id, raw_event=raw_event)]


def _close_tool(
    self: DeepAgentAgUiAdapter,
    tool_call_id: str,
    raw_event: dict[str, JsonValue],
) -> list[BaseEvent]:
    active = self._active_tools.pop(tool_call_id, None)
    if active is None or tool_call_id in self._ended_tool_ids:
        return []
    if active.parent_message_id is not None:
        self._tool_ids_by_slot.pop(
            (
                active.namespace,
                active.parent_message_id,
                active.index if active.index is not None else active.tool_call_id,
            ),
            None,
        )
    self._ended_tool_ids.add(tool_call_id)
    return [ToolCallEndEvent(tool_call_id=tool_call_id, raw_event=raw_event)]


def _close_tools(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    raw_event: dict[str, JsonValue],
) -> list[BaseEvent]:
    events: list[BaseEvent] = []
    for tool_call_id, active in list(self._active_tools.items()):
        if active.namespace == namespace:
            events.extend(self._close_tool(tool_call_id, raw_event))
    return events


def _close_all_reasoning(self: DeepAgentAgUiAdapter) -> list[BaseEvent]:
    events: list[BaseEvent] = []
    namespaces = list(
        dict.fromkeys(active.namespace for active in self._active_reasoning.values())
    )
    for namespace in namespaces:
        source = self._source(namespace)
        events.extend(
            self._close_reasoning(
                namespace,
                self._event_context("messages", source),
            )
        )
    return events


def _close_all_messages(self: DeepAgentAgUiAdapter) -> list[BaseEvent]:
    events: list[BaseEvent] = []
    for namespace in list(self._active_messages):
        source = self._source(namespace)
        events.extend(
            self._close_message(
                namespace,
                self._event_context("messages", source),
            )
        )
    return events


def _close_all_tools(self: DeepAgentAgUiAdapter) -> list[BaseEvent]:
    events: list[BaseEvent] = []
    for tool_call_id, active in list(self._active_tools.items()):
        source = self._source(active.namespace)
        events.extend(
            self._close_tool(
                tool_call_id,
                self._event_context("messages", source),
            )
        )
    return events


def _record_agent_name(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    agent_name: str | None,
) -> None:
    invocation = self._subagent_invocations.get(namespace)
    if invocation is None or not agent_name:
        return
    if invocation.agent_name != agent_name:
        raise ValueError(
            "task subagent_type does not match streamed lc_agent_name: "
            f"namespace={namespace!r} expected={invocation.agent_name!r} "
            f"actual={agent_name!r}"
        )
    self._namespace_agent_names[namespace] = agent_name


def _require_started_source(self: DeepAgentAgUiAdapter, source: AgentSource) -> None:
    if source.kind == "root":
        return
    if source.graph_namespace not in self._graph_scopes:
        raise RuntimeError(
            "subgraph stream arrived before its native task-start correlation: "
            f"namespace={source.graph_namespace!r}"
        )


def _source(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
) -> AgentSource:
    """Resolve graph provenance only from previously correlated Native task facts.

    Root, verified Deep Agents delegation, and ordinary compiled subgraphs remain
    distinct. An unknown non-root namespace is represented conservatively and is never
    promoted to a subagent solely because its namespace is non-empty.
    """

    if not namespace:
        return AgentSource(
            kind="root",
            agent_type="main",
            agent_name="main",
            graph_namespace=namespace,
        )
    invocation = self._subagent_invocations.get(namespace)
    if invocation is not None:
        return AgentSource(
            kind="deep_agent_subagent",
            graph_namespace=namespace,
            parent_graph_namespace=invocation.parent_namespace,
            graph_task_id=invocation.graph_task_id,
            node_name="tools",
            agent_type="subagent",
            agent_name=self._namespace_agent_names.get(
                namespace, invocation.agent_name
            ),
            parent_tool_call_id=invocation.parent_tool_call_id,
            subagent_input=invocation.subagent_input,
            subagent_invocation_id=(invocation.provenance.subagent_invocation_id),
        )
    scope = self._graph_scopes.get(namespace)
    if scope is None:
        return AgentSource(kind="compiled_subgraph", graph_namespace=namespace)
    return AgentSource(
        kind="compiled_subgraph",
        graph_namespace=namespace,
        parent_graph_namespace=scope.parent_namespace,
        graph_task_id=scope.graph_task_id,
        node_name=scope.node_name,
    )


def _event_context(
    self: DeepAgentAgUiAdapter,
    stream_mode: StreamMode,
    source: AgentSource,
    *,
    langgraph_node: str | None = None,
    interrupt_id: str | None = None,
    related_namespace: tuple[str, ...] | None = None,
    related_subagent_invocation_id: str | None = None,
    parent_tool_call_id: str | None = None,
    tool_result_status: Literal["success", "error"] | None = None,
) -> dict[str, JsonValue]:
    """Build the finite raw-event provenance shared by every emitted AG-UI event.

    The context retains full namespaces and correlation IDs but never serializes live
    LangGraph objects or repurposes AG-UI branch lineage for subagent relationships.
    """

    context = EventContext(
        stream_mode=stream_mode,
        source=source,
        run_id=self._identity.run_id,
        related_graph_namespace=related_namespace,
        related_subagent_invocation_id=related_subagent_invocation_id,
        parent_tool_call_id=parent_tool_call_id,
        langgraph_node=langgraph_node,
        interrupt_id=interrupt_id,
        tool_result_status=tool_result_status,
    )
    return cast(
        dict[str, JsonValue],
        context.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def _stable_message_id(message: AIMessage) -> str:
    """Require the framework-provided stable ID used for cross-frame correlation."""

    if not isinstance(message.id, str) or not message.id:
        raise ValueError("AI messages require a stable ID")
    return message.id


def _message_id(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    raw_id: str,
) -> str:
    """Encode a native message ID as a namespace-aware AG-UI ID."""

    return self._ids.encode("message", namespace, raw_id)


def _tool_call_id(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    raw_id: str,
) -> str:
    """Encode a native Tool call ID as a namespace-aware AG-UI ID."""

    return self._ids.encode("tool", namespace, raw_id)


def _convert_messages(
    self: DeepAgentAgUiAdapter,
    raw_messages: object,
    namespace: tuple[str, ...],
) -> list[Message]:
    """Project complete checkpoint messages into an authoritative AG-UI snapshot."""

    if not isinstance(raw_messages, Sequence) or isinstance(
        raw_messages,
        (str, bytes),
    ):
        raise TypeError("values messages must be a sequence")
    messages = cast(Sequence[object], raw_messages)
    converted: list[Message] = []
    for index, message in enumerate(messages):
        if not isinstance(message, BaseMessage):
            raise TypeError("values messages must contain LangChain messages")
        raw_message_id = (
            self._stable_message_id(message)
            if isinstance(message, AIMessage)
            else str(message.id or f"state-message-{index}")
        )
        message_id = self._message_id(namespace, raw_message_id)
        if isinstance(message, HumanMessage):
            converted.append(
                UserMessage.model_validate(
                    {
                        "id": message_id,
                        "content": user_content_to_agui(message.content),
                        "name": message.name,
                    }
                )
            )
            continue
        if isinstance(message, SystemMessage):
            converted.append(
                AgUiSystemMessage(
                    id=message_id,
                    content=_normalize_tool_content(message.content),
                    name=message.name,
                )
            )
            continue
        if isinstance(message, ToolMessage):
            converted.append(
                AttachmentToolMessage(
                    id=message_id,
                    content=_normalize_tool_content(message.content),
                    error=(
                        _normalize_tool_content(message.content)
                        or "Tool execution failed"
                        if message.status == "error"
                        else None
                    ),
                    tool_call_id=self._tool_call_id(
                        namespace,
                        str(message.tool_call_id),
                    ),
                    attachments=[
                        Attachment.model_validate(item)
                        for item in content_attachments(message.content)
                    ],
                )
            )
            continue
        if isinstance(message, AIMessage):
            tool_calls: list[ToolCall] = []
            for call in message.tool_calls:
                raw_call_id = call.get("id")
                name = call.get("name")
                if not raw_call_id or not isinstance(name, str) or not name:
                    raise ValueError(
                        "assistant snapshot tool calls require stable IDs and names"
                    )
                arguments = json.dumps(
                    _to_json_value(call.get("args", {})),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                tool_calls.append(
                    ToolCall(
                        id=self._tool_call_id(namespace, str(raw_call_id)),
                        function=FunctionCall(
                            name=name,
                            arguments=arguments,
                        ),
                    )
                )
            public_metadata = sanitize_public_data(
                {
                    "additional_kwargs": message.additional_kwargs,
                    "response_metadata": message.response_metadata,
                }
            )
            if not isinstance(public_metadata, dict):
                raise TypeError("assistant metadata must be a JSON object")
            converted.append(
                AttachmentAssistantMessage.model_validate(
                    {
                        "id": message_id,
                        "content": _visible_ai_content(message),
                        "attachments": content_attachments(message.content),
                        "name": message.name,
                        "toolCalls": tool_calls or None,
                        **public_metadata,
                    }
                )
            )
            continue
        raise ValueError(
            f"unsupported checkpoint message type: {type(message).__name__}"
        )
    return converted
