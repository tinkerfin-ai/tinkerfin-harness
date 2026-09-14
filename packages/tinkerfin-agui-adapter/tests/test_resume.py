"""Mapping contracts from AG-UI resume entries to Deep Agents decisions."""

from __future__ import annotations

import pytest
from ag_ui.core.types import Interrupt as AgUiInterrupt
from ag_ui.core.types import ResumeEntry
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import JsonValue

from tinkerfin_agui_adapter import AgUiAdapterErrorCode, ResumeTranslation
from tinkerfin_agui_adapter.ids import ScopedIdCodec
from tinkerfin_agui_adapter.models import AgentRuntimeInterrupt
from tinkerfin_agui_adapter.resume import (
    ResumeMapper,
    ResumeMappingError,
)


def _interrupts() -> tuple[AgentRuntimeInterrupt, ...]:
    return (
        AgentRuntimeInterrupt(
            id="interrupt-main",
            value={
                "action_requests": [
                    {
                        "name": "write_file",
                        "args": {"file_path": "a.txt", "content": "A"},
                    },
                    {
                        "name": "write_file",
                        "args": {"file_path": "b.txt", "content": "B"},
                    },
                ],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": [
                            "approve",
                            "edit",
                            "reject",
                            "respond",
                        ],
                    },
                    {
                        "action_name": "write_file",
                        "allowed_decisions": [
                            "approve",
                            "edit",
                            "reject",
                            "respond",
                        ],
                    },
                ],
            },
        ),
    )


def _main_checkpoint_message() -> AIMessage:
    return AIMessage(
        id="message-main-review",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "a.txt", "content": "A"},
                "id": "call-main-a",
                "type": "tool_call",
            },
            {
                "name": "write_file",
                "args": {"file_path": "b.txt", "content": "B"},
                "id": "call-main-b",
                "type": "tool_call",
            },
        ],
    )


def _entry(
    interrupt_id: str,
    *,
    status: str = "resolved",
    payload: object | None = None,
) -> ResumeEntry:
    return ResumeEntry.model_validate(
        {
            "interruptId": interrupt_id,
            "status": status,
            **({"payload": payload} if payload is not None else {}),
        }
    )


def _public_interrupts() -> tuple[AgUiInterrupt, ...]:
    native_value = _interrupts()[0].value
    codec = ScopedIdCodec()
    interrupts: list[AgUiInterrupt] = []
    for index, raw_tool_call_id in enumerate(("call-main-a", "call-main-b")):
        deepagents = {
            "schema": "tinkerfin.deepagents.tool-review",
            "nativeInterruptId": "interrupt-main",
            "actionIndex": index,
            "toolName": "write_file",
            "allowedDecisions": ["approve", "edit", "reject", "respond"],
            "originalArgs": {
                "file_path": f"{'a' if index == 0 else 'b'}.txt",
                "content": "A" if index == 0 else "B",
            },
        }
        interrupts.append(
            AgUiInterrupt(
                id=f"interrupt-main#{index}",
                reason="tool_call",
                tool_call_id=codec.encode("tool", (), raw_tool_call_id),
                metadata={
                    "langgraphValue": native_value,
                    "deepagents": deepagents,
                },
            )
        )
    return tuple(interrupts)


def test_resume_mapper_uses_persisted_agui_tool_ids_without_checkpoint() -> None:
    translation = ResumeMapper().map_agui(
        entries=(
            _entry("interrupt-main#1", payload={"type": "approve"}),
            _entry("interrupt-main#0", payload={"type": "reject"}),
        ),
        interrupts=_public_interrupts(),
    )

    assert translation.root == {"decisions": [{"type": "reject"}, {"type": "approve"}]}
    assert translation.prior_tool_call_ids == (
        ScopedIdCodec().encode("tool", (), "call-main-a"),
        ScopedIdCodec().encode("tool", (), "call-main-b"),
    )


