from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable

import pytest
from ag_ui.core import (
    BaseEvent,
    MessagesSnapshotEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageStartEvent,
)
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from tinkerfin_agui_adapter import (
    AgUiStreamContractError,
    DeepAgentAgUiAdapter,
    RunIdentity,
    astream_events,
)
from tinkerfin_agui_adapter.microbatch import ContentBatcher, micro_batch
from tinkerfin_native_stream import NativeStreamContractError


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")


def _message_part(message: AIMessageChunk) -> dict[str, object]:
    return {
        "type": "messages",
        "ns": (),
        "data": (message, {"lc_agent_name": None, "langgraph_node": "model"}),
    }


def test_message_only_values_does_not_create_a_delta_baseline() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    assert (
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {"messages": []},
                "interrupts": (),
            }
        )
        == []
    )

    events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"step": 2},
            "interrupts": (),
        }
    )

    assert len(events) == 1
    assert isinstance(events[0], StateSnapshotEvent)
    assert events[0].snapshot == {"step": 2}


@pytest.mark.parametrize("expose_reasoning_events", [False, True])
@pytest.mark.parametrize(
    "business_block",
    [
        {"type": "reasoning", "reasoning": "business reasoning"},
        {"type": "thinking", "thinking": "business thinking"},
    ],
)
def test_content_blocks_are_business_data_not_provider_reasoning(
    expose_reasoning_events: bool,
    business_block: dict[str, str],
) -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(), expose_reasoning_events=expose_reasoning_events
    )

    live_events = adapter.process(
        _message_part(AIMessageChunk(id="message-1", content=[business_block]))
    )
    snapshot_events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [AIMessage(id="message-1", content=[business_block])],
                "state": "visible",
            },
            "interrupts": ({"id": "pause", "value": {"pause": True}},),
        }
    )

    assert not any(event.type.value.startswith("REASONING_") for event in live_events)
    snapshot = next(
        event for event in snapshot_events if isinstance(event, MessagesSnapshotEvent)
    )
    assert snapshot.messages[0].content == json.dumps(
        [business_block], ensure_ascii=False
    )


@pytest.mark.parametrize("expose_reasoning_events", [False, True])
@pytest.mark.asyncio
async def test_successful_structured_ai_content_uses_one_business_text_projection(
    expose_reasoning_events: bool,
) -> None:
    secret = "PROVIDER-SECRET"
    content = [
        {"type": "text", "text": "visible text"},
        {"type": "reasoning", "reasoning": "business reasoning"},
        {
            "type": "thinking",
            "thinking": "business thinking",
            "additional_kwargs": {
                "reasoning_content": secret,
                "keep": "visible-sibling",
            },
        },
    ]
    expected_content = json.dumps(
        [
            {"type": "text", "text": "visible text"},
            {"type": "reasoning", "reasoning": "business reasoning"},
            {
                "type": "thinking",
                "thinking": "business thinking",
                "additional_kwargs": {"keep": "visible-sibling"},
            },
        ],
        ensure_ascii=False,
    )

    async def parts():
        yield _message_part(
            AIMessageChunk(
                id="message-structured",
                content=content,
                chunk_position="last",
            )
        )
        yield {
            "type": "values",
            "ns": (),
            "data": {"messages": [AIMessage(id="message-structured", content=content)]},
            "interrupts": (),
        }

    events = [
        event
        async for event in astream_events(
            parts=parts(),
            identity=_identity(),
            expose_reasoning_events=expose_reasoning_events,
        )
    ]
    text_events = [
        event for event in events if isinstance(event, TextMessageContentEvent)
    ]
    serialized = "\n".join(
        event.model_dump_json(by_alias=True, exclude_none=True) for event in events
    )

    assert [event.delta for event in text_events] == [expected_content]
    assert not any(event.type.value.startswith("REASONING_") for event in events)
    assert secret not in serialized
    assert "visible-sibling" in serialized
    assert serialized.count("visible text") == 1


