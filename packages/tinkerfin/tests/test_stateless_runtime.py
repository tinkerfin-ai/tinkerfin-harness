from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import cast

import pytest
from langchain.agents.middleware.types import InputAgentState
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import RunControl
from langgraph.types import StreamMode

from tinkerfin import AgentRuntime, NativeRunStream, RunIdentity, TinkerFin


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")


def _graph_input() -> InputAgentState:
    return InputAgentState(messages=[])


class RecordingGraph:
    def __init__(
        self,
        parts: list[object],
        *,
        pull_gate: asyncio.Event | None = None,
        close_gate: asyncio.Event | None = None,
    ) -> None:
        self.parts = parts
        self.pull_gate = pull_gate
        self.close_gate = close_gate
        self.calls: list[dict[str, object]] = []
        self.started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.closed = asyncio.Event()

    async def astream(
        self,
        input: object,
        config: object | None = None,
        *,
        context: object | None = None,
        stream_mode: StreamMode | tuple[StreamMode, ...] | None = None,
        print_mode: StreamMode | tuple[StreamMode, ...] = (),
        output_keys: str | tuple[str, ...] | None = None,
        interrupt_before: str | tuple[str, ...] | None = None,
        interrupt_after: str | tuple[str, ...] | None = None,
        durability: str | None = None,
        control: object | None = None,
        subgraphs: bool = False,
        debug: bool | None = None,
        version: str = "v1",
        **kwargs: object,
    ) -> AsyncIterator[object]:
        self.calls.append(
            {
                "input": input,
                "config": config,
                "context": context,
                "stream_mode": stream_mode,
                "print_mode": print_mode,
                "output_keys": output_keys,
                "interrupt_before": interrupt_before,
                "interrupt_after": interrupt_after,
                "durability": durability,
                "control": control,
                "subgraphs": subgraphs,
                "debug": debug,
                "version": version,
                "kwargs": kwargs,
            }
        )
        self.started.set()
        try:
            if self.pull_gate is not None:
                await self.pull_gate.wait()
            for part in self.parts:
                yield part
        finally:
            self.close_started.set()
            if self.close_gate is not None:
                await self.close_gate.wait()
            self.closed.set()


@pytest.mark.asyncio
async def test_run_stream_forwards_native_arguments_and_observes_before_delivery(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    graph = RecordingGraph([{"type": "values", "ns": (), "data": {"n": 1}}])
    order: list[tuple[str, object]] = []

    async def on_part(part: object) -> None:
        order.append(("observed", part))

    config: RunnableConfig = {"configurable": {"thread_id": "thread-1"}}
    control = RunControl()
    runtime = definition_factory(graph)
    stream = runtime.open_run(
        thread_id=_identity().thread_id,
        run_id=_identity().run_id,
        on_native_part=on_part,
        input=_graph_input(),
        config=config,
        stream_mode=("messages", "tasks", "values"),
        print_mode="debug",
        interrupt_before=("agent",),
        interrupt_after=("tools",),
        durability="sync",
        control=control,
        debug=False,
    )

    assert isinstance(stream, NativeRunStream)
    part = await anext(stream)
    order.append(("delivered", part))
    await stream.aclose()

    assert order == [("observed", part), ("delivered", part)]
    assert len(graph.calls) == 1
    forwarded = dict(graph.calls[0])
    forwarded_config = cast(RunnableConfig, forwarded.pop("config"))
    assert forwarded_config.get("configurable", {}).get("thread_id") == "thread-1"
    assert [forwarded] == [
        {
            "input": {"messages": []},
            "context": None,
            "stream_mode": ("messages", "tasks", "values"),
            "print_mode": "debug",
            "output_keys": None,
            "interrupt_before": ("agent",),
            "interrupt_after": ("tools",),
            "durability": "sync",
            "control": control,
            "subgraphs": True,
            "debug": False,
            "version": "v2",
            "kwargs": {},
        }
    ]
    assert config == {"configurable": {"thread_id": "thread-1"}}
    assert graph.closed.is_set()


@pytest.mark.asyncio
async def test_closing_stream_cancels_an_active_graph_pull_before_releasing(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    released = asyncio.Event()

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncIterator[None]:
        del identity
        try:
            yield
        finally:
            released.set()

    graph = RecordingGraph(
        [{"type": "values", "ns": (), "data": {}}],
        pull_gate=asyncio.Event(),
    )
    stream = definition_factory(
        graph, tinkerfin=TinkerFin(run_coordinator=coordinate).with_namespace("test")
    ).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_graph_input()
    )
    pull = asyncio.create_task(anext(stream))
    await graph.started.wait()

    try:
        await stream.aclose()
    finally:
        if not pull.done():
            pull.cancel()
        outcome = (await asyncio.gather(pull, return_exceptions=True))[0]

    assert isinstance(outcome, asyncio.CancelledError)
    assert graph.closed.is_set()
    assert released.is_set()


@pytest.mark.asyncio
async def test_upstream_failure_is_not_replaced_by_source_close_failure(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    class FailingSource:
        def __aiter__(self) -> FailingSource:
            return self

        async def __anext__(self) -> object:
            raise ValueError("graph failed")

        async def aclose(self) -> None:
            raise RuntimeError("source close failed")

    class FailingGraph:
        def astream(
            self,
            *_args: object,
            **_options: object,
        ) -> AsyncIterator[object]:
            return FailingSource()

    stream = definition_factory(FailingGraph()).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_graph_input()
    )

    with pytest.raises(ValueError, match="graph failed"):
        await anext(stream)