def test_resume_mapper_revalidates_edited_args_against_persisted_schema() -> None:
    interrupt = AgentRuntimeInterrupt(
        id="schema-review",
        value={
            "action_requests": [
                {"name": "write_file", "args": {"file_path": "/workspace/a.txt"}}
            ],
            "review_configs": [
                {
                    "action_name": "write_file",
                    "allowed_decisions": ["edit"],
                    "args_schema": {
                        "type": "object",
                        "required": ["file_path"],
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "pattern": "^/workspace/",
                            }
                        },
                        "additionalProperties": False,
                    },
                }
            ],
        },
    )
    message = AIMessage(
        id="schema-message",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "/workspace/a.txt"},
                "id": "schema-call",
                "type": "tool_call",
            }
        ],
    )

    with pytest.raises(ResumeMappingError) as captured:
        ResumeMapper().map(
            entries=(
                _entry(
                    "schema-review",
                    payload={
                        "type": "edit",
                        "edited_action": {
                            "name": "write_file",
                            "args": {"file_path": "/etc/passwd"},
                        },
                    },
                ),
            ),
            interrupts=(interrupt,),
            messages_by_graph_namespace={(): (message,)},
        )

    assert captured.value.code is AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID


def test_resume_mapper_rejects_tampered_persisted_agui_correlation() -> None:
    interrupts = list(_public_interrupts())
    metadata = dict(interrupts[0].metadata or {})
    deepagents = dict(metadata["deepagents"])
    deepagents["originalArgs"] = {"file_path": "other.txt", "content": "A"}
    metadata["deepagents"] = deepagents
    interrupts[0] = interrupts[0].model_copy(update={"metadata": metadata})

    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map_agui(
            entries=(
                _entry("interrupt-main#0", payload={"type": "approve"}),
                _entry("interrupt-main#1", payload={"type": "approve"}),
            ),
            interrupts=interrupts,
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED


def test_resume_mapper_restores_multiple_persisted_agui_groups() -> None:
    secondary = AgUiInterrupt(
        id="interrupt-secondary",
        reason="tool_call",
        tool_call_id=ScopedIdCodec().encode("tool", ("tools:task-1",), "call-ask"),
        metadata={
            "langgraphValue": {
                "action_requests": [
                    {"name": "ask_user", "args": {"question": "Continue?"}}
                ],
                "review_configs": [
                    {
                        "action_name": "ask_user",
                        "allowed_decisions": ["respond"],
                    }
                ],
            },
            "deepagents": {
                "schema": "tinkerfin.deepagents.tool-review",
                "nativeInterruptId": "interrupt-secondary",
                "actionIndex": 0,
                "toolName": "ask_user",
                "allowedDecisions": ["respond"],
                "originalArgs": {"question": "Continue?"},
            },
        },
    )

    translation = ResumeMapper().map_agui(
        entries=(
            _entry(
                "interrupt-secondary",
                payload={"type": "respond", "message": "Yes"},
            ),
            _entry("interrupt-main#1", payload={"type": "approve"}),
            _entry("interrupt-main#0", payload={"type": "reject"}),
        ),
        interrupts=(*_public_interrupts(), secondary),
    )

    assert translation.root == {
        "interrupt-main": {"decisions": [{"type": "reject"}, {"type": "approve"}]},
        "interrupt-secondary": {"decisions": [{"type": "respond", "message": "Yes"}]},
    }
    assert translation.prior_tool_call_ids == (
        ScopedIdCodec().encode("tool", (), "call-main-a"),
        ScopedIdCodec().encode("tool", (), "call-main-b"),
        ScopedIdCodec().encode("tool", ("tools:task-1",), "call-ask"),
    )


def test_resume_mapper_preserves_all_persisted_tool_ids_for_mixed_resume() -> None:
    translation = ResumeMapper().map_agui(
        entries=(
            _entry("interrupt-main#1", status="cancelled"),
            _entry("interrupt-main#0", payload={"type": "approve"}),
        ),
        interrupts=_public_interrupts(),
    )

    assert translation.mode == "custom"
    assert translation.cancelled_interrupt_ids == ("interrupt-main#1",)
    assert translation.prior_tool_call_ids == (
        ScopedIdCodec().encode("tool", (), "call-main-a"),
        ScopedIdCodec().encode("tool", (), "call-main-b"),
    )


def test_resume_mapper_rejects_incomplete_persisted_agui_group() -> None:
    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map_agui(
            entries=(_entry("interrupt-main#0", payload={"type": "approve"}),),
            interrupts=_public_interrupts()[:1],
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_INCOMPLETE


def test_resume_mapper_restores_multi_action_order_and_replaces_edited_args() -> None:
    result = ResumeMapper().map(
        entries=(
            _entry(
                "interrupt-main#1",
                payload={
                    "type": "edit",
                    "edited_action": {
                        "name": "write_file",
                        "args": {"file_path": "b.txt", "content": "B2"},
                    },
                },
            ),
            _entry(
                "interrupt-main#0",
                payload={"type": "reject", "message": "Use another path"},
            ),
        ),
        interrupts=_interrupts(),
        messages_by_graph_namespace={(): (_main_checkpoint_message(),)},
    )

    assert result.root == {
        "decisions": [
            {"type": "reject", "message": "Use another path"},
            {
                "type": "edit",
                "edited_action": {
                    "name": "write_file",
                    "args": {"file_path": "b.txt", "content": "B2"},
                },
            },
        ]
    }


def test_resume_mapper_rejects_edit_that_changes_the_reviewed_tool() -> None:
    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(
            entries=(
                _entry(
                    "interrupt-main#0",
                    payload={
                        "type": "edit",
                        "edited_action": {
                            "name": "execute",
                            "args": {"command": "rm -rf /workspace"},
                        },
                    },
                ),
                _entry("interrupt-main#1", payload={"type": "approve"}),
            ),
            interrupts=_interrupts(),
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_resume_mapper_rejects_non_finite_edited_arguments(number: float) -> None:
    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(
            entries=(
                _entry(
                    "interrupt-main#0",
                    payload={
                        "type": "edit",
                        "edited_action": {
                            "name": "write_file",
                            "args": {"value": number},
                        },
                    },
                ),
                _entry("interrupt-main#1", payload={"type": "approve"}),
            ),
            interrupts=_interrupts(),
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID


def test_same_name_actions_use_their_positionally_paired_review_policy() -> None:
    interrupt = AgentRuntimeInterrupt(
        id="interrupt-positional",
        value={
            "action_requests": [
                {"name": "write_file", "args": {"file_path": "a.txt"}},
                {"name": "write_file", "args": {"file_path": "b.txt"}},
            ],
            "review_configs": [
                {"action_name": "write_file", "allowed_decisions": ["reject"]},
                {"action_name": "write_file", "allowed_decisions": ["approve"]},
            ],
        },
    )

    result = ResumeMapper().map(
        entries=(
            _entry("interrupt-positional#1", payload={"type": "approve"}),
            _entry("interrupt-positional#0", payload={"type": "reject"}),
        ),
        interrupts=(interrupt,),
        messages_by_graph_namespace={
            (): (
                AIMessage(
                    id="message-positional-review",
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "a.txt"},
                            "id": "call-positional-a",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {"file_path": "b.txt"},
                            "id": "call-positional-b",
                            "type": "tool_call",
                        },
                    ],
                ),
            )
        },
    )

    assert result.root == {"decisions": [{"type": "reject"}, {"type": "approve"}]}


def test_resume_mapper_rejects_unequal_hitl_action_and_config_lengths() -> None:
    interrupt = AgentRuntimeInterrupt(
        id="interrupt-unequal",
        value={
            "action_requests": [
                {"name": "write_file", "args": {"file_path": "a.txt"}},
                {"name": "write_file", "args": {"file_path": "b.txt"}},
            ],
            "review_configs": [
                {"action_name": "write_file", "allowed_decisions": ["approve"]}
            ],
        },
    )

    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(entries=(), interrupts=(interrupt,))

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED


@pytest.mark.parametrize(
    ("entries", "failure"),
    [
        ((), AgUiAdapterErrorCode.RESUME_INCOMPLETE),
        (
            (_entry("unknown", payload={"type": "approve"}),),
            AgUiAdapterErrorCode.RESUME_UNKNOWN_INTERRUPT_ID,
        ),
        (
            (
                _entry("interrupt-main#0", payload={"type": "approve"}),
                _entry("interrupt-main#0", payload={"type": "approve"}),
            ),
            AgUiAdapterErrorCode.RESUME_DUPLICATE_INTERRUPT_ID,
        ),
    ],
)
def test_resume_mapper_raises_stable_typed_failures(
    entries: tuple[ResumeEntry, ...],
    failure: AgUiAdapterErrorCode,
) -> None:
    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(entries=entries, interrupts=_interrupts())

    assert raised.value.code is failure


def test_resume_mapper_rejects_missing_payload() -> None:
    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(
            entries=(
                _entry("interrupt-main#0"),
                _entry("interrupt-main#1", payload={"type": "approve"}),
            ),
            interrupts=_interrupts(),
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_PAYLOAD_REQUIRED


def test_cancelled_resume_abandons_without_building_deep_agents_decisions() -> None:
    translation = ResumeMapper().map(
        entries=(
            _entry("interrupt-main#0", status="cancelled"),
            _entry("interrupt-main#1", status="cancelled"),
        ),
        interrupts=_interrupts(),
    )

    assert translation.mode == "abandon"
    assert translation.resume_data is None
    assert translation.cancelled_interrupt_ids == (
        "interrupt-main#0",
        "interrupt-main#1",
    )


def test_resume_mapper_supports_reject_feedback_and_required_respond_message() -> None:
    translation = ResumeMapper().map(
        entries=(
            _entry(
                "interrupt-main#0",
                payload={"type": "reject", "message": "Run tests first"},
            ),
            _entry(
                "interrupt-main#1",
                payload={"type": "respond", "message": "Use the staged file"},
            ),
        ),
        interrupts=_interrupts(),
        messages_by_graph_namespace={(): (_main_checkpoint_message(),)},
    )

    assert translation.root == {
        "decisions": [
            {"type": "reject", "message": "Run tests first"},
            {"type": "respond", "message": "Use the staged file"},
        ]
    }
    assert isinstance(translation, ResumeTranslation)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "respond"},
        {"type": "approve", "message": "unexpected"},
        {
            "type": "edit",
            "edited_action": {"name": "write_file"},
        },
    ],
)
def test_resume_mapper_rejects_decision_payloads_outside_public_schema(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(
            entries=(
                _entry("interrupt-main#0", payload=payload),
                _entry("interrupt-main#1", payload={"type": "approve"}),
            ),
            interrupts=_interrupts(),
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID


def test_resume_translation_carries_verified_prior_tool_call_ids() -> None:
    final_message = AIMessage(
        id="message-pending",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "a.txt", "content": "A"},
                "id": "call-a",
                "type": "tool_call",
            },
            {
                "name": "write_file",
                "args": {"file_path": "b.txt", "content": "B"},
                "id": "call-b",
                "type": "tool_call",
            },
        ],
    )

    translation = ResumeMapper().map(
        entries=(
            _entry("interrupt-main#0", payload={"type": "approve"}),
            _entry("interrupt-main#1", payload={"type": "approve"}),
        ),
        interrupts=_interrupts(),
        messages_by_graph_namespace={("execute_step:task-1",): (final_message,)},
    )

    codec = ScopedIdCodec()
    assert translation.prior_tool_call_ids == (
        codec.encode("tool", ("execute_step:task-1",), "call-a"),
        codec.encode("tool", ("execute_step:task-1",), "call-b"),
    )