def test_opaque_content_block_fails_closed_at_snapshot_boundary() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    with pytest.raises(AgUiStreamContractError) as raised:
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {
                    "messages": [
                        AIMessage(
                            id="message-opaque",
                            content=[{"kind": "opaque", "value": object()}],
                        )
                    ],
                    "state": "visible",
                },
                "interrupts": ({"id": "pause", "value": {"pause": True}},),
            }
        )
    assert isinstance(raised.value.cause, PydanticSerializationError)


@pytest.mark.parametrize(
    "malformed_part",
    [
        {
            "type": "messages",
            "ns": [],
            "data": (
                AIMessageChunk(id="message-invalid-ns", content="invalid"),
                {},
            ),
        },
        {
            "type": "messages",
            "ns": set(),
            "data": (
                AIMessageChunk(id="message-invalid-set", content="invalid"),
                {},
            ),
        },
        {
            "type": "messages",
            "ns": (),
            "data": [
                AIMessageChunk(id="message-invalid-data", content="invalid"),
                {},
            ],
        },
    ],
)
def test_malformed_v2_container_shapes_are_rejected_without_state_mutation(
    malformed_part: dict[str, object],
) -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_message_part(AIMessageChunk(id="message-open", content="visible")))

    with pytest.raises(AgUiStreamContractError) as raised:
        adapter.process(malformed_part)
    assert isinstance(raised.value.cause, NativeStreamContractError)
    assert isinstance(raised.value.cause.cause, ValidationError)

    assert [event.type.value for event in adapter.finish()] == ["TEXT_MESSAGE_END"]


@pytest.mark.parametrize(
    "malformed_part",
    [
        {
            "type": "messages",
            "ns": (),
            "data": (
                {"type": "ai", "content": "serialized", "id": "message-dict"},
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        },
        {"type": "values", "ns": (), "data": {}, "interrupts": []},
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-start-list",
                "name": "model",
                "input": {},
                "triggers": [],
            },
        },
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-result-list",
                "name": "model",
                "error": None,
                "interrupts": (),
                "result": {},
            },
        },
    ],
    ids=(
        "serialized-message",
        "values-interrupt-list",
        "task-trigger-list",
        "task-interrupt-tuple",
    ),
)
def test_native_v2_objects_and_tuple_fields_reject_coerced_shapes_before_mutation(
    malformed_part: dict[str, object],
) -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(
        _message_part(AIMessageChunk(id="message-open-strict", content="visible"))
    )

    with pytest.raises(AgUiStreamContractError) as raised:
        adapter.process(malformed_part)
    assert isinstance(raised.value.cause, NativeStreamContractError)
    assert isinstance(raised.value.cause.cause, ValidationError)

    assert [event.type.value for event in adapter.finish()] == ["TEXT_MESSAGE_END"]


def _open_reasoning(adapter: DeepAgentAgUiAdapter) -> list[BaseEvent]:
    return adapter.process(
        _message_part(
            AIMessageChunk(
                id="message-reasoning",
                content="",
                additional_kwargs={"reasoning_content": "reasoning"},
            )
        )
    )


def _open_text(adapter: DeepAgentAgUiAdapter) -> list[BaseEvent]:
    return adapter.process(
        _message_part(AIMessageChunk(id="message-text", content="answer"))
    )


def _open_tool(adapter: DeepAgentAgUiAdapter) -> list[BaseEvent]:
    return adapter.process(
        _message_part(
            AIMessageChunk(
                id="message-tool",
                content="",
                tool_call_chunks=[
                    {
                        "name": "search",
                        "args": '{"query":"test"}',
                        "id": "call-search",
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            )
        )
    )


@pytest.mark.parametrize(
    ("open_stream", "expose_reasoning_events", "end_type"),
    [
        (_open_reasoning, True, "REASONING_END"),
        (_open_text, False, "TEXT_MESSAGE_END"),
        (_open_tool, False, "TOOL_CALL_END"),
    ],
)
@pytest.mark.parametrize("second_state", [False, True])
def test_root_state_closes_child_lifecycles_before_snapshot_or_delta(
    open_stream: Callable[[DeepAgentAgUiAdapter], list[BaseEvent]],
    expose_reasoning_events: bool,
    end_type: str,
    second_state: bool,
) -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(), expose_reasoning_events=expose_reasoning_events
    )
    if second_state:
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {"step": 1},
                "interrupts": (),
            }
        )
    open_stream(adapter)

    events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"step": 2},
            "interrupts": (),
        }
    )
    event_types = [event.type.value for event in events]
    state_type = "STATE_DELTA" if second_state else "STATE_SNAPSHOT"

    assert event_types.index(end_type) < event_types.index(state_type)
    assert adapter.finish() == []


