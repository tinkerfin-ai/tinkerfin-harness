from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Callable

import pytest
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import AIMessageChunk
from langgraph.graph.state import CompiledStateGraph

from tinkerfin import AgentRuntime, RunIdentity


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")


def _graph_input() -> InputAgentState:
    return InputAgentState(messages=[])


class _SourceGraph:
    def __init__(self, source_factory: Callable[[], AsyncIterator[object]]) -> None:
        self._source_factory = source_factory

    def astream(
        self,
        *_args: object,
        **_options: object,
    ) -> AsyncIterator[object]:
        return self._source_factory()


setattr(
    _SourceGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


@pytest.mark.asyncio
async def test_native_object_stream_encodes_the_current_sse_contract(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        yield {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-1",
                "name": "child",
                "input": {},
                "triggers": ("branch:to:child",),
            },
        }
        yield {
            "type": "values",
            "ns": ("child:task-1",),
            "data": {"answer": 42},
            "interrupts": (),
        }

    body = (
        definition_factory(_SourceGraph(source))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse()
    )
    frames = [frame.decode("utf-8") async for frame in body]

    assert len(frames) == 2
    assert frames[-1].startswith("event: stream-part\n")
    payload = json.loads(frames[-1].split("data: ", maxsplit=1)[1])
    assert payload == {
        "type": "values",
        "ns": ["child:task-1"],
        "data": {
            "state": {"answer": 42},
            "messages": [],
            "messagesPresent": False,
            "interrupts": [],
        },
        "interrupts": [],
        "graphOrigin": {
            "parentTask": {
                "graphNamespace": [],
                "taskId": "task-1",
                "nodeName": "child",
            },
            "subagentRequest": None,
        },
        "subagentRequests": [],
    }


@pytest.mark.parametrize(
    "mode",
    [
        "messages",
        "tasks",
        "values",
        "updates",
        "checkpoints",
        "debug",
        "custom",
    ],
)
@pytest.mark.asyncio
async def test_native_object_stream_encodes_every_canonical_mode(
    mode: str,
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    payloads: dict[str, object] = {
        "messages": (
            AIMessageChunk(id="message-1", content="visible"),
            {"langgraph_node": "model"},
        ),
        "tasks": {
            "id": "task-1",
            "name": "model",
            "input": {},
            "triggers": (),
        },
        "values": {"messages": []},
        "updates": {"model": {"messages": []}},
        "checkpoints": {"checkpoint": "one"},
        "debug": {"debug": True},
        "custom": {"custom": True},
    }

    async def source() -> AsyncIterator[object]:
        yield {"type": mode, "ns": (), "data": payloads[mode]}

    body = (
        definition_factory(_SourceGraph(source))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse()
    )
    frames = [frame.decode("utf-8") async for frame in body]

    assert len(frames) == 1
    payload = json.loads(frames[0].split("data: ", maxsplit=1)[1])
    assert payload["type"] == mode
    assert payload["ns"] == []
    assert "data" in payload
    assert payload["interrupts"] == []


@pytest.mark.asyncio
async def test_agui_object_stream_encodes_protocol_json_without_event_name(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        if False:  # pragma: no cover - produces only lifecycle events
            yield None

    body = (
        definition_factory(_SourceGraph(source))
        .open_agui_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse()
    )
    frames = [frame.decode("utf-8") async for frame in body]

    assert [json.loads(frame.removeprefix("data: "))["type"] for frame in frames] == [
        "RUN_STARTED",
        "RUN_FINISHED",
    ]
    assert all("event:" not in frame for frame in frames)


@pytest.mark.asyncio
async def test_sse_prepare_runs_preflight_without_opening_the_source(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    factory_calls = 0
    preflights = 0

    async def source() -> AsyncIterator[object]:
        nonlocal factory_calls
        factory_calls += 1
        if False:  # pragma: no cover - validates lazy preparation
            yield None

    async def preflight() -> None:
        nonlocal preflights
        preflights += 1

    body = (
        definition_factory(_SourceGraph(source))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse()
    )

    await body.prepare(preflight=preflight)

    assert preflights == 1
    assert factory_calls == 0
    assert [frame.decode("utf-8") async for frame in body] == []
    assert factory_calls == 1