@pytest.mark.parametrize("same_namespace", [False, True])
def test_completed_checkpoint_calls_are_excluded_only_in_their_graph(
    same_namespace: bool,
) -> None:
    pending = _main_checkpoint_message()
    previous = pending.model_copy(deep=True)
    previous.id = "previous-message"
    if same_namespace:
        for call in previous.tool_calls:
            identifier = call["id"]
            assert identifier is not None
            call["id"] = "previous-" + identifier
    completed = [
        ToolMessage(content="saved", tool_call_id=call["id"])
        for call in previous.tool_calls
    ]
    before = [message.model_dump() for message in (pending, previous, *completed)]
    namespace = ("task:current",)
    messages: dict[tuple[str, ...], tuple[BaseMessage, ...]] = (
        {namespace: (previous, *completed, pending)}
        if same_namespace
        else {("task:previous",): (previous, *completed), namespace: (pending,)}
    )
    translation = ResumeMapper().map(
        entries=(
            _entry("interrupt-main#0", payload={"type": "approve"}),
            _entry("interrupt-main#1", payload={"type": "approve"}),
        ),
        interrupts=_interrupts(),
        messages_by_graph_namespace=messages,
    )
    assert translation.prior_tool_call_ids == tuple(
        ScopedIdCodec().encode("tool", namespace, identifier)
        for identifier in ("call-main-a", "call-main-b")
    )
    assert [
        message.model_dump() for message in (pending, previous, *completed)
    ] == before