def test_content_batcher_flushes_at_exact_character_threshold() -> None:
    batcher = ContentBatcher()

    assert (
        batcher.add(TextMessageContentEvent(message_id="message-1", delta="a" * 1023))
        == []
    )
    emitted = batcher.add(TextMessageContentEvent(message_id="message-1", delta="b"))

    assert len(emitted) == 1
    assert isinstance(emitted[0], TextMessageContentEvent)
    assert emitted[0].delta == "a" * 1023 + "b"


def test_content_batcher_merges_only_consecutive_deltas_for_one_message() -> None:
    batcher = ContentBatcher()

    assert batcher.add(TextMessageContentEvent(message_id="message-1", delta="a")) == []
    assert batcher.add(TextMessageContentEvent(message_id="message-1", delta="b")) == []
    emitted = batcher.add(TextMessageContentEvent(message_id="message-2", delta="c"))

    assert len(emitted) == 1
    first = emitted[0]
    assert isinstance(first, TextMessageContentEvent)
    assert (first.message_id, first.delta) == ("message-1", "ab")
    flushed = batcher.flush()
    assert len(flushed) == 1
    second = flushed[0]
    assert isinstance(second, TextMessageContentEvent)
    assert (second.message_id, second.delta) == ("message-2", "c")


def test_content_batcher_flushes_before_every_non_text_event() -> None:
    batcher = ContentBatcher()
    boundary = TextMessageStartEvent(message_id="message-2")
    assert (
        batcher.add(TextMessageContentEvent(message_id="message-1", delta="pending"))
        == []
    )

    emitted = batcher.add(boundary)

    assert [event.type.value for event in emitted] == [
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_START",
    ]
    assert emitted[1] is boundary


@pytest.mark.asyncio
async def test_microbatch_accepts_an_empty_iterator() -> None:
    async def events() -> AsyncGenerator[BaseEvent, None]:
        if False:  # pragma: no cover - supplies the empty asynchronous shape
            yield TextMessageStartEvent(message_id="unused")

    assert [event async for event in micro_batch(events())] == []


class _BlockedFailingCloseEvents:
    def __init__(self) -> None:
        self.pull_started = asyncio.Event()
        self.pull_cancelled = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _BlockedFailingCloseEvents:
        return self

    async def __anext__(self) -> BaseEvent:
        self.pull_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.pull_cancelled.set()
            raise
        raise AssertionError("blocked pull unexpectedly resumed")

    async def aclose(self) -> None:
        self.close_calls += 1
        raise RuntimeError("upstream close failed")


class _PullAndCloseFailureEvents:
    def __init__(self) -> None:
        self.pull_error = RuntimeError("upstream pull failed")
        self.close_calls = 0

    def __aiter__(self) -> _PullAndCloseFailureEvents:
        return self

    async def __anext__(self) -> BaseEvent:
        raise self.pull_error

    async def aclose(self) -> None:
        self.close_calls += 1
        raise RuntimeError("upstream close failed")


class _BlockingCloseEvents:
    def __init__(self) -> None:
        self.pull_started = asyncio.Event()
        self.pull_cancelled = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_completed = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _BlockingCloseEvents:
        return self

    async def __anext__(self) -> BaseEvent:
        self.pull_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.pull_cancelled.set()
            raise
        raise AssertionError("blocked pull unexpectedly resumed")

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        self.close_completed.set()


