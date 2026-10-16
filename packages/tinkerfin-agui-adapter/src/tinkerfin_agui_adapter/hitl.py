"""Locked Deep Agents HITL boundary models used by the AG-UI adapter."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import Field, JsonValue, ValidationError, model_validator

from ._json_schema import SchemaError, require_valid_schema
from .errors import HitlCorrelationError, HitlNoMatchError
from .models import JsonObject, RuntimeModel
from .reasoning import json_values_equal


class HitlActionRequest(RuntimeModel):
    """One ordered Deep Agents action awaiting human review."""

    name: str = Field(min_length=1, description="Name of the action under review")
    args: JsonObject = Field(description="Action argument object")
    description: str | None = Field(
        default=None,
        description="Human-readable action description",
    )


class HitlReviewConfig(RuntimeModel):
    """Decision policy positionally paired with one HITL action."""

    action_name: str = Field(
        min_length=1,
        description="Action name paired with this review policy",
    )
    allowed_decisions: list[Literal["approve", "edit", "reject", "respond"]] = Field(
        min_length=1,
        description="Decisions allowed for this action",
    )
    args_schema: JsonObject | None = Field(
        default=None,
        description="JSON Schema for the action arguments",
    )


class HitlRequest(RuntimeModel):
    """Stable projection of the Deep Agents HITL request shape."""

    action_requests: list[HitlActionRequest] = Field(
        min_length=1,
        description="Ordered actions awaiting review",
    )
    review_configs: list[HitlReviewConfig] = Field(
        min_length=1,
        description="Review policies positionally paired with the actions",
    )

    @model_validator(mode="after")
    def validate_positional_pairs(self) -> HitlRequest:
        """Require action and review entries to pair in native positional order."""

        if len(self.action_requests) != len(self.review_configs):
            raise ValueError(
                "action_requests and review_configs must have equal lengths"
            )
        for index, (action, review) in enumerate(
            zip(self.action_requests, self.review_configs, strict=True)
        ):
            if action.name != review.action_name:
                raise ValueError(f"action and review names differ at index {index}")
            if len(set(review.allowed_decisions)) != len(review.allowed_decisions):
                raise ValueError(
                    f"allowed_decisions contains duplicates at index {index}"
                )
            if review.args_schema is not None:
                try:
                    require_valid_schema(review.args_schema.root)
                except SchemaError as error:
                    raise ValueError(
                        f"args_schema is invalid at index {index}"
                    ) from error
        return self


@dataclass(frozen=True, slots=True)
class HitlToolCallCandidate:
    """One position-preserving Tool call considered for HITL correlation."""

    graph_namespace: tuple[str, ...]
    tool_call_id: str
    tool_name: str
    parent_message_id: str
    position: int
    arguments: JsonValue | None
    arguments_error: json.JSONDecodeError | None = None


_MAX_MULTI_GROUP_SEARCH_STATES = 100_000


def _ordered_candidate_matches(
    actions: Sequence[HitlActionRequest],
    candidates: Sequence[HitlToolCallCandidate],
    *,
    on_search_step: Callable[[], None] | None = None,
) -> Iterator[tuple[str, ...]]:
    """Yield exact ordered subsequence matches without materializing combinations."""

    def search(
        action_index: int,
        candidate_index: int,
        selected: list[str],
    ) -> Iterator[tuple[str, ...]]:
        if on_search_step is not None:
            on_search_step()
        if action_index == len(actions):
            yield tuple(selected)
            return
        action = actions[action_index]
        for index in range(candidate_index, len(candidates)):
            if on_search_step is not None:
                on_search_step()
            candidate = candidates[index]
            if (
                candidate.arguments_error is None
                and candidate.tool_name == action.name
                and json_values_equal(candidate.arguments, action.args.root)
            ):
                selected.append(candidate.tool_call_id)
                yield from search(action_index + 1, index + 1, selected)
                selected.pop()

    return search(0, 0, [])


def _ordered_projected_matches(
    actions: Sequence[HitlActionRequest],
    candidates: Sequence[HitlToolCallCandidate],
    selected_slots: Sequence[bool],
) -> tuple[tuple[str, ...], ...]:
    """Return up to two distinct selected-ID projections for one action group."""

    @cache
    def search(
        action_index: int,
        candidate_index: int,
    ) -> tuple[tuple[str, ...], ...]:
        if action_index == len(actions):
            return ((),)
        solutions: list[tuple[str, ...]] = []
        action = actions[action_index]
        for index in range(candidate_index, len(candidates)):
            candidate = candidates[index]
            if (
                candidate.arguments_error is not None
                or candidate.tool_name != action.name
                or not json_values_equal(candidate.arguments, action.args.root)
            ):
                continue
            for suffix in search(action_index + 1, index + 1):
                solution = (
                    (candidate.tool_call_id, *suffix)
                    if selected_slots[action_index]
                    else suffix
                )
                if solution in solutions:
                    continue
                solutions.append(solution)
                if len(solutions) > 1:
                    return tuple(solutions)
        return tuple(solutions)

    return search(0, 0)


def match_hitl_action_groups(
    action_groups: Sequence[Sequence[HitlActionRequest]],
    candidate_messages: Sequence[Sequence[HitlToolCallCandidate]],
    *,
    selected_slots: Sequence[Sequence[bool]] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Assign every native HITL group to one unique candidate message.

    Each group's actions must form an exact ordered subsequence of one message's
    Tool calls. Groups may select disjoint calls from the same message, but a
    Tool-call ID cannot be reused. By default the full assignment must be unique.
    When `selected_slots` is provided, unselected actions still constrain matching,
    but uniqueness is evaluated after projecting each solution to selected slots.

    Args:
        action_groups: Native interrupt action groups in checkpoint order.
        candidate_messages: Candidate Tool calls grouped by their parent AI message.
        selected_slots: Optional per-action mask retained in the public result.

    Returns:
        One ordered tuple of scoped Tool-call IDs per native action group.

    Raises:
        HitlCorrelationError: No complete assignment exists or multiple assignments
            are possible.
    """

    if not action_groups or any(not group for group in action_groups):
        raise HitlCorrelationError("Deep Agents HITL action groups must be non-empty")
    if not candidate_messages:
        raise HitlNoMatchError(
            "checkpoint does not contain an AIMessage with stable tool calls"
        )
    projected_slots: tuple[tuple[bool, ...], ...] | None = None
    if selected_slots is not None:
        try:
            projected_slots = tuple(
                tuple(slots)
                for _actions, slots in zip(
                    action_groups,
                    selected_slots,
                    strict=True,
                )
            )
        except ValueError as error:
            raise HitlCorrelationError(
                "selected Tool-call slots must match native HITL groups"
            ) from error
        if any(
            len(actions) != len(slots)
            for actions, slots in zip(
                action_groups,
                projected_slots,
                strict=True,
            )
        ):
            raise HitlCorrelationError(
                "selected Tool-call slots must match native HITL actions"
            )

    solutions: list[tuple[tuple[str, ...], ...]] = []

    if len(action_groups) == 1:
        slots = (
            projected_slots[0]
            if projected_slots is not None
            else tuple(True for _action in action_groups[0])
        )
        for candidates in candidate_messages:
            for match in _ordered_projected_matches(
                action_groups[0],
                candidates,
                slots,
            ):
                solution = (match,)
                if solution in solutions:
                    continue
                solutions.append(solution)
                if len(solutions) > 1:
                    break
            if len(solutions) > 1:
                break
    else:
        search_states = 0

        def count_search_step() -> None:
            nonlocal search_states
            search_states += 1
            if search_states > _MAX_MULTI_GROUP_SEARCH_STATES:
                raise HitlCorrelationError(
                    "Deep Agents HITL correlation exceeded its safe search budget"
                )

        def assign(
            group_index: int,
            used_call_ids: set[str],
            selected: list[tuple[str, ...]],
        ) -> None:
            nonlocal search_states
            if len(solutions) > 1:
                return
            if group_index == len(action_groups):
                solution = tuple(selected)
                if projected_slots is not None:
                    solution = tuple(
                        tuple(
                            call_id
                            for call_id, include in zip(
                                call_ids,
                                slots,
                                strict=True,
                            )
                            if include
                        )
                        for call_ids, slots in zip(
                            solution,
                            projected_slots,
                            strict=True,
                        )
                    )
                if solution not in solutions:
                    solutions.append(solution)
                return
            actions = action_groups[group_index]
            for candidates in candidate_messages:
                for call_ids in _ordered_candidate_matches(
                    actions,
                    candidates,
                    on_search_step=count_search_step,
                ):
                    if len(set(call_ids)) != len(call_ids) or any(
                        call_id in used_call_ids for call_id in call_ids
                    ):
                        continue
                    used_call_ids.update(call_ids)
                    selected.append(call_ids)
                    assign(group_index + 1, used_call_ids, selected)
                    selected.pop()
                    used_call_ids.difference_update(call_ids)
                    if len(solutions) > 1:
                        return

        assign(0, set(), [])
    if len(solutions) > 1:
        raise HitlCorrelationError(
            "Deep Agents HITL tool-call correlation is ambiguous"
        )
    if not solutions:
        raise HitlNoMatchError(
            "Deep Agents HITL actions cannot be correlated to checkpoint tool calls"
        )
    return solutions[0]


