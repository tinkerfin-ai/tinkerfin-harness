"""Mapping contracts from AG-UI resume entries to Deep Agents decisions."""

from __future__ import annotations

import pytest
from ag_ui.core.types import Interrupt as AgUiInterrupt
from ag_ui.core.types import ResumeEntry
from langchain_core.messages import AIMessage

from tinkerfin_agui_adapter import AgUiAdapterErrorCode
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