class _FailingPullBlockingCloseEvents:
    def __init__(self) -> None:
        self.pull_error = RuntimeError("upstream pull failed")
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_completed = asyncio.Event()

    def __aiter__(self) -> _FailingPullBlockingCloseEvents:
        return self

    async def __anext__(self) -> BaseEvent:
        raise self.pull_error

    async def aclose(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        self.close_completed.set()


class _BlockingPartClose:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_completed = asyncio.Event()
        self.close_error = close_error
        self.close_calls = 0

    def __aiter__(self) -> _BlockingPartClose:
        return self

    async def __anext__(self) -> object:
        await asyncio.Event().wait()
        raise AssertionError("blocked part pull unexpectedly resumed")

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.release_close.wait()
        self.close_completed.set()
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.asyncio
async def test_cancellation_remains_primary_when_upstream_close_fails() -> None:
    events = _BlockedFailingCloseEvents()
    stream = micro_batch(events)
    pull = asyncio.create_task(anext(stream))
    await asyncio.wait_for(events.pull_started.wait(), timeout=1)

    pull.cancel()

    with pytest.raises(asyncio.CancelledError) as caught:
        await pull
    assert events.pull_cancelled.is_set()
    assert events.close_calls == 1
    assert any(
        "upstream close also failed: RuntimeError: upstream close failed" in note
        for note in caught.value.__notes__
    )


@pytest.mark.asyncio
async def test_pull_error_remains_primary_when_upstream_close_fails() -> None:
    events = _PullAndCloseFailureEvents()
    stream = micro_batch(events)

    with pytest.raises(RuntimeError, match="upstream pull failed") as caught:
        await anext(stream)

    assert caught.value is events.pull_error
    assert events.close_calls == 1
    assert any(
        "upstream close also failed: RuntimeError: upstream close failed" in note
        for note in caught.value.__notes__
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_microbatch_cleanup() -> None:
    events = _BlockingCloseEvents()
    stream = micro_batch(events)
    baseline_tasks = set(asyncio.all_tasks())
    consumer = asyncio.create_task(anext(stream))
    await asyncio.wait_for(events.pull_started.wait(), timeout=1)

    consumer.cancel()
    await asyncio.wait_for(events.close_started.wait(), timeout=1)
    consumer.cancel()
    events.release_close.set()

    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.sleep(0)

    assert events.pull_cancelled.is_set()
    assert events.close_calls == 1
    assert events.close_completed.is_set()
    assert not events.close_cancelled.is_set()
    assert not [
        task
        for task in asyncio.all_tasks()
        if task not in baseline_tasks and not task.done()
    ]


@pytest.mark.asyncio
async def test_cleanup_cancellation_overrides_an_earlier_pull_failure() -> None:
    events = _FailingPullBlockingCloseEvents()
    consumer = asyncio.create_task(anext(micro_batch(events)))
    await asyncio.wait_for(events.close_started.wait(), timeout=1)

    consumer.cancel("caller cancelled during microbatch cleanup")
    events.release_close.set()

    with pytest.raises(
        asyncio.CancelledError,
        match="caller cancelled during microbatch cleanup",
    ):
        await consumer
    assert events.close_completed.is_set()


@pytest.mark.asyncio
async def test_public_stream_close_propagates_cancellation_after_cleanup() -> None:
    parts = _BlockingPartClose()
    stream = astream_events(parts=parts, identity=_identity())
    assert isinstance(stream, AsyncGenerator)
    assert (await anext(stream)).type.value == "RUN_STARTED"

    close_task = asyncio.ensure_future(stream.aclose())
    await asyncio.wait_for(parts.close_started.wait(), timeout=1)
    close_task.cancel("caller cancelled public stream close")
    parts.release_close.set()

    with pytest.raises(
        asyncio.CancelledError,
        match="caller cancelled public stream close",
    ):
        await close_task
    assert parts.close_calls == 1
    assert parts.close_completed.is_set()


@pytest.mark.asyncio
async def test_public_stream_close_propagates_upstream_cleanup_failure() -> None:
    cleanup_error = RuntimeError("upstream close failed")
    parts = _BlockingPartClose(close_error=cleanup_error)
    parts.release_close.set()
    stream = astream_events(parts=parts, identity=_identity())
    assert isinstance(stream, AsyncGenerator)
    assert (await anext(stream)).type.value == "RUN_STARTED"

    with pytest.raises(RuntimeError) as raised:
        await stream.aclose()

    assert raised.value is cleanup_error
    assert parts.close_calls == 1
    assert parts.close_completed.is_set()
