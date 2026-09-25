"""Public stream closure owns accepted work, not the host's consumer task."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Literal

import pytest
from langchain.agents.middleware.types import InputAgentState
from test_stateless_sse import _SourceGraph

from tinkerfin import AgentRuntime, SseBody


class _StreamControl(BaseException):
    """Exercise process-control priority without stopping the test event loop."""


def _includes_exception(error: BaseException, target: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if value is target:
            return True
        if id(value) in seen:
            continue
        seen.add(id(value))
        pending.extend(
            item for item in (value.__cause__, value.__context__) if item is not None
        )
        if isinstance(value, BaseExceptionGroup):
            pending.extend(value.exceptions)
    return False


@pytest.mark.parametrize("kind", ["sse", "agui", "native"])
async def test_external_close_does_not_cancel_or_join_unrelated_consumer_work(
    kind: Literal["sse", "agui", "native"],
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_interrupted = asyncio.Event()
    closed = asyncio.Event()
    business_started = asyncio.Event()
    release_business = asyncio.Event()
    second_closer_started = asyncio.Event()

    async def source() -> AsyncGenerator[object, None]:
        try:
            if kind == "sse":
                yield b"data: ready\n\n"
            elif kind == "native":
                yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}
            started.set()
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cleanup_interrupted.set()
                raise
            closed.set()

    upstream = source()
    if kind == "sse":
        stream = SseBody(source_factory=lambda: upstream, close=upstream.aclose)
    else:
        runtime = definition_factory(_SourceGraph(lambda: upstream))
        stream = (
            runtime.open_agui_run(
                thread_id="thread", run_id="run", input=InputAgentState(messages=[])
            )
            if kind == "agui"
            else runtime.open_run(
                thread_id="thread", run_id="run", input=InputAgentState(messages=[])
            )
        )
    await anext(stream)

    async def consume() -> None:
        try:
            await anext(stream)
        except asyncio.CancelledError:
            business_started.set()
            await release_business.wait()

    async def close_again() -> None:
        second_closer_started.set()
        await stream.aclose()

    consumer = asyncio.create_task(consume())
    closers: list[asyncio.Task[None]] = []
    try:
        await started.wait()
        closers.append(asyncio.create_task(stream.aclose()))
        await cleanup_started.wait()
        closers.append(asyncio.create_task(close_again()))
        await second_closer_started.wait()
        assert not cleanup_interrupted.is_set()
        release_cleanup.set()
        await business_started.wait()
        assert consumer.cancelling() == 0
        await asyncio.gather(*closers)
        assert closed.is_set()
        assert not consumer.done()
    finally:
        release_cleanup.set()
        release_business.set()
        await asyncio.gather(consumer, *closers, return_exceptions=True)
        await stream.aclose()


async def test_idle_native_close_keeps_original_source_cleanup_failure(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    failure = OSError("source cleanup failed")

    async def source() -> AsyncGenerator[object, None]:
        try:
            yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}
        finally:
            raise failure

    stream = definition_factory(_SourceGraph(source)).open_run(
        thread_id="thread", run_id="run", input=InputAgentState(messages=[])
    )
    await anext(stream)
    with pytest.raises(asyncio.CancelledError) as captured:
        await stream.aclose()
    assert captured.value.__cause__ is failure
    await stream.aclose()


@pytest.mark.parametrize("kind", ["sse", "agui", "native"])
async def test_cancelled_first_consumer_does_not_open_an_unaccepted_source(
    kind: Literal["sse", "agui", "native"],
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    opened = False

    async def source() -> AsyncGenerator[object, None]:
        nonlocal opened
        opened = True
        yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}

    upstream = source()
    if kind == "sse":
        stream = SseBody(source_factory=lambda: upstream, close=upstream.aclose)
    else:
        runtime = definition_factory(_SourceGraph(lambda: upstream))
        stream = (
            runtime.open_agui_run(
                thread_id="thread", run_id="run", input=InputAgentState(messages=[])
            )
            if kind == "agui"
            else runtime.open_run(
                thread_id="thread", run_id="run", input=InputAgentState(messages=[])
            )
        )
    consumer = asyncio.create_task(anext(stream))
    consumer.cancel("consumer stopped before its first pull")
    try:
        with pytest.raises(asyncio.CancelledError, match="before its first pull"):
            await consumer
    finally:
        await stream.aclose()
    assert not opened


@pytest.mark.parametrize("control", [False, True])
async def test_sse_repeated_cancellation_retains_original_pull_and_close_failures(
    control: bool,
) -> None:
    failure = _StreamControl("source control") if control else OSError("source failed")
    close_failure = ValueError("cleanup failed")
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_interrupted = asyncio.Event()

    async def source() -> AsyncGenerator[bytes, None]:
        try:
            started.set()
            await asyncio.Event().wait()
            yield b"unreachable"
        finally:
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cleanup_interrupted.set()
                raise
            raise failure

    async def close() -> None:
        raise close_failure

    body = SseBody(source_factory=source, close=close)
    consumer = asyncio.create_task(anext(body))
    try:
        await started.wait()
        consumer.cancel("caller stopped")
        await cleanup_started.wait()
        consumer.cancel("caller stopped again")
        release_cleanup.set()
        with pytest.raises((asyncio.CancelledError, _StreamControl)) as captured:
            await consumer
        assert not cleanup_interrupted.is_set()
        assert _includes_exception(captured.value, failure)
        assert _includes_exception(captured.value, close_failure)
        if control:
            assert captured.value is failure
        else:
            assert captured.value.args == ("caller stopped",)
    finally:
        release_cleanup.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.gather(body.aclose(), return_exceptions=True)
