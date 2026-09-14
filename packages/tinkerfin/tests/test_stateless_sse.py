from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import cast

import pytest
from ag_ui.core import BaseEvent
from langchain.agents.middleware.types import InputAgentState
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from tinkerfin import (
    AgentRuntime,
    NativeStreamPart,
    RunIdentity,
    SseBody,
    SseMapper,
    SsePayload,
)


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


class _CountingParts:
    def __init__(self) -> None:
        self.pulls = 0
        self.closed = asyncio.Event()

    def __aiter__(self) -> _CountingParts:
        return self

    async def __anext__(self) -> object:
        self.pulls += 1
        if self.pulls == 1:
            return {
                "type": "values",
                "ns": (),
                "data": {"value": 1},
                "interrupts": (),
            }
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed.set()


class _BlockingParts:
    def __init__(self) -> None:
        self.pull_started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _BlockingParts:
        return self

    async def __anext__(self) -> object:
        self.pull_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed.set()


@pytest.mark.asyncio
async def test_prepare_does_not_observe_or_resolve_an_event_id(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    parts = _CountingParts()
    order: list[str] = []

    async def preflight() -> None:
        order.append("preflight")

    async def on_event(event: BaseEvent) -> None:
        order.append(f"observe:{event.type.value}")

    async def event_id_resolver(event: BaseEvent) -> str:
        order.append(f"resolve:{event.type.value}")
        return f"event-{len(order)}"

    body = (
        definition_factory(_SourceGraph(lambda: parts))
        .open_agui_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            on_agui_event=on_event,
            input=_graph_input(),
        )
        .to_sse(event_id_resolver=event_id_resolver)
    )

    await body.prepare(preflight=preflight)

    assert order == ["preflight"]
    assert parts.pulls == 0

    first = (await anext(body)).decode("utf-8")

    assert first.startswith("id: event-3\n")
    assert '"type":"RUN_STARTED"' in first
    assert order == [
        "preflight",
        "observe:RUN_STARTED",
        "resolve:RUN_STARTED",
    ]
    assert parts.pulls == 0

    remaining = [frame.decode("utf-8") async for frame in body]
    assert len(remaining) == 2
    assert parts.pulls == 2
    assert parts.closed.is_set()


