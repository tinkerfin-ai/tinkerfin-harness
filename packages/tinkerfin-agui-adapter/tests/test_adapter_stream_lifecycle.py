from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any

import pytest
from ag_ui.core import BaseEvent, RunStartedEvent
from langchain_core.messages import AIMessageChunk, ChatMessage, ToolMessage
from pydantic import ValidationError

from tinkerfin_agui_adapter import (
    DeepAgentAgUiAdapter,
    RunIdentity,
    astream_events,
)
from tinkerfin_agui_adapter.ids import ScopedIdCodec


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


class _GateParts:
    """Yield the supplied parts, then make the next upstream pull observable."""

    def __init__(self, parts: list[object]) -> None:
        self._parts = parts
        self._index = 0
        self.pull_started = asyncio.Event()
        self.release_pull = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self.active_pulls = 0

    def __aiter__(self) -> _GateParts:
        return self

    async def __anext__(self) -> object:
        if self._index < len(self._parts):
            part = self._parts[self._index]
            self._index += 1
            return part
        self.pull_started.set()
        self.active_pulls += 1
        try:
            await self.release_pull.wait()
        finally:
            self.active_pulls -= 1
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed.set()


class _FailingCloseParts:
    """Finish normally but fail while releasing the upstream resource."""

    def __init__(self) -> None:
        self._finished = False
        self.close_calls = 0

    def __aiter__(self) -> _FailingCloseParts:
        return self

    async def __anext__(self) -> object:
        if self._finished:
            raise StopAsyncIteration
        self._finished = True
        return _text_part()

    async def aclose(self) -> None:
        self.close_calls += 1
        raise RuntimeError("upstream close failed")


class _FailingPartAndClose:
    """Emit one invalid part and fail while closing its upstream resource."""

    def __init__(self) -> None:
        self._finished = False

    def __aiter__(self) -> _FailingPartAndClose:
        return self

    async def __anext__(self) -> object:
        if self._finished:
            raise StopAsyncIteration
        self._finished = True
        return {"type": "invalid-mode", "ns": (), "data": {}}

    async def aclose(self) -> None:
        raise RuntimeError("secondary upstream close failure")


class _InterruptedCloseParts:
    """Finish normally while making an interrupted close retry observable."""

    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_completed = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _InterruptedCloseParts:
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        self.close_completed.set()


def _message_part(
    chunk: AIMessageChunk,
    namespace: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "messages",
        "ns": namespace,
        "data": (
            chunk,
            {"lc_agent_name": None, "langgraph_node": "model"},
        ),
    }


def _reasoning_part() -> dict[str, object]:
    return _message_part(
        AIMessageChunk(
            id="message-reasoning",
            content="",
            additional_kwargs={"reasoning_content": "visible reasoning"},
        )
    )


def _text_part() -> dict[str, object]:
    return _message_part(AIMessageChunk(id="message-text", content="buffered text"))