def test_resume_mapper_rejects_reused_tool_call_ids_within_one_group() -> None:
    message = AIMessage(
        id="message-duplicate-tool-ids",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "a.txt", "content": "A"},
                "id": "call-duplicate",
                "type": "tool_call",
            },
            {
                "name": "write_file",
                "args": {"file_path": "b.txt", "content": "B"},
                "id": "call-duplicate",
                "type": "tool_call",
            },
        ],
    )

    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(
            entries=(
                _entry("interrupt-main#0", payload={"type": "approve"}),
                _entry("interrupt-main#1", payload={"type": "approve"}),
            ),
            interrupts=_interrupts(),
            messages_by_graph_namespace={(): (message,)},
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED


def test_resolved_resume_requires_checkpoint_messages_for_tool_correlation() -> None:
    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(
            entries=(
                _entry("interrupt-main#0", payload={"type": "approve"}),
                _entry("interrupt-main#1", payload={"type": "approve"}),
            ),
            interrupts=_interrupts(),
            messages_by_graph_namespace=None,
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_CHECKPOINT_MESSAGES_REQUIRED


@pytest.mark.parametrize("action_value", [True, 1, 1.0])
def test_resume_hitl_correlation_matches_json_argument_types_exactly(
    action_value: bool | int | float,
) -> None:
    value: JsonValue = {
        "action_requests": [{"name": "write_file", "args": {"value": action_value}}],
        "review_configs": [
            {
                "action_name": "write_file",
                "allowed_decisions": ["approve"],
            }
        ],
    }
    interrupt = AgentRuntimeInterrupt(
        id="interrupt-json-types",
        value=value,
    )
    final_message = AIMessage(
        id="message-json-types",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"value": value},
                "id": call_id,
                "type": "tool_call",
            }
            for value, call_id in (
                (True, "call-bool"),
                (1, "call-int"),
                (1.0, "call-float"),
            )
        ],
    )

    translation = ResumeMapper().map(
        entries=(_entry("interrupt-json-types", payload={"type": "approve"}),),
        interrupts=(interrupt,),
        messages_by_graph_namespace={(): (final_message,)},
    )

    expected_id = {
        bool: "call-bool",
        int: "call-int",
        float: "call-float",
    }[type(action_value)]
    assert translation.prior_tool_call_ids == (
        ScopedIdCodec().encode("tool", (), expected_id),
    )