def relevant_malformed_hitl_candidate(
    action_groups: Sequence[Sequence[HitlActionRequest]],
    candidate_messages: Sequence[Sequence[HitlToolCallCandidate]],
) -> HitlToolCallCandidate | None:
    """Return malformed arguments that can participate in a complete assignment.

    A malformed candidate is a wildcard only for arguments. Tool name, parent-message
    grouping, position, namespace-scoped ID uniqueness, and action order remain hard
    constraints. This detects whether ignoring invalid JSON could change correlation;
    it does not choose a Tool call for execution.

    Args:
        action_groups: Native interrupt actions whose complete assignment is required.
        candidate_messages: Valid and malformed Tool candidates grouped by message.

    Returns:
        First malformed candidate relevant to a valid assignment, or ``None``.

    Raises:
        HitlCorrelationError: The relevance search exceeds its bounded state budget.
    """

    if not action_groups or not candidate_messages:
        return None
    search_states = 0

    def count_search_step() -> None:
        nonlocal search_states
        search_states += 1
        if search_states > _MAX_MULTI_GROUP_SEARCH_STATES:
            raise HitlCorrelationError(
                "Deep Agents HITL malformed-candidate relevance exceeded its "
                "safe search budget"
            )

    def ordered_matches(
        actions: Sequence[HitlActionRequest],
        candidates: Sequence[HitlToolCallCandidate],
    ) -> Iterator[tuple[HitlToolCallCandidate, ...]]:
        def search(
            action_index: int,
            candidate_index: int,
            selected: list[HitlToolCallCandidate],
        ) -> Iterator[tuple[HitlToolCallCandidate, ...]]:
            count_search_step()
            if action_index == len(actions):
                yield tuple(selected)
                return
            action = actions[action_index]
            for index in range(candidate_index, len(candidates)):
                count_search_step()
                candidate = candidates[index]
                if candidate.tool_name != action.name:
                    continue
                if candidate.arguments_error is None and not json_values_equal(
                    candidate.arguments,
                    action.args.root,
                ):
                    continue
                selected.append(candidate)
                yield from search(action_index + 1, index + 1, selected)
                selected.pop()

        return search(0, 0, [])

    def assign(
        group_index: int,
        used_call_ids: set[str],
        first_malformed: HitlToolCallCandidate | None,
    ) -> HitlToolCallCandidate | None:
        count_search_step()
        if group_index == len(action_groups):
            return first_malformed
        actions = action_groups[group_index]
        for candidates in candidate_messages:
            for selected in ordered_matches(actions, candidates):
                call_ids = tuple(candidate.tool_call_id for candidate in selected)
                if len(set(call_ids)) != len(call_ids) or any(
                    call_id in used_call_ids for call_id in call_ids
                ):
                    continue
                malformed = first_malformed or next(
                    (
                        candidate
                        for candidate in selected
                        if candidate.arguments_error is not None
                    ),
                    None,
                )
                used_call_ids.update(call_ids)
                result = assign(group_index + 1, used_call_ids, malformed)
                used_call_ids.difference_update(call_ids)
                if result is not None:
                    return result
        return None

    return assign(0, set(), None)


