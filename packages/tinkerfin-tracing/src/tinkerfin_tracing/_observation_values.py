"""Pure value normalization and exact Tool-review correlation for observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

from tinkerfin_contracts import (
    NativeMessageRecord,
    NativeToolCall,
)

from .errors import TraceCorruption
from .facts import (
    TraceFactBase,
    TraceSemanticFact,
)


def fingerprint(value: JsonValue) -> str:
    """Hash a canonical JSON value for semantic deduplication."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def message_role(message: NativeMessageRecord) -> str:
    """Map a Native message type to its retained conversation role."""

    return {
        "human": "user",
        "assistant": "assistant",
        "assistant_chunk": "assistant",
        "tool": "tool",
        "system": "system",
    }.get(message.message_type, "other")


def make_fact(
    fact_type: type[TraceFactBase],
    common: Mapping[str, object],
    **values: object,
) -> TraceSemanticFact:
    """Validate a fact without weakening constructor types through kwargs maps."""

    return cast(
        TraceSemanticFact,
        fact_type.model_validate({**common, **values}),
    )


def extract_user_message(value: JsonValue) -> tuple[str, JsonValue] | None:
    """Return the final user message from the authoritative top-level channel."""

    if not isinstance(value, dict):
        return None
    raw_messages = value.get("messages")
    if isinstance(raw_messages, dict) and raw_messages.get("$type") == "tuple":
        raw_messages = raw_messages.get("items")
    if not isinstance(raw_messages, list):
        return None
    candidates: list[tuple[str, JsonValue]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        if item.get("$type") == "langchain.message":
            wrapped = item.get("value")
            if isinstance(wrapped, dict) and wrapped.get("type") in {"human", "user"}:
                data = wrapped.get("data")
                message_id = data.get("id") if isinstance(data, dict) else None
                content = data.get("content") if isinstance(data, dict) else None
                if isinstance(message_id, str) and message_id and content is not None:
                    candidates.append((message_id, cast(JsonValue, content)))
            continue
        if item.get("role") == "user":
            message_id = item.get("id")
            content = item.get("content")
            if isinstance(message_id, str) and message_id and content is not None:
                candidates.append((message_id, content))
    return candidates[-1] if candidates else None


def parsed_json(value: str) -> JsonValue | None:
    """Parse finite JSON fragments, leaving incomplete input unresolved."""

    try:
        return cast(
            JsonValue,
            json.loads(
                value,
                parse_constant=lambda _value: _raise_invalid_json_constant(),
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def append_json_content(current: JsonValue, delta: JsonValue) -> JsonValue:
    """Append matching text or list fragments without coercing values."""

    if isinstance(current, str) and isinstance(delta, str):
        return current + delta
    if isinstance(current, list) and isinstance(delta, list):
        return [*current, *delta]
    return delta


def _raise_invalid_json_constant() -> None:
    raise ValueError("non-finite JSON constants are not supported")


def discard_private_state(
    value: JsonValue,
    private_keys: frozenset[str],
) -> JsonValue:
    """Remove only Runtime-declared top-level state channels."""

    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key not in private_keys}


def plan_metadata(value: JsonValue) -> tuple[int | None, str | None]:
    """Read a valid plan revision and status from captured state."""

    if not isinstance(value, dict):
        return None, None
    raw_revision = value.get("revision")
    revision = (
        raw_revision
        if isinstance(raw_revision, int)
        and not isinstance(raw_revision, bool)
        and raw_revision >= 0
        else None
    )
    raw_status = value.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status else None
    return revision, status


def classify_interaction(value: JsonValue) -> str:
    """Identify Tool approval or application input from interrupt data."""

    if isinstance(value, dict):
        kind = value.get("kind")
        if isinstance(kind, str) and kind:
            return kind
        if "action_requests" in value:
            return "tool_approval"
    return "input_required"


def interaction_tool_call_ids(
    value: JsonValue,
    messages: tuple[NativeMessageRecord, ...],
) -> tuple[str, ...]:
    """Correlate reviewed actions to one unique ordered Tool-call subsequence.

    Deep Agents publishes only policy-selected actions in an interrupt, while the
    authoritative message can also contain unreviewed Tool calls. Correlation therefore
    matches exact ``name + args`` in model order, never arrival order or Tool name alone.
    The dynamic-programming count is capped at two because the only meaningful outcomes
    are missing, unique, and ambiguous.

    Args:
        value: Native interrupt value from the root or subgraph state snapshot.
        messages: Complete Native message records from the same state snapshot.

    Returns:
        Raw Tool call IDs in the same position order as ``action_requests``. Non-Tool
        runtime interrupts return an empty tuple.

    Raises:
        TraceCorruption: A Tool review is malformed, too large to correlate safely,
            missing its checkpoint Tool calls, or ambiguous.
    """

    if not isinstance(value, dict) or "action_requests" not in value:
        return ()
    raw_actions = value.get("action_requests")
    raw_reviews = value.get("review_configs")
    if (
        not isinstance(raw_actions, list)
        or not raw_actions
        or not isinstance(raw_reviews, list)
        or len(raw_reviews) != len(raw_actions)
    ):
        raise TraceCorruption(
            "Tool review action and review-config lists must be non-empty and aligned"
        )
    actions: list[tuple[str, dict[str, JsonValue]]] = []
    for action in raw_actions:
        if not isinstance(action, dict):
            raise TraceCorruption("Tool review actions must be objects")
        name = action.get("name")
        arguments = action.get("args")
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise TraceCorruption("Tool review actions require a name and object args")
        actions.append((name, arguments))

    completed_call_ids = {
        message.tool_call_id
        for message in messages
        if message.message_type == "tool" and message.tool_call_id is not None
    }
    unique: tuple[str, ...] | None = None
    work = 0
    for message in messages:
        # A completed historical call cannot be the proposal that produced the
        # currently pending interrupt. Keep unresolved messages available so
        # parallel review groups and subgraph scopes retain their exact IDs.
        calls = tuple(
            call for call in message.tool_calls if call.id not in completed_call_ids
        )
        if not calls:
            continue
        work += len(actions) * len(calls)
        if work > 100_000:
            raise TraceCorruption(
                "Tool review correlation exceeded its safe work budget"
            )
        count, candidate = _ordered_tool_call_match(tuple(actions), calls)
        if count == 0:
            continue
        if count > 1 or unique is not None:
            raise TraceCorruption(
                "Tool review actions match multiple Tool call sequences"
            )
        unique = candidate
    if unique is None:
        raise TraceCorruption("Tool review actions do not match checkpoint Tool calls")
    return unique


def _ordered_tool_call_match(
    actions: tuple[tuple[str, dict[str, JsonValue]], ...],
    calls: tuple[NativeToolCall, ...],
) -> tuple[int, tuple[str, ...] | None]:
    """Count up to two ordered action matches and reconstruct the unique path."""

    normalized_calls = [(call.id, call.name, call.arguments) for call in calls]

    action_count = len(actions)
    call_count = len(normalized_calls)
    counts = [[0] * (call_count + 1) for _ in range(action_count + 1)]
    for call_index in range(call_count + 1):
        counts[action_count][call_index] = 1
    for action_index in range(action_count - 1, -1, -1):
        action_name, action_args = actions[action_index]
        for call_index in range(call_count - 1, -1, -1):
            total = counts[action_index][call_index + 1]
            _call_id, call_name, call_args = normalized_calls[call_index]
            if call_name == action_name and call_args == action_args:
                total += counts[action_index + 1][call_index + 1]
            counts[action_index][call_index] = min(2, total)
    match_count = counts[0][0]
    if match_count != 1:
        return match_count, None

    selected: list[str] = []
    action_index = 0
    call_index = 0
    while action_index < action_count:
        if call_index >= call_count:
            raise TraceCorruption("Unique Tool review correlation became incomplete")
        action_name, action_args = actions[action_index]
        call_id, call_name, call_args = normalized_calls[call_index]
        take = (
            call_name == action_name
            and call_args == action_args
            and counts[action_index + 1][call_index + 1] > 0
        )
        skip = counts[action_index][call_index + 1] > 0
        if take and skip:
            raise TraceCorruption("Unique Tool review correlation became ambiguous")
        if take:
            selected.append(call_id)
            action_index += 1
        elif not skip:
            raise TraceCorruption("Unique Tool review correlation became incomplete")
        call_index += 1
    return 1, tuple(selected)