def test_resume_mapper_preserves_multiple_native_interrupt_groups() -> None:
    second = AgentRuntimeInterrupt(
        id="interrupt-secondary",
        value={
            "action_requests": [
                {"name": "ask_user", "args": {"question": "Continue?"}}
            ],
            "review_configs": [
                {
                    "action_name": "ask_user",
                    "allowed_decisions": ["respond"],
                }
            ],
        },
    )

    translation = ResumeMapper().map(
        entries=(
            _entry(
                "interrupt-secondary",
                payload={"type": "respond", "message": "Continue"},
            ),
            _entry("interrupt-main#1", payload={"type": "approve"}),
            _entry("interrupt-main#0", payload={"type": "reject"}),
        ),
        interrupts=(*_interrupts(), second),
        messages_by_graph_namespace={
            (): (
                _main_checkpoint_message(),
                AIMessage(
                    id="message-secondary-review",
                    content="",
                    tool_calls=[
                        {
                            "name": "ask_user",
                            "args": {"question": "Continue?"},
                            "id": "call-secondary",
                            "type": "tool_call",
                        }
                    ],
                ),
            )
        },
    )

    assert translation.root == {
        "interrupt-main": {"decisions": [{"type": "reject"}, {"type": "approve"}]},
        "interrupt-secondary": {
            "decisions": [{"type": "respond", "message": "Continue"}]
        },
    }