def _tool_part() -> dict[str, object]:
    return _message_part(
        AIMessageChunk(
            id="message-tool",
            content="",
            tool_call_chunks=[
                {
                    "name": "search",
                    "args": '{"query":"tinkerfin"}',
                    "id": "call-search",
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )
    )


async def _collect_through(
    stream: AsyncIterator[BaseEvent], event_type: str
) -> list[BaseEvent]:
    events: list[BaseEvent] = []
    while True:
        event = await anext(stream)
        events.append(event)
        if event.type.value == event_type:
            return events


@pytest.mark.asyncio
async def test_run_started_contains_only_the_canonical_identity() -> None:
    identity = _identity()

    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - supplies the async iterator shape
            yield None

    stream = astream_events(parts(), identity=identity)
    events = [event async for event in stream]

    assert isinstance(events[0], RunStartedEvent)
    assert events[0].thread_id == identity.thread_id
    assert events[0].run_id == identity.run_id
    assert events[0].parent_run_id is None
    assert events[0].input is None


@pytest.mark.asyncio
async def test_non_identity_fails_before_pulling_parts() -> None:
    pulled = False

    async def parts() -> AsyncIterator[object]:
        nonlocal pulled
        pulled = True
        yield _text_part()

    invalid_identity: Any = object()
    with pytest.raises(TypeError, match="identity must be a RunIdentity"):
        astream_events(
            parts(),
            identity=invalid_identity,
        )

    assert pulled is False


def test_identity_is_strict_frozen_and_serializes_protocol_aliases() -> None:
    identity = RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")

    assert identity.model_dump(by_alias=True) == {
        "namespace": "test",
        "threadId": "thread-1",
        "runId": "run-1",
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunIdentity.model_validate(
            {
                "namespace": "test",
                "threadId": "thread-1",
                "runId": "run-1",
                "parentRunId": "parent",
            }
        )
    with pytest.raises(ValidationError, match="surrounding whitespace"):
        RunIdentity(namespace="test", thread_id=" thread-1", run_id="run-1")
    with pytest.raises(ValidationError, match="frozen"):
        setattr(identity, "thread_id", "thread-2")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("part_factory", "expose_reasoning_events", "open_event"),
    [
        (_reasoning_part, True, "REASONING_MESSAGE_CONTENT"),
        (_text_part, False, "TEXT_MESSAGE_START"),
        (_tool_part, False, "TOOL_CALL_ARGS"),
    ],
)
async def test_consumer_close_abandons_open_child_lifecycle_without_run_error(
    part_factory: Callable[[], dict[str, object]],
    expose_reasoning_events: bool,
    open_event: str,
) -> None:
    parts = _GateParts([part_factory()])
    stream = astream_events(
        parts=parts,
        identity=_identity(),
        expose_reasoning_events=expose_reasoning_events,
    )
    assert isinstance(stream, AsyncGenerator)

    events = await _collect_through(stream, open_event)
    await stream.aclose()

    await parts.closed.wait()
    assert parts.close_calls == 1
    assert parts.active_pulls == 0
    assert not any(event.type.value == "RUN_ERROR" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("part_factory", "expose_reasoning_events", "open_event"),
    [
        (_reasoning_part, True, "REASONING_MESSAGE_CONTENT"),
        (_text_part, False, "TEXT_MESSAGE_START"),
        (_tool_part, False, "TOOL_CALL_ARGS"),
    ],
)
async def test_cancellation_during_blocked_upstream_pull_propagates_and_closes(
    part_factory: Callable[[], dict[str, object]],
    expose_reasoning_events: bool,
    open_event: str,
) -> None:
    parts = _GateParts([part_factory()])
    stream = astream_events(
        parts=parts,
        identity=_identity(),
        expose_reasoning_events=expose_reasoning_events,
    )
    events = await _collect_through(stream, open_event)

    pending_event = asyncio.ensure_future(anext(stream))
    await parts.pull_started.wait()
    pending_event.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending_event
    await parts.closed.wait()
    assert parts.close_calls == 1
    assert parts.active_pulls == 0
    assert not any(event.type.value == "RUN_ERROR" for event in events)


@pytest.mark.asyncio
async def test_normal_terminal_closes_every_delivered_child_lifecycle_first() -> None:
    async def parts() -> AsyncIterator[object]:
        yield _reasoning_part()
        yield _message_part(
            AIMessageChunk(
                id="message-reasoning", content="answer", chunk_position="last"
            )
        )
        yield _tool_part()

    events = [
        event
        async for event in astream_events(
            parts=parts(),
            identity=_identity(),
            expose_reasoning_events=True,
        )
    ]
    event_types = [event.type.value for event in events]
    terminal_index = event_types.index("RUN_FINISHED")

    for start, end in (
        ("REASONING_START", "REASONING_END"),
        ("REASONING_MESSAGE_START", "REASONING_MESSAGE_END"),
        ("TEXT_MESSAGE_START", "TEXT_MESSAGE_END"),
        ("TOOL_CALL_START", "TOOL_CALL_END"),
    ):
        assert event_types.index(start) < event_types.index(end) < terminal_index
    assert "RUN_ERROR" not in event_types


@pytest.mark.parametrize(
    ("opening_chunk", "expose_reasoning_events", "expected_end_types"),
    [
        (
            AIMessageChunk(id="message-final-text", content="partial"),
            False,
            ["TEXT_MESSAGE_END"],
        ),
        (
            AIMessageChunk(
                id="message-final-reasoning",
                content="",
                additional_kwargs={"reasoning_content": "partial reasoning"},
            ),
            True,
            ["REASONING_MESSAGE_END", "REASONING_END"],
        ),
        (
            AIMessageChunk(
                id="message-final-tool",
                content="",
                tool_call_chunks=[
                    {
                        "name": "search",
                        "args": '{"query":"one"}',
                        "id": "call-final-one",
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            ),
            False,
            ["TOOL_CALL_END"],
        ),
        (
            AIMessageChunk(
                id="message-final-parallel-tools",
                content="",
                tool_call_chunks=[
                    {
                        "name": "search",
                        "args": '{"query":"one"}',
                        "id": "call-final-one",
                        "index": 0,
                        "type": "tool_call_chunk",
                    },
                    {
                        "name": "search",
                        "args": '{"query":"two"}',
                        "id": "call-final-two",
                        "index": 1,
                        "type": "tool_call_chunk",
                    },
                ],
            ),
            False,
            ["TOOL_CALL_END", "TOOL_CALL_END"],
        ),
    ],
    ids=("text", "reasoning", "tool", "parallel-tools"),
)
def test_empty_final_chunk_closes_active_namespace_lifecycles(
    opening_chunk: AIMessageChunk,
    expose_reasoning_events: bool,
    expected_end_types: list[str],
) -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(run_id="run-final-boundary"),
        expose_reasoning_events=expose_reasoning_events,
    )
    adapter.process(_message_part(opening_chunk))

    events = adapter.process(
        _message_part(
            AIMessageChunk(
                id=opening_chunk.id,
                content="",
                chunk_position="last",
            )
        )
    )

    assert [event.type.value for event in events] == expected_end_types


@pytest.mark.parametrize(
    ("opening_chunk", "continuation_chunk", "expected_continuation_types"),
    [
        (
            AIMessageChunk(id="message-heartbeat-text", content="first"),
            AIMessageChunk(id="message-heartbeat-text", content="second"),
            ["TEXT_MESSAGE_CONTENT"],
        ),
        (
            AIMessageChunk(
                id="message-heartbeat-reasoning",
                content="",
                additional_kwargs={"reasoning_content": "first"},
            ),
            AIMessageChunk(
                id="message-heartbeat-reasoning",
                content="",
                additional_kwargs={"reasoning_content": "second"},
            ),
            ["REASONING_MESSAGE_CONTENT"],
        ),
        (
            AIMessageChunk(
                id="message-heartbeat-tool",
                content="",
                tool_call_chunks=[
                    {
                        "name": "search",
                        "args": "{",
                        "id": "call-heartbeat-tool",
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            ),
            AIMessageChunk(
                id="message-heartbeat-tool",
                content="",
                tool_call_chunks=[
                    {
                        "name": None,
                        "args": "}",
                        "id": None,
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            ),
            ["TOOL_CALL_ARGS"],
        ),
    ],
    ids=("text", "reasoning", "tool"),
)
def test_empty_nonfinal_heartbeat_keeps_active_lifecycle_open(
    opening_chunk: AIMessageChunk,
    continuation_chunk: AIMessageChunk,
    expected_continuation_types: list[str],
) -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(run_id="run-heartbeat"),
        expose_reasoning_events=True,
    )
    adapter.process(_message_part(opening_chunk))

    heartbeat = adapter.process(
        _message_part(AIMessageChunk(id=opening_chunk.id, content=""))
    )
    continuation = adapter.process(_message_part(continuation_chunk))

    assert heartbeat == []
    assert [event.type.value for event in continuation] == expected_continuation_types


def test_empty_final_chunk_closes_only_its_own_namespace() -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(run_id="run-final-namespace"),
        expose_subagent_events=True,
    )
    adapter.process(
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "graph-child",
                "name": "tools",
                "input": [],
                "triggers": ("branch:to:tools",),
            },
        }
    )
    child_namespace = ("tools:graph-child",)
    adapter.process(_message_part(AIMessageChunk(id="message-root", content="root")))
    adapter.process(
        _message_part(
            AIMessageChunk(id="message-child", content="child"),
            child_namespace,
        )
    )

    root_final = adapter.process(
        _message_part(
            AIMessageChunk(
                id="message-root",
                content="",
                chunk_position="last",
            )
        )
    )
    child_continuation = adapter.process(
        _message_part(
            AIMessageChunk(id="message-child", content=" still open"),
            child_namespace,
        )
    )

    assert [event.type.value for event in root_final] == ["TEXT_MESSAGE_END"]
    assert [event.type.value for event in child_continuation] == [
        "TEXT_MESSAGE_CONTENT"
    ]


@pytest.mark.asyncio
async def test_root_values_boundary_closes_an_open_child_message_lifecycle() -> None:
    child_namespace = ("tools:graph-child",)

    async def parts() -> AsyncIterator[object]:
        yield {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "graph-child",
                "name": "tools",
                "input": [
                    {
                        "name": "task",
                        "args": {
                            "description": "Research",
                            "subagent_type": "researcher",
                        },
                        "id": "call-child",
                        "type": "tool_call",
                    }
                ],
                "triggers": ("branch:to:tools",),
            },
        }
        yield {
            "type": "messages",
            "ns": child_namespace,
            "data": (
                AIMessageChunk(id="child-message", content="open child text"),
                {"lc_agent_name": "researcher", "langgraph_node": "model"},
            ),
        }
        yield {
            "type": "values",
            "ns": (),
            "data": {"messages": [], "step": "root-boundary"},
            "interrupts": (),
        }

    events = [
        event
        async for event in astream_events(
            parts(),
            identity=_identity(),
            expose_subagent_events=True,
        )
    ]
    event_types = [event.type.value for event in events]

    assert event_types.index("TEXT_MESSAGE_END") < event_types.index("STATE_SNAPSHOT")
    assert event_types.count("RUN_FINISHED") == 1


@pytest.mark.asyncio
async def test_prior_tool_call_ids_flow_through_the_public_stream() -> None:
    async def parts() -> AsyncIterator[object]:
        for message in (
            ToolMessage(
                id="result-prior",
                name="write_file",
                tool_call_id="call-prior",
                content="prior done",
            ),
            ToolMessage(
                id="result-new",
                name="read_file",
                tool_call_id="call-new",
                content="new done",
            ),
        ):
            yield {
                "type": "messages",
                "ns": (),
                "data": (
                    message,
                    {"lc_agent_name": None, "langgraph_node": "tools"},
                ),
            }

    events = [
        event
        async for event in astream_events(
            parts(),
            identity=_identity(),
            prior_tool_call_ids=frozenset(
                {ScopedIdCodec().encode("tool", (), "call-prior")}
            ),
        )
    ]
    event_types = [event.type.value for event in events]

    assert [
        event_type for event_type in event_types if event_type.startswith("TOOL_")
    ] == [
        "TOOL_CALL_RESULT",
        "TOOL_CALL_START",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
    ]
    assert event_types.count("RUN_FINISHED") == 1
    assert "RUN_ERROR" not in event_types


@pytest.mark.asyncio
async def test_consumer_close_after_start_closes_upstream_iterator() -> None:
    parts = _GateParts([])
    stream = astream_events(parts=parts, identity=_identity())
    assert isinstance(stream, AsyncGenerator)

    await anext(stream)
    await stream.aclose()

    await parts.closed.wait()
    assert parts.close_calls == 1


@pytest.mark.asyncio
async def test_upstream_close_failure_becomes_error_before_any_success_terminal() -> (
    None
):
    parts = _FailingCloseParts()

    events = [
        event async for event in astream_events(parts=parts, identity=_identity())
    ]
    event_types = [event.type.value for event in events]

    assert parts.close_calls == 1
    assert event_types.count("RUN_ERROR") == 1
    assert "RUN_FINISHED" not in event_types
    assert event_types[-1] == "RUN_ERROR"


@pytest.mark.asyncio
async def test_conversion_and_secondary_close_failures_emit_no_package_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    parts = _FailingPartAndClose()

    with caplog.at_level("DEBUG", logger="tinkerfin.agui"):
        events = [
            event
            async for event in astream_events(
                parts=parts,
                identity=_identity(),
            )
        ]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert caplog.records == []


@pytest.mark.asyncio
async def test_cancelled_terminal_pull_finishes_interrupted_upstream_close() -> None:
    parts = _InterruptedCloseParts()
    stream = astream_events(parts, identity=_identity())
    assert isinstance(stream, AsyncGenerator)
    assert (await anext(stream)).type.value == "RUN_STARTED"
    terminal_pull = asyncio.create_task(anext(stream))
    await parts.close_started.wait()

    terminal_pull.cancel("request cancelled during adapter parts close")
    await parts.close_cancelled.wait()
    parts.release_close.set()

    try:
        with pytest.raises(
            asyncio.CancelledError,
            match="request cancelled during adapter parts close",
        ):
            await terminal_pull
        await stream.aclose()
    finally:
        parts.release_close.set()
        await asyncio.gather(terminal_pull, return_exceptions=True)

    assert parts.close_calls == 2
    assert parts.close_completed.is_set()


@pytest.mark.asyncio
async def test_snapshot_conversion_failure_closes_open_text_before_unique_run_error() -> (
    None
):
    async def parts() -> AsyncIterator[object]:
        yield _text_part()
        yield {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    ChatMessage(
                        id="unsupported-snapshot-message",
                        role="critic",
                        content="unsupported",
                    )
                ]
            },
            "interrupts": ({"id": "pause", "value": {"pause": True}},),
        }

    events = [
        event async for event in astream_events(parts=parts(), identity=_identity())
    ]
    event_types = [event.type.value for event in events]

    assert "TEXT_MESSAGE_END" in event_types
    assert event_types.count("RUN_ERROR") == 1
    assert event_types.index("TEXT_MESSAGE_END") < event_types.index("RUN_ERROR")


