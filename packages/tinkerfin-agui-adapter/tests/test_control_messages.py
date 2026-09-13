"""Live state-control messages do not become assistant text or replace snapshots."""

from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from tinkerfin_agui_adapter import (
    AgUiStreamContractError,
    AttachmentMessagesSnapshotEvent,
    DeepAgentAgUiAdapter,
)
from tinkerfin_contracts import RunIdentity


def test_control_messages_wait_for_authoritative_values_without_assistant_output() -> (
    None
):
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="control", thread_id="thread", run_id="run")
    )
    system = SystemMessage(id="system", content="Instructions")
    old_user = HumanMessage(id="old-user", content="Old request")
    new_user = HumanMessage(id="new-user", content="New request")
    answer = AIMessage(id="answer", content="Visible answer")
    initial = adapter.process(
        {"type": "values", "ns": (), "data": {"messages": [system, old_user]}}
    )
    assert initial == []
    for message in (RemoveMessage(id="__remove_all__"), system, new_user):
        assert (
            adapter.process(
                {
                    "type": "messages",
                    "ns": (),
                    "data": (message, {"langgraph_node": "rewrite"}),
                }
            )
            == []
        )
    final = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [system, new_user, answer]},
            "interrupts": (
                {
                    "id": "input-required",
                    "value": {
                        "schema": "tinkerfin.runtime-interrupt",
                        "kind": "input_required",
                        "message": "Confirm the corrected request",
                        "response_schema": {"type": "string"},
                    },
                },
            ),
        }
    )
    snapshot = next(
        event for event in final if isinstance(event, AttachmentMessagesSnapshotEvent)
    )
    assert [(message.role, message.content) for message in snapshot.messages] == [
        ("system", "Instructions"),
        ("user", "New request"),
        ("assistant", "Visible answer"),
    ]
    assert len({message.id for message in snapshot.messages}) == 3
    assert all("__remove_all__" not in message.id for message in snapshot.messages)
    assert adapter.finish() == []


def test_serialized_control_message_is_not_accepted_as_a_live_part() -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="control", thread_id="thread", run_id="run")
    )
    with pytest.raises(AgUiStreamContractError, match="live LangChain BaseMessage"):
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (RemoveMessage(id="__remove_all__").model_dump(), {}),
            }
        )


@pytest.mark.parametrize("late_values", [False, True])
def test_new_tool_proposal_cannot_reuse_a_completed_tool_id(
    late_values: bool,
) -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="control", thread_id="thread", run_id="run")
    )

    def proposal(parent: str) -> AIMessage:
        return AIMessage(
            id=parent,
            content="",
            tool_calls=[{"id": "save", "name": "save_report", "args": {}}],
        )

    def emit(message: AIMessage | ToolMessage):
        return adapter.process({"type": "messages", "ns": (), "data": (message, {})})

    if late_values:
        emit(
            AIMessageChunk(
                id="old",
                content="",
                tool_call_chunks=[
                    {"id": "save", "name": "save_report", "args": "{}", "index": 0}
                ],
            )
        )
    adapter.process(
        {"type": "values", "ns": (), "data": {"messages": [proposal("old")]}}
    )
    emit(ToolMessage(id="old-result", content="Saved", tool_call_id="save"))
    expected = (
        "ended tool-call ID cannot start again"
        if late_values
        else "new Tool proposals require unique IDs"
    )
    with pytest.raises(ValueError, match=expected):
        emit(proposal("another"))