def _checkpoint_tool_call_candidates(
    messages: Sequence[BaseMessage],
) -> tuple[tuple[HitlToolCallCandidate, ...], ...]:
    completed_call_ids = {
        str(message.tool_call_id)
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id
    }
    candidate_messages: list[tuple[HitlToolCallCandidate, ...]] = []
    seen_call_ids: set[str] = set()
    for message_index, message in enumerate(messages):
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        candidates: list[HitlToolCallCandidate] = []
        parent_message_id = str(message.id or f"checkpoint-message-{message_index}")
        for position, call in enumerate(message.tool_calls):
            call_id = call.get("id")
            name = call.get("name")
            if not call_id or not isinstance(name, str) or not name:
                raise HitlCorrelationError(
                    "checkpoint tool calls require stable IDs and names"
                )
            normalized_call_id = str(call_id)
            if normalized_call_id in seen_call_ids:
                raise HitlCorrelationError(
                    "checkpoint tool-call IDs must be unique within a graph scope"
                )
            seen_call_ids.add(normalized_call_id)
            # HITL proposals have no ToolMessage yet. Excluding completed historical
            # calls preserves parallel pending messages without letting an earlier
            # identical Turn make the current review appear ambiguous.
            if normalized_call_id in completed_call_ids:
                continue
            try:
                args = JsonObject.model_validate(call.get("args"))
            except ValidationError as error:
                raise HitlCorrelationError(
                    "checkpoint tool calls require JSON object arguments"
                ) from error
            candidates.append(
                HitlToolCallCandidate(
                    graph_namespace=(),
                    tool_call_id=normalized_call_id,
                    tool_name=name,
                    parent_message_id=parent_message_id,
                    position=position,
                    arguments=args.root,
                )
            )
        if candidates:
            candidate_messages.append(tuple(candidates))
    return tuple(candidate_messages)


def match_hitl_tool_call_id_groups(
    action_groups: Sequence[Sequence[HitlActionRequest]],
    messages: Sequence[BaseMessage],
    *,
    selected_slots: Sequence[Sequence[bool]] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Recover a unique checkpoint Tool-call assignment for native groups."""

    return match_hitl_action_groups(
        action_groups,
        _checkpoint_tool_call_candidates(messages),
        selected_slots=selected_slots,
    )


def match_hitl_tool_call_ids(
    actions: Sequence[HitlActionRequest],
    messages: Sequence[BaseMessage],
) -> tuple[str, ...]:
    """Recover the unique ordered Tool-call IDs reviewed by a HITL request.

    Matching considers every checkpoint AI message and requires one unique
    `name + args` ordered subsequence. It does not perform checkpoint I/O, use
    stream arrival order, or mutate Tool arguments.

    Args:
        actions: One native HITL action group in checkpoint order.
        messages: Trusted checkpoint messages containing candidate Tool calls.

    Returns:
        Unique scoped Tool-call IDs in native action order.

    Raises:
        HitlCorrelationError: The checkpoint lacks stable Tool calls or more than
            one message/subsequence matches the reviewed actions.
    """

    return match_hitl_tool_call_id_groups((actions,), messages)[0]
