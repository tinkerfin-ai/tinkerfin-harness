"""AG-UI event types and stateless readers used by protocol adapters."""

from __future__ import annotations

import json
from enum import Enum
from typing import cast

from ag_ui.core import (
    BaseEvent,
    Event,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from pydantic import JsonValue

AgentEvent = BaseEvent


def enum_value(value: object) -> str:
    """Project a protocol enum or ordinary value to text."""

    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def event_type(event: AgentEvent) -> str:
    """Return the AG-UI event type string."""

    return enum_value(event.type)


def root_interrupt_batch_id(interrupt_id: str) -> str:
    """Recover the native batch ID from a per-action interrupt ID."""

    return interrupt_id.split("#", 1)[0]


def json_text(value: object) -> str:
    """Return a stable text projection of protocol content."""

    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def json_object(value: JsonValue | None) -> dict[str, JsonValue]:
    """Return the value only when it is a JSON object."""

    return value if isinstance(value, dict) else {}


def json_array(value: JsonValue | None) -> list[JsonValue]:
    """Return the value only when it is a JSON array."""

    return value if isinstance(value, list) else []


def raw_event_dict(event: AgentEvent) -> dict[str, JsonValue]:
    """Return a validated AG-UI `rawEvent` object when present."""

    raw_event = event.raw_event
    return cast(dict[str, JsonValue], raw_event) if isinstance(raw_event, dict) else {}


def event_run_id(
    event: AgentEvent,
    *,
    default_run_id: str | None,
) -> str | None:
    """Resolve an event run ID from standard, extension, then fallback data."""

    run_id = getattr(event, "run_id", None)
    if isinstance(run_id, str) and run_id:
        return run_id
    raw_run_id = raw_event_dict(event).get("runId")
    if isinstance(raw_run_id, str) and raw_run_id:
        return raw_run_id
    if event_type(event) == "RUN_ERROR":
        return default_run_id
    return default_run_id if default_run_id else None


__all__ = [
    "AgentEvent",
    "Event",
    "RunErrorEvent",
    "RunFinishedEvent",
    "RunStartedEvent",
    "StateDeltaEvent",
    "StateSnapshotEvent",
    "TextMessageContentEvent",
    "TextMessageEndEvent",
    "TextMessageStartEvent",
    "ToolCallArgsEvent",
    "ToolCallEndEvent",
    "ToolCallResultEvent",
    "ToolCallStartEvent",
    "enum_value",
    "event_run_id",
    "event_type",
    "json_array",
    "json_object",
    "json_text",
    "raw_event_dict",
    "root_interrupt_batch_id",
]
