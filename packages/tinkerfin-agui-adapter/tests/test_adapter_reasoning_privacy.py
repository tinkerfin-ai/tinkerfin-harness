from __future__ import annotations

import json

import pytest
from ag_ui.core import (
    AssistantMessage,
    MessagesSnapshotEvent,
    ReasoningMessageContentEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    StateSnapshotEvent,
)
from langchain_core.messages import AIMessage, AIMessageChunk

from tinkerfin_agui_adapter import RunIdentity, astream_events


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")


@pytest.mark.parametrize("expose_reasoning_events", [False, True])
@pytest.mark.asyncio
async def test_provider_reasoning_is_private_but_business_fields_survive(
    expose_reasoning_events: bool,
) -> None:
    state_secret = "STATE-SECRET"
    interrupt_secret = "INTERRUPT-SECRET"
    state_shape_secret = "STATE-SHAPE-SECRET"
    interrupt_shape_secret = "INTERRUPT-SHAPE-SECRET"

    async def parts():
        yield {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="ai-1",
                    content="",
                    additional_kwargs={"reasoning_content": "VISIBLE-EVENT"},
                    chunk_position="last",
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
        yield {
            "type": "values",
            "ns": (),
            "data": {
                "provider": {
                    "additional_kwargs": {
                        "reasoning_content": state_secret,
                        "keep": "state-sibling",
                    }
                },
                "reasoning_content": "STATE-BUSINESS",
                "domain": {
                    "reasoning_content": "NESTED-BUSINESS",
                    "type": "reasoning",
                },
                "originalArgs": {
                    "additional_kwargs": {
                        "reasoning_content": state_shape_secret,
                        "keep": "state-original-args-sibling",
                    }
                },
                "action_requests": [
                    {
                        "name": "business-record",
                        "args": {
                            "additional_kwargs": {
                                "reasoning_content": state_shape_secret,
                                "keep": "state-action-sibling",
                            }
                        },
                    }
                ],
            },
            "interrupts": (
                {
                    "id": "interrupt-1",
                    "value": {
                        "provider": {
                            "additional_kwargs": {
                                "reasoning_content": interrupt_secret,
                                "keep": "interrupt-sibling",
                            }
                        },
                        "reasoning_content": "INTERRUPT-BUSINESS",
                        "type": "thinking",
                        "originalArgs": {
                            "additional_kwargs": {
                                "reasoning_content": interrupt_shape_secret,
                                "keep": "interrupt-original-args-sibling",
                            }
                        },
                    },
                },
            ),
        }

    events = [
        event
        async for event in astream_events(
            parts=parts(),
            identity=_identity(),
            expose_reasoning_events=expose_reasoning_events,
        )
    ]
    serialized = "\n".join(
        event.model_dump_json(by_alias=True, exclude_none=True) for event in events
    )

    assert state_secret not in serialized
    assert interrupt_secret not in serialized
    assert state_shape_secret not in serialized
    assert interrupt_shape_secret not in serialized
    assert "state-sibling" in serialized
    assert "interrupt-sibling" in serialized
    for visible in (
        "STATE-BUSINESS",
        "NESTED-BUSINESS",
        "INTERRUPT-BUSINESS",
        "state-original-args-sibling",
        "state-action-sibling",
        "interrupt-original-args-sibling",
    ):
        assert visible in serialized

    state = next(event for event in events if isinstance(event, StateSnapshotEvent))
    assert state.snapshot["provider"] == {
        "additional_kwargs": {"keep": "state-sibling"}
    }
    terminal = next(event for event in events if isinstance(event, RunFinishedEvent))
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    metadata = terminal.outcome.interrupts[0].metadata
    assert metadata is not None
    langgraph_value = metadata["langgraphValue"]
    assert langgraph_value["provider"] == {
        "additional_kwargs": {"keep": "interrupt-sibling"}
    }

    event_types = [event.type.value for event in events]
    assert ("REASONING_MESSAGE_CONTENT" in event_types) is expose_reasoning_events
    if expose_reasoning_events:
        content = next(
            event for event in events if isinstance(event, ReasoningMessageContentEvent)
        )
        assert content.delta == "VISIBLE-EVENT"


@pytest.mark.asyncio
async def test_public_state_rejects_opaque_objects_before_event_serialization() -> None:
    class Opaque:
        pass

    async def parts():
        yield {
            "type": "values",
            "ns": (),
            "data": {"opaque": Opaque()},
            "interrupts": (),
        }

    events = [
        event async for event in astream_events(parts=parts(), identity=_identity())
    ]
    terminal = json.loads(events[-1].model_dump_json(by_alias=True))

    assert terminal["type"] == "RUN_ERROR"
    assert len(events) == 2


@pytest.mark.parametrize("expose_reasoning_events", [False, True])
@pytest.mark.asyncio
async def test_message_snapshot_filters_provider_metadata_but_preserves_siblings(
    expose_reasoning_events: bool,
) -> None:
    secret = "MESSAGE-SECRET"
    tool_args = {
        "additional_kwargs": {"reasoning_content": "TOOL-BUSINESS", "keep": True},
        "reasoning_content": "TOOL-TOP-LEVEL",
    }

    async def parts():
        yield {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    AIMessage(
                        id="ai-snapshot",
                        content="Visible answer",
                        additional_kwargs={
                            "reasoning_content": secret,
                            "keep": "message-sibling",
                        },
                        response_metadata={"provider": "visible-metadata"},
                        tool_calls=[
                            {
                                "name": "business_tool",
                                "args": tool_args,
                                "id": "call-snapshot",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            "interrupts": ({"id": "pause", "value": {"pause": True}},),
        }

    events = [
        event
        async for event in astream_events(
            parts=parts(),
            identity=_identity(),
            expose_reasoning_events=expose_reasoning_events,
        )
    ]
    snapshot = next(
        event for event in events if isinstance(event, MessagesSnapshotEvent)
    )
    assistant = snapshot.messages[0]
    assert isinstance(assistant, AssistantMessage)
    payload = assistant.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert secret not in json.dumps(payload)
    assert payload["content"] == "Visible answer"
    assert payload["additional_kwargs"] == {"keep": "message-sibling"}
    assert payload["response_metadata"] == {"provider": "visible-metadata"}
    assert assistant.tool_calls is not None
    assert json.loads(assistant.tool_calls[0].function.arguments) == tool_args


@pytest.mark.asyncio
async def test_structured_message_snapshot_filters_only_reserved_provider_reasoning() -> (
    None
):
    secret = "PROVIDER-SECRET"

    async def parts():
        yield {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    AIMessage(
                        id="ai-structured-snapshot",
                        content=[
                            {
                                "type": "data",
                                "additional_kwargs": {
                                    "reasoning_content": secret,
                                    "keep": "visible-sibling",
                                },
                                "reasoning_content": "BUSINESS-VALUE",
                            }
                        ],
                    )
                ]
            },
            "interrupts": ({"id": "pause", "value": {"pause": True}},),
        }

    events = [
        event async for event in astream_events(parts=parts(), identity=_identity())
    ]
    serialized = "\n".join(
        event.model_dump_json(by_alias=True, exclude_none=True) for event in events
    )

    assert secret not in serialized
    assert "visible-sibling" in serialized
    assert "BUSINESS-VALUE" in serialized