@pytest.mark.asyncio
async def test_preflight_failure_closes_the_claimed_body_without_calling_factory(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    factory_calls = 0

    async def source() -> AsyncIterator[object]:
        nonlocal factory_calls
        factory_calls += 1
        if False:  # pragma: no cover - preflight must not open this source
            yield None

    body = (
        definition_factory(_SourceGraph(source))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse()
    )

    async def preflight() -> None:
        raise RuntimeError("request is no longer authorized")

    with pytest.raises(RuntimeError, match="no longer authorized"):
        await body.prepare(preflight=preflight)

    assert factory_calls == 0
    with pytest.raises(StopAsyncIteration):
        await anext(body)


@pytest.mark.asyncio
async def test_native_custom_mapper_filters_and_controls_payload_fields(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        yield {"type": "values", "ns": (), "data": {"value": 1}, "interrupts": ()}
        yield {"type": "values", "ns": (), "data": {"value": 2}, "interrupts": ()}

    async def mapper(part: NativeStreamPart) -> SsePayload | None:
        if part.data == {
            "state": {"value": 1},
            "messages": [],
            "interrupts": [],
        }:
            return None
        return SsePayload(data="line-1\nline-2", event="custom", retry=1500)

    async def event_id_resolver(part: NativeStreamPart) -> int:
        assert part.data == {
            "state": {"value": 2},
            "messages": [],
            "interrupts": [],
        }
        return 7

    body = (
        definition_factory(_SourceGraph(source))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse(
            mapper=mapper,
            event_id_resolver=event_id_resolver,
        )
    )

    assert [frame.decode("utf-8") async for frame in body] == [
        "id: 7\nevent: custom\nretry: 1500\ndata: line-1\ndata: line-2\n\n"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected_frame"),
    [
        ("line\n", "data: line\ndata: \n\n"),
        (
            "first\r\nsecond\rthird",
            "data: first\ndata: second\ndata: third\n\n",
        ),
        ("left\u2028right", "data: left\u2028right\n\n"),
    ],
)
async def test_custom_sse_data_preserves_exact_sse_line_semantics(
    data: str,
    expected_frame: str,
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}

    async def mapper(_part: NativeStreamPart) -> SsePayload:
        return SsePayload(data=data)

    body = (
        definition_factory(_SourceGraph(source))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse(mapper=mapper)
    )

    assert [frame.decode("utf-8") async for frame in body] == [expected_frame]


def test_sse_payload_rejects_boolean_retry_and_multiline_event_name() -> None:
    with pytest.raises(ValidationError):
        SsePayload(data="value", retry=True)
    with pytest.raises(ValidationError, match="line breaks"):
        SsePayload(data="value", event="bad\nevent")


@pytest.mark.asyncio
async def test_custom_mapper_must_be_async_and_return_sse_payload(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}

    def synchronous_mapper(_: NativeStreamPart) -> SsePayload:
        return SsePayload(data="invalid")

    body = (
        definition_factory(_SourceGraph(source))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse(mapper=cast(SseMapper[NativeStreamPart], synchronous_mapper))
    )

    with pytest.raises(TypeError, match="mapper must return an awaitable"):
        await anext(body)


@pytest.mark.asyncio
async def test_agui_mapper_receives_validated_event_objects(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        if False:
            yield None

    seen: list[str] = []

    async def mapper(event: BaseEvent) -> SsePayload:
        seen.append(event.type.value)
        return SsePayload(data=json.dumps({"kind": event.type.value}))

    body: SseBody[bytes] = (
        definition_factory(_SourceGraph(source))
        .open_agui_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse(mapper=mapper)
    )

    frames = [frame.decode("utf-8") async for frame in body]

    assert seen == ["RUN_STARTED", "RUN_FINISHED"]
    assert [
        json.loads(frame.removeprefix("data: "))["kind"] for frame in frames
    ] == seen


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id", [True, "bad\nid", "bad\x00id", object()])
async def test_event_id_resolver_rejects_unsafe_or_unsupported_ids(
    event_id: object,
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    parts = _CountingParts()

    async def event_id_resolver(
        _part: NativeStreamPart,
    ) -> str | int | None:
        return cast(str | int | None, event_id)

    body = (
        definition_factory(_SourceGraph(lambda: parts))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse(
            event_id_resolver=event_id_resolver,
        )
    )

    with pytest.raises(
        TypeError if event_id is True or not isinstance(event_id, str) else ValueError
    ):
        await anext(body)

    assert parts.closed.is_set()


@pytest.mark.asyncio
async def test_external_sse_close_cancels_an_active_pull_and_is_idempotent(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    parts = _BlockingParts()
    body = (
        definition_factory(_SourceGraph(lambda: parts))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse()
    )
    pull = asyncio.create_task(anext(body))
    await parts.pull_started.wait()

    await body.aclose()
    await body.aclose()

    assert (await asyncio.gather(pull, return_exceptions=True))[
        0
    ].__class__ is asyncio.CancelledError
    assert parts.closed.is_set()
    assert parts.close_calls == 1


@pytest.mark.asyncio
async def test_sse_body_repeated_cancellation_waits_for_source_cleanup() -> None:
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    closed = asyncio.Event()

    async def source() -> AsyncGenerator[str, None]:
        try:
            started.set()
            await asyncio.Future()
            yield "unreachable"
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            closed.set()

    iterator = source()
    body = SseBody(source_factory=lambda: iterator, close=iterator.aclose)
    pull = asyncio.create_task(anext(body))
    await started.wait()

    pull.cancel("first cancellation")
    await cleanup_started.wait()
    pull.cancel("repeated cancellation")
    await asyncio.sleep(0)
    try:
        assert not pull.done()
    finally:
        release_cleanup.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await pull
    assert captured.value.args == ("first cancellation",)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_sse_body_factory_failure_closes_and_terminates_the_body() -> None:
    close_calls = 0

    def source_factory() -> AsyncIterator[str]:
        raise LookupError("factory failed")

    async def close() -> None:
        nonlocal close_calls
        close_calls += 1

    body = SseBody(source_factory=source_factory, close=close)

    with pytest.raises(LookupError, match="factory failed"):
        await anext(body)

    assert close_calls == 1
    with pytest.raises(StopAsyncIteration):
        await anext(body)
    assert close_calls == 1


@pytest.mark.asyncio
async def test_sse_body_factory_failure_keeps_an_ordinary_close_failure_secondary() -> (
    None
):
    def source_factory() -> AsyncIterator[str]:
        raise LookupError("factory failed")

    async def close() -> None:
        raise ValueError("close failed")

    body = SseBody(source_factory=source_factory, close=close)

    with pytest.raises(LookupError, match="factory failed") as captured:
        await anext(body)

    assert any("ValueError: close failed" in note for note in captured.value.__notes__)


@pytest.mark.asyncio
async def test_sse_body_does_not_hide_cancellation_during_failure_cleanup() -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    def source_factory() -> AsyncIterator[str]:
        raise LookupError("factory failed")

    async def close() -> None:
        close_started.set()
        await release_close.wait()

    body = SseBody(source_factory=source_factory, close=close)
    owner = asyncio.create_task(anext(body))
    await close_started.wait()

    owner.cancel("caller cancelled")
    await asyncio.sleep(0)
    try:
        assert not owner.done()
    finally:
        release_close.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await owner
    assert captured.value.args == ("caller cancelled",)
    assert isinstance(captured.value.__cause__, LookupError)
    with pytest.raises(StopAsyncIteration):
        await anext(body)


@pytest.mark.asyncio
async def test_mapper_failure_closes_upstream_and_preserves_the_primary_error(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    parts = _CountingParts()

    async def mapper(_part: NativeStreamPart) -> SsePayload:
        raise LookupError("cannot map part")

    body = (
        definition_factory(_SourceGraph(lambda: parts))
        .open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )
        .to_sse(mapper=mapper)
    )

    with pytest.raises(LookupError, match="cannot map part"):
        await anext(body)

    assert parts.closed.is_set()
