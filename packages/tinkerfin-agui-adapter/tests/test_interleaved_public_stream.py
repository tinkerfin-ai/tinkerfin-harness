"""Public interrupt privacy and interleaved text/Tool lifecycle contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from ag_ui.core import (
    MessagesSnapshotEvent,
    RunFinishedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from ag_ui.core.types import ResumeEntry
from langchain_core.messages import AIMessage, AIMessageChunk

from tinkerfin_agui_adapter import (
    ResumeMapper,
    RunIdentity,
    RuntimeInterruptEnvelope,
    astream_events,
    encode_sse,
)


@pytest.mark.parametrize("expose_reasoning", (False, True))
async def test_interrupt_correlation_is_public_and_remains_resumable(
    expose_reasoning: bool,
) -> None:
    envelope = RuntimeInterruptEnvelope(
        kind="input_required",
        response_schema={"type": "object"},
        metadata={
            "additional_kwargs": {"reasoning_content": "PRIVATE_PROVIDER_MARKER"},
            "business": {"reasoning_content": "keep-business-value"},
        },
    )

    async def parts() -> AsyncIterator[object]:
        yield {
            "type": "values",
            "ns": (),
            "data": {"messages": []},
            "interrupts": (
                {
                    "id": "question",
                    "value": envelope.model_dump(mode="json", by_alias=True),
                },
            ),
        }

    events = [
        event
        async for event in astream_events(
            parts(),
            identity=RunIdentity(
                namespace="test", thread_id="privacy-thread", run_id="privacy-run"
            ),
            expose_reasoning_events=expose_reasoning,
        )
    ]
    wire = "".join(encode_sse(event) for event in events)
    assert "PRIVATE_PROVIDER_MARKER" not in wire
    assert "keep-business-value" in wire
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    restored = RunFinishedEvent.model_validate_json(terminal.model_dump_json())
    outcome = restored.outcome
    assert outcome is not None and outcome.type == "interrupt"
    assert outcome.interrupts[0].response_schema == {"type": "object"}
    translation = ResumeMapper().map_agui(
        entries=(
            ResumeEntry.model_validate(
                {
                    "interruptId": "question",
                    "status": "resolved",
                    "payload": {"answer": "accepted"},
                }
            ),
        ),
        interrupts=outcome.interrupts,
    )
    assert translation.root == {"answer": "accepted"}
    assert envelope.metadata["additional_kwargs"] == {
        "reasoning_content": "PRIVATE_PROVIDER_MARKER"
    }


@pytest.mark.parametrize("final_kind", ("text", "tool", "empty"))
async def test_interleaved_text_and_parallel_tools_share_one_message(
    final_kind: str,
) -> None:
    chunks = [
        AIMessageChunk(id="message", content="before"),
        AIMessageChunk(
            id="message",
            content="",
            tool_call_chunks=[
                {"id": "first", "index": 0, "name": "lookup", "args": "{"}
            ],
        ),
        AIMessageChunk(
            id="message",
            content="middle",
            tool_call_chunks=[
                {"id": "second", "index": 1, "name": "lookup", "args": "{}"}
            ],
        ),
    ]
    tool_tail = AIMessageChunk(
        id="message",
        content="",
        tool_call_chunks=[{"index": 0, "args": "}", "id": None, "name": None}],
        chunk_position="last" if final_kind == "tool" else None,
    )
    text_tail = AIMessageChunk(
        id="message",
        content="after",
        chunk_position="last" if final_kind == "text" else None,
    )
    chunks.extend(
        (text_tail, tool_tail) if final_kind == "tool" else (tool_tail, text_tail)
    )
    if final_kind == "empty":
        chunks.append(AIMessageChunk(id="message", content="", chunk_position="last"))

    async def parts() -> AsyncIterator[object]:
        for chunk in chunks:
            yield {
                "type": "messages",
                "ns": (),
                "data": (chunk, {"langgraph_node": "model"}),
            }
        yield {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    AIMessage(
                        id="message",
                        content="beforemiddleafter",
                        tool_calls=[
                            {"id": "first", "name": "lookup", "args": {}},
                            {"id": "second", "name": "lookup", "args": {}},
                        ],
                    )
                ]
            },
            "interrupts": ({"id": "continue", "value": "Continue?"},),
        }

    events = [
        event
        async for event in astream_events(
            parts(),
            identity=RunIdentity(
                namespace="test", thread_id="mixed-thread", run_id="mixed-run"
            ),
        )
    ]
    starts = [event for event in events if isinstance(event, TextMessageStartEvent)]
    ends = [event for event in events if isinstance(event, TextMessageEndEvent)]
    assert len(starts) == len(ends) == 1
    message_id = starts[0].message_id
    assert ends[0].message_id == message_id
    content = [event for event in events if isinstance(event, TextMessageContentEvent)]
    assert "".join(event.delta for event in content) == "beforemiddleafter"
    assert all(events.index(event) < events.index(ends[0]) for event in content)
    tools = [event for event in events if isinstance(event, ToolCallStartEvent)]
    assert len(tools) == 2
    for tool in tools:
        assert tool.parent_message_id == message_id
        arguments = [
            event.delta
            for event in events
            if isinstance(event, ToolCallArgsEvent)
            and event.tool_call_id == tool.tool_call_id
        ]
        assert "".join(arguments) == "{}"
        assert (
            sum(
                isinstance(event, ToolCallEndEvent)
                and event.tool_call_id == tool.tool_call_id
                for event in events
            )
            == 1
        )
    snapshots = [event for event in events if isinstance(event, MessagesSnapshotEvent)]
    assert snapshots[-1].messages[0].id == message_id
    assert snapshots[-1].messages[0].content == "beforemiddleafter"
    assert sum(isinstance(event, RunFinishedEvent) for event in events) == 1
