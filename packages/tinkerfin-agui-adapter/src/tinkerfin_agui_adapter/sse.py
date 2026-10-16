"""Framework-independent Server-Sent Events encoding for AG-UI events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeAlias

from ag_ui.core import BaseEvent
from pydantic_core import to_json

from .reasoning import normalize_operational_data

SseEventId: TypeAlias = str | int
SseEventObserver: TypeAlias = Callable[
    [BaseEvent],
    Awaitable[SseEventId | None],
]

_FORBIDDEN_ID_CHARACTERS = frozenset({"\r", "\n", "\0"})


def encode_sse(
    event: BaseEvent,
    *,
    event_id: SseEventId | None = None,
) -> str:
    """Encode one AG-UI event as an SSE frame with an optional durable ID.

    This pure encoder does not open a connection, buffer an event stream, create
    an HTTP route, or own server/transport state. The caller controls event order,
    delivery, retry policy, and connection cancellation.

    Args:
        event: Validated AG-UI event to encode.
        event_id: Durable host-assigned sequence or event identifier. Strings may
            not contain CR, LF, or NUL because those characters alter SSE framing.

    Returns:
        One complete SSE frame ending in a blank line.

    Raises:
        TypeError: `event_id` is not a string, integer, or `None`.
        ValueError: A string `event_id` contains CR, LF, or NUL, or the event
            contains a non-finite number that JSON cannot represent.
    """

    encoded_id = _encode_event_id(event_id)
    payload = normalize_operational_data(
        event.model_dump(mode="python", by_alias=True, exclude_none=True)
    )
    data_frame = f"data: {to_json(payload).decode('utf-8')}\n\n"
    if encoded_id is None:
        return data_frame
    return f"id: {encoded_id}\n{data_frame}"


def _encode_event_id(event_id: SseEventId | None) -> str | None:
    if event_id is None:
        return None
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
        raise TypeError("event_id must be a string, integer, or None")
    if isinstance(event_id, str) and any(
        character in event_id for character in _FORBIDDEN_ID_CHARACTERS
    ):
        raise ValueError("event_id must not contain CR, LF, or NUL")
    return str(event_id)
