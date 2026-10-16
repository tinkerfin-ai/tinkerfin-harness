"""LangGraph v3 event-stream carrier for the canonical Native Runtime boundary."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.stream import (
    AsyncGraphRunStream,
    ProtocolEvent,
    StreamChannel,
    StreamTransformer,
)

from .errors import TinkerFinStreamProtocolError

_CHANNEL_NAME = "tinkerfin_native"
_STREAM_MODES = (
    "messages",
    "tasks",
    "values",
    "updates",
    "checkpoints",
    "debug",
    "custom",
)


class _CanonicalEventTransformer(StreamTransformer):
    """Retain ordered raw v3 events needed by the current Runtime contract."""

    before_builtins = True
    required_stream_modes = _STREAM_MODES

    def __init__(self, scope: tuple[str, ...] = ()) -> None:
        super().__init__(scope)
        self._events: StreamChannel[ProtocolEvent] = StreamChannel()

    def init(self) -> dict[str, object]:
        """Expose one request-owned projection without changing the main event log."""

        return {_CHANNEL_NAME: self._events}

    def process(self, event: ProtocolEvent) -> bool:
        """Copy one ordered protocol event into the canonical projection."""

        if event["method"] in self.required_stream_modes:
            self._events.push(event)
        return True


@dataclass(slots=True)
class _MessageState:
    message_id: str
    emitted: bool = False


class _V3EventConverter:
    """Convert one v3 ProtocolEvent sequence into current live Native parts."""

    def __init__(self) -> None:
        self._messages: dict[tuple[tuple[str, ...], str], _MessageState] = {}
        self._tool_messages: set[tuple[tuple[str, ...], str]] = set()

    def convert(self, event: object) -> tuple[Mapping[str, object], ...]:
        """Return ordered canonical parts derived from one upstream event."""

        raw_event = _mapping(event, name="v3 event")
        if raw_event.get("type") != "event":
            raise TinkerFinStreamProtocolError("v3 stream event type must be 'event'")
        method = raw_event.get("method")
        if not isinstance(method, str) or method not in _STREAM_MODES:
            raise TinkerFinStreamProtocolError("v3 stream event method is unsupported")
        params = _mapping(raw_event.get("params"), name="v3 event params")
        namespace = _namespace(params.get("namespace"))
        if "data" not in params:
            raise TinkerFinStreamProtocolError("v3 stream event params require data")
        data = params["data"]
        if method == "messages":
            converted = self._message(namespace, data)
            if converted is None:
                return ()
            return ({"type": "messages", "ns": namespace, "data": converted},)
        result: dict[str, object] = {
            "type": method,
            "ns": namespace,
            "data": data,
        }
        if method == "values":
            raw_interrupts = params.get("interrupts", ())
            if isinstance(raw_interrupts, str | bytes) or not isinstance(
                raw_interrupts, Sequence
            ):
                raise TinkerFinStreamProtocolError(
                    "v3 values interrupts must be a sequence"
                )
            result["interrupts"] = tuple(cast(Sequence[object], raw_interrupts))
            return (*self._new_tool_messages(namespace, data), result)
        return (result,)

    def _new_tool_messages(
        self,
        namespace: tuple[str, ...],
        data: object,
    ) -> tuple[Mapping[str, object], ...]:
        """Restore stable ToolMessage events omitted by the v3 messages channel."""

        if not isinstance(data, Mapping):
            return ()
        raw_messages = cast(Mapping[object, object], data).get("messages", ())
        if isinstance(raw_messages, str | bytes) or not isinstance(
            raw_messages, Sequence
        ):
            return ()
        parts: list[Mapping[str, object]] = []
        for message in cast(Sequence[object], raw_messages):
            if not isinstance(message, ToolMessage):
                continue
            key = self._tool_message_key(namespace, message)
            if key in self._tool_messages:
                continue
            self._tool_messages.add(key)
            parts.append(
                {
                    "type": "messages",
                    "ns": namespace,
                    "data": (message, {"langgraph_node": "tools"}),
                }
            )
        return tuple(parts)

    @staticmethod
    def _tool_message_key(
        namespace: tuple[str, ...],
        message: ToolMessage,
    ) -> tuple[tuple[str, ...], str]:
        identity = message.id or message.tool_call_id
        if not isinstance(identity, str) or not identity:
            raise TinkerFinStreamProtocolError(
                "v3 ToolMessage requires a stable message or Tool call ID"
            )
        return namespace, identity

    def _message(
        self,
        namespace: tuple[str, ...],
        data: object,
    ) -> tuple[BaseMessage, Mapping[str, object]] | None:
        if not isinstance(data, tuple) or len(cast(tuple[object, ...], data)) != 2:
            raise TinkerFinStreamProtocolError(
                "v3 messages data must contain payload and metadata"
            )
        payload, raw_metadata = cast(tuple[object, object], data)
        metadata = _mapping(raw_metadata, name="v3 message metadata")
        if isinstance(payload, ToolMessage):
            self._tool_messages.add(self._tool_message_key(namespace, payload))
            return payload, metadata
        if isinstance(payload, AIMessage):
            return payload, metadata
        protocol = _mapping(payload, name="v3 message protocol event")
        event_type = protocol.get("event")
        if not isinstance(event_type, str):
            raise TinkerFinStreamProtocolError(
                "v3 message protocol event requires an event name"
            )
        run_id = metadata.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise TinkerFinStreamProtocolError(
                "v3 message protocol event requires a callback run ID"
            )
        key = (namespace, run_id)
        if event_type == "message-start":
            message_id = protocol.get("id", protocol.get("message_id"))
            if not isinstance(message_id, str) or not message_id:
                raise TinkerFinStreamProtocolError(
                    "v3 message start requires a stable message ID"
                )
            if key in self._messages:
                raise TinkerFinStreamProtocolError(
                    "v3 message callback started more than once"
                )
            self._messages[key] = _MessageState(message_id=message_id)
            return None
        state = self._messages.get(key)
        if state is None:
            raise TinkerFinStreamProtocolError(
                "v3 message content has no matching message start"
            )
        if event_type == "message-finish":
            self._messages.pop(key, None)
            if state.emitted:
                return None
            return AIMessageChunk(content="", id=state.message_id), metadata
        if event_type == "content-block-start":
            _mapping(protocol.get("content"), name="v3 content block")
            return None
        if event_type == "content-block-delta":
            delta = _mapping(protocol.get("delta"), name="v3 content delta")
            chunk = _chunk_from_delta(
                state.message_id,
                protocol.get("index"),
                delta,
                metadata,
            )
            state.emitted = chunk is not None or state.emitted
            return chunk
        if event_type == "content-block-finish":
            content = _mapping(protocol.get("content"), name="v3 finalized block")
            chunk = _chunk_from_finished_block(
                state.message_id,
                content,
                metadata,
            )
            state.emitted = chunk is not None or state.emitted
            return chunk
        raise TinkerFinStreamProtocolError(
            "v3 message protocol event name is unsupported"
        )


def _chunk_from_delta(
    message_id: str,
    raw_index: object,
    delta: Mapping[str, object],
    metadata: Mapping[str, object],
) -> tuple[BaseMessage, Mapping[str, object]] | None:
    delta_type = delta.get("type")
    if delta_type == "text-delta":
        text = delta.get("text")
        if not isinstance(text, str):
            raise TinkerFinStreamProtocolError("v3 text delta must contain text")
        return AIMessageChunk(content=text, id=message_id), metadata
    if delta_type == "reasoning-delta":
        reasoning = delta.get("reasoning")
        if not isinstance(reasoning, str):
            raise TinkerFinStreamProtocolError(
                "v3 reasoning delta must contain reasoning text"
            )
        return (
            AIMessageChunk(
                content="",
                id=message_id,
                additional_kwargs={"reasoning_content": reasoning},
            ),
            metadata,
        )
    if delta_type == "block-delta":
        fields = _mapping(delta.get("fields"), name="v3 block delta fields")
        return _chunk_from_block(
            message_id,
            raw_index,
            fields,
            metadata,
            emit_empty=True,
        )
    return AIMessageChunk(content=[dict(delta)], id=message_id), metadata


def _chunk_from_block(
    message_id: str,
    raw_index: object,
    block: Mapping[str, object],
    metadata: Mapping[str, object],
    *,
    emit_empty: bool,
) -> tuple[BaseMessage, Mapping[str, object]] | None:
    block_type = block.get("type")
    if block_type in {"text", "reasoning"}:
        return None
    if block_type in {
        "tool_call",
        "tool_call_chunk",
        "server_tool_call",
        "server_tool_call_chunk",
    }:
        index = raw_index if isinstance(raw_index, int) and raw_index >= 0 else 0
        raw_args = block.get("args", "")
        if isinstance(raw_args, str):
            arguments = raw_args
        else:
            arguments = json.dumps(
                raw_args,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        raw_id = block.get("id")
        raw_name = block.get("name")
        call_id = raw_id if isinstance(raw_id, str) and raw_id else None
        name = raw_name if isinstance(raw_name, str) and raw_name else None
        if not emit_empty and not arguments and call_id is None and name is None:
            return None
        return (
            AIMessageChunk(
                content="",
                id=message_id,
                tool_call_chunks=[
                    {
                        "type": "tool_call_chunk",
                        "index": index,
                        "id": call_id,
                        "name": name,
                        "args": arguments,
                    }
                ],
            ),
            metadata,
        )
    return AIMessageChunk(content=[dict(block)], id=message_id), metadata


def _chunk_from_finished_block(
    message_id: str,
    block: Mapping[str, object],
    metadata: Mapping[str, object],
) -> tuple[BaseMessage, Mapping[str, object]] | None:
    """Preserve self-contained blocks without replaying accumulated deltas."""

    if block.get("type") in {
        "text",
        "reasoning",
        "tool_call",
        "tool_call_chunk",
        "server_tool_call",
        "server_tool_call_chunk",
        "invalid_tool_call",
    }:
        return None
    return AIMessageChunk(content=[dict(block)], id=message_id), metadata


def _mapping(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TinkerFinStreamProtocolError(f"{name} must be a mapping")
    mapping = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in mapping):
        raise TinkerFinStreamProtocolError(f"{name} keys must be strings")
    return cast(Mapping[str, object], mapping)


def _namespace(value: object) -> tuple[str, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TinkerFinStreamProtocolError("v3 event namespace must be a sequence")
    namespace = tuple(cast(Sequence[object], value))
    if any(not isinstance(segment, str) or not segment for segment in namespace):
        raise TinkerFinStreamProtocolError(
            "v3 event namespace must contain non-empty strings"
        )
    return cast(tuple[str, ...], namespace)


def graph_v3_stream(
    graph: object,
) -> Callable[..., AsyncIterator[Mapping[str, object]]]:
    """Return one v2-shaped canonical source over a public v3 run stream."""

    astream_events = getattr(graph, "astream_events", None)
    if not callable(astream_events):
        raise TypeError("Deep Agents v3 graph must expose astream_events")

    def stream(
        input: object,
        config: RunnableConfig | None = None,
        **raw_options: object,
    ) -> AsyncIterator[Mapping[str, object]]:
        async def events() -> AsyncIterator[Mapping[str, object]]:
            options = dict(raw_options)
            raw_modes = options.pop("stream_mode", _STREAM_MODES)
            if isinstance(raw_modes, str):
                modes = frozenset({raw_modes})
            elif isinstance(raw_modes, Sequence) and not isinstance(raw_modes, bytes):
                values = tuple(cast(Sequence[object], raw_modes))
                if any(not isinstance(value, str) for value in values):
                    raise TypeError("v3 canonical stream modes must be strings")
                modes = frozenset(cast(tuple[str, ...], values))
            else:
                raise TypeError("v3 canonical stream_mode must be a string or sequence")
            unsupported = modes.difference(_STREAM_MODES)
            if unsupported:
                raise ValueError(
                    f"v3 canonical stream modes are unsupported: {sorted(unsupported)!r}"
                )
            if options.pop("version", "v3") != "v3":
                raise ValueError("v3 canonical source requires version='v3'")
            if options.pop("subgraphs", True) is not True:
                raise ValueError("v3 canonical source requires subgraphs=True")
            options.pop("output_keys", None)
            raw_transformers = options.pop("transformers", ())
            if isinstance(raw_transformers, str | bytes) or not isinstance(
                raw_transformers, Sequence
            ):
                raise TypeError("v3 transformers must be a sequence")
            transformers = tuple(cast(Sequence[object], raw_transformers))
            if any(not callable(value) for value in transformers):
                raise TypeError("v3 transformers must contain callables")

            requested_modes = tuple(mode for mode in _STREAM_MODES if mode in modes)

            class _RequestedCanonicalEventTransformer(_CanonicalEventTransformer):
                required_stream_modes = requested_modes

            def canonical_transformer(
                scope: tuple[str, ...],
            ) -> _CanonicalEventTransformer:
                return _RequestedCanonicalEventTransformer(scope)

            pending = astream_events(
                input,
                config,
                version="v3",
                transformers=(canonical_transformer, *transformers),
                **options,
            )
            if not inspect.isawaitable(pending):
                raise TypeError(
                    "Deep Agents v3 astream_events must return an awaitable"
                )
            run = await pending
            if not isinstance(run, AsyncGraphRunStream):
                raise TypeError(
                    "Deep Agents v3 astream_events must return AsyncGraphRunStream"
                )
            raw_channel = run.extensions.get(_CHANNEL_NAME)
            if not isinstance(raw_channel, StreamChannel):
                raise TypeError("Deep Agents v3 canonical event channel is unavailable")
            channel = cast(StreamChannel[ProtocolEvent], raw_channel)
            converter = _V3EventConverter()
            async with run:
                async for event in channel:
                    for part in converter.convert(event):
                        if part["type"] in modes:
                            yield part

        return events()

    return stream


__all__ = ["graph_v3_stream"]