@pytest.mark.asyncio
async def test_failed_ai_chunk_does_not_leave_undelivered_reasoning_lifecycle() -> None:
    async def parts() -> AsyncIterator[object]:
        yield _message_part(
            AIMessageChunk(
                id="message-invalid-content",
                content=[{"type": "data", "value": object()}],
                additional_kwargs={"reasoning_content": "PRIVATE"},
                chunk_position="last",
            )
        )

    events = [
        event
        async for event in astream_events(
            parts=parts(),
            identity=_identity(),
            expose_reasoning_events=True,
        )
    ]
    event_types = [event.type.value for event in events]

    assert event_types == ["RUN_STARTED", "RUN_ERROR"]
    assert not any(event_type.startswith("REASONING_") for event_type in event_types)


@pytest.mark.asyncio
async def test_duplicate_tool_id_in_one_part_fails_before_any_child_lifecycle() -> None:
    async def parts() -> AsyncIterator[object]:
        yield _message_part(
            AIMessageChunk(
                id="message-duplicate-tool",
                content="",
                tool_call_chunks=[
                    {
                        "name": "alpha",
                        "args": "{",
                        "id": "same-id",
                        "index": 0,
                        "type": "tool_call_chunk",
                    },
                    {
                        "name": "beta",
                        "args": "{",
                        "id": "same-id",
                        "index": 1,
                        "type": "tool_call_chunk",
                    },
                    {
                        "name": None,
                        "args": "}",
                        "id": None,
                        "index": 1,
                        "type": "tool_call_chunk",
                    },
                ],
            )
        )

    events = [
        event async for event in astream_events(parts=parts(), identity=_identity())
    ]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