def test_resume_mapper_correlates_multiple_interrupt_groups_to_distinct_messages() -> (
    None
):
    write_interrupt = AgentRuntimeInterrupt(
        id="interrupt-write",
        value={
            "action_requests": [{"name": "write_file", "args": {"file_path": "a.txt"}}],
            "review_configs": [
                {
                    "action_name": "write_file",
                    "allowed_decisions": ["approve"],
                }
            ],
        },
    )
    ask_interrupt = AgentRuntimeInterrupt(
        id="interrupt-ask",
        value={
            "action_requests": [
                {"name": "ask_user", "args": {"question": "Continue?"}}
            ],
            "review_configs": [
                {
                    "action_name": "ask_user",
                    "allowed_decisions": ["respond"],
                }
            ],
        },
    )
    messages = (
        AIMessage(
            id="message-write",
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "a.txt"},
                    "id": "call-write",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            id="message-ask",
            content="",
            tool_calls=[
                {
                    "name": "ask_user",
                    "args": {"question": "Continue?"},
                    "id": "call-ask",
                    "type": "tool_call",
                }
            ],
        ),
    )

    translation = ResumeMapper().map(
        entries=(
            _entry("interrupt-ask", payload={"type": "respond", "message": "Yes"}),
            _entry("interrupt-write", payload={"type": "approve"}),
        ),
        interrupts=(write_interrupt, ask_interrupt),
        messages_by_graph_namespace={(): messages},
    )

    codec = ScopedIdCodec()
    assert translation.prior_tool_call_ids == (
        codec.encode("tool", (), "call-write"),
        codec.encode("tool", (), "call-ask"),
    )


def test_resume_mapper_rejects_an_ambiguous_cross_scope_tool_match() -> None:
    interrupt = AgentRuntimeInterrupt(
        id="interrupt-write",
        value={
            "action_requests": [{"name": "write_file", "args": {"file_path": "a.txt"}}],
            "review_configs": [
                {
                    "action_name": "write_file",
                    "allowed_decisions": ["approve"],
                }
            ],
        },
    )
    message = AIMessage(
        id="proposal",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "a.txt"},
                "id": "same-native-id",
                "type": "tool_call",
            }
        ],
    )

    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(
            entries=(_entry("interrupt-write", payload={"type": "approve"}),),
            interrupts=(interrupt,),
            messages_by_graph_namespace={
                ("first:task-1",): (message,),
                ("second:task-2",): (message,),
            },
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED


def test_resume_mapper_rejects_a_decision_forbidden_by_its_position() -> None:
    interrupt = AgentRuntimeInterrupt(
        id="interrupt-policy",
        value={
            "action_requests": [{"name": "write_file", "args": {}}],
            "review_configs": [
                {
                    "action_name": "write_file",
                    "allowed_decisions": ["reject"],
                }
            ],
        },
    )

    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(
            entries=(_entry("interrupt-policy", payload={"type": "approve"}),),
            interrupts=(interrupt,),
        )

    assert raised.value.code is AgUiAdapterErrorCode.RESUME_DECISION_NOT_ALLOWED
