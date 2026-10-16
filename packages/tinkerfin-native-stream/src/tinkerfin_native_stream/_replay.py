"""Restore public Native semantics from finite Runtime records."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from tinkerfin_contracts import NativeInterruptRecord, NativeMessageRecord

from .errors import NativeStreamContractError
from .frame import NativeStreamFrame
from .stream import validate_native_stream_part

if TYPE_CHECKING:
    from .serialization import NativeStreamPart


class _ReplayPayload(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid", strict=True)


class _MessagePayload(_ReplayPayload):
    message: NativeMessageRecord
    metadata: dict[str, JsonValue]


class _TaskPayload(_ReplayPayload):
    phase: Literal["start", "result"]
    task_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    triggers: tuple[str, ...]
    input: JsonValue
    result: JsonValue
    error_type: str | None
    interrupts: tuple[NativeInterruptRecord, ...]
    metadata: dict[str, JsonValue]


class _StatePayload(_ReplayPayload):
    state: dict[str, JsonValue]
    messages: tuple[NativeMessageRecord, ...]
    messages_present: bool
    interrupts: tuple[NativeInterruptRecord, ...]


def _message(record: NativeMessageRecord) -> BaseMessage:
    values: dict[str, object] = {
        "id": record.id,
        "name": record.name,
        "content": record.content,
        "response_metadata": record.response_metadata,
    }
    if record.source is not None:
        values["additional_kwargs"] = {
            "tinkerfin_source": record.source.model_dump(mode="json")
        }
    if record.message_type in {"assistant", "assistant_chunk"}:
        values["usage_metadata"] = record.usage_metadata
        if record.message_type == "assistant_chunk":
            values["chunk_position"] = record.chunk_position
            values["tool_call_chunks"] = [
                {
                    "id": call.id,
                    "name": call.name,
                    "args": call.arguments,
                    "index": call.index,
                }
                for call in record.tool_call_chunks
            ]
            return AIMessageChunk.model_validate(values)
        values["tool_calls"] = [
            {
                "id": call.id,
                "name": call.name,
                "args": call.arguments,
                "type": "tool_call",
            }
            for call in record.tool_calls
        ]
        return AIMessage.model_validate(values)
    if record.message_type == "tool":
        values.update(
            tool_call_id=record.tool_call_id,
            status=record.tool_status or "success",
            artifact=record.artifact,
        )
        return ToolMessage.model_validate(values)
    if record.message_type == "human":
        return HumanMessage.model_validate(values)
    if record.message_type == "system":
        return SystemMessage.model_validate(values)
    if record.message_type == "remove":
        return RemoveMessage.model_validate(values)
    if record.message_type == "chat":
        values["role"] = record.chat_role
        return ChatMessage.model_validate(values)
    raise NativeStreamContractError("Native replay has an unsupported message type")


def replay_frame(part: NativeStreamPart) -> NativeStreamFrame:
    """Validate one finite record and restore only its supported public values."""

    try:
        encoded = json.dumps(part.data, allow_nan=False)
        nested_interrupts: tuple[NativeInterruptRecord, ...] = ()
        if part.mode == "messages":
            message = _MessagePayload.model_validate_json(encoded)
            data: object = (_message(message.message), message.metadata)
        elif part.mode == "tasks":
            task = _TaskPayload.model_validate_json(encoded)
            nested_interrupts = task.interrupts
            if task.phase == "start":
                if (
                    task.result is not None
                    or task.error_type is not None
                    or task.interrupts
                ):
                    raise ValueError("Native task start cannot carry terminal fields")
                # A missing SDK metadata object is recorded as {}; a present
                # NativeStreamMetadata retains its declared fields, including None.
                data = {
                    "id": task.task_id,
                    "name": task.name,
                    "input": task.input,
                    "triggers": task.triggers,
                    "metadata": task.metadata or None,
                }
            else:
                if task.input is not None or task.triggers or task.metadata:
                    raise ValueError("Native task result cannot carry start fields")
                data = {
                    "id": task.task_id,
                    "name": task.name,
                    "result": task.result,
                    "error": task.error_type,
                    "interrupts": [item.model_dump() for item in task.interrupts],
                }
        elif part.mode == "values":
            state = _StatePayload.model_validate_json(encoded)
            nested_interrupts = state.interrupts
            if "messages" in state.state:
                raise ValueError(
                    "Native state message records must use their explicit field"
                )
            if state.messages and not state.messages_present:
                raise ValueError(
                    "Native state has messages without their declared channel"
                )
            state_values: dict[str, object] = dict(state.state)
            if state.messages_present:
                state_values["messages"] = [_message(item) for item in state.messages]
            data = state_values
        else:
            data = part.data
        if part.interrupts != tuple(
            item.model_dump(mode="json", by_alias=True) for item in nested_interrupts
        ):
            raise ValueError("Native replay interrupt fields disagree")
        envelope: dict[str, object] = {
            "type": part.mode,
            "ns": part.graph_namespace,
            "data": data,
        }
        if part.mode == "values":
            envelope["interrupts"] = tuple(
                item.model_dump() for item in nested_interrupts
            )
        canonical = validate_native_stream_part(envelope)
        return NativeStreamFrame(
            canonical=canonical,
            observations=(),
            replay=part,
            root_interrupt_ids=tuple(item.id for item in nested_interrupts)
            if not part.graph_namespace
            else (),
            origin=part.graph_origin,
            subagent_requests=part.subagent_requests,
        )
    except NativeStreamContractError:
        raise
    except (TypeError, ValueError) as error:
        raise NativeStreamContractError(
            "Native replay payload violates its declared mode", cause=error
        ) from error
