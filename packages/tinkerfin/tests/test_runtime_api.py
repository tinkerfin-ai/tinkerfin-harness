"""Public builder, lazy execution, delivery admission, and close contracts."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Literal

import pytest
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Interrupt

from tinkerfin import (
    AgentRuntime,
    AgUiRunStream,
    NativeRunStream,
    TinkerFin,
    TinkerFinLifecycleError,
)
from tinkerfin_contracts import (
    RunIdentity,
)


class _Graph:
    def __init__(
        self,
        *,
        blocked: bool = False,
        failure: Exception | None = None,
        interrupts: tuple[Interrupt, ...] = (),
    ) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.release = asyncio.Event()
        self.failure = failure
        self.interrupts = interrupts
        self.builds = 0
        if not blocked:
            self.release.set()

    async def astream(
        self, *args: object, **kwargs: object
    ) -> AsyncIterator[Mapping[str, object]]:
        self.started.set()
        try:
            await self.release.wait()
            if self.failure is not None:
                raise self.failure
            yield {
                "type": "values",
                "ns": (),
                "data": {"answer": 42},
                "interrupts": self.interrupts,
            }
        finally:
            self.closed.set()


setattr(_Graph.astream, "__signature__", inspect.signature(CompiledStateGraph.astream))


@pytest.fixture
def build_runtime(monkeypatch: pytest.MonkeyPatch) -> Callable[..., AgentRuntime[None]]:
    def build(graph: _Graph, builder: TinkerFin | None = None) -> AgentRuntime[None]:
        def create(*args: object, **kwargs: object) -> _Graph:
            graph.builds += 1
            return graph

        monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", create)
        return (builder or TinkerFin().with_namespace("chosen-by-host")).build(
            model="provider:model"
        )

    return build


def _open(
    runtime: AgentRuntime[None], protocol: Literal["native", "agui"]
) -> NativeRunStream | AgUiRunStream:
    if protocol == "native":
        return runtime.open_run(
            thread_id="thread", run_id="run", input={"messages": []}
        )
    return runtime.open_agui_run(
        thread_id="thread", run_id="run", input={"messages": []}
    )


@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_coordination_precedes_graph_preparation_and_lasts_until_close(
    build_runtime: Callable[..., AgentRuntime[None]],
    protocol: Literal["native", "agui"],
) -> None:
    graph = _Graph()
    waiting, admitted, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncGenerator[None]:
        assert identity.namespace == "company"
        waiting.set()
        await admitted.wait()
        try:
            yield
        finally:
            exited.set()

    runtime = build_runtime(
        graph, TinkerFin(run_coordinator=coordinate).with_namespace("company")
    )
    stream = _open(runtime, protocol)
    preflight = asyncio.create_task(stream.messaging_owner_preflight())
    await waiting.wait()
    assert graph.builds == 0 and not exited.is_set()
    admitted.set()
    await preflight
    assert graph.builds == 0 and not exited.is_set()
    if protocol == "agui":
        assert isinstance(stream, AgUiRunStream)
        assert (await anext(stream)).type == "RUN_STARTED"
        assert graph.builds == 0
    await anext(stream)
    assert graph.builds == 1 and not exited.is_set()
    await stream.aclose()
    await stream.aclose()
    assert exited.is_set()


@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_closing_while_waiting_for_admission_does_not_build_a_graph(
    build_runtime: Callable[..., AgentRuntime[None]],
    protocol: Literal["native", "agui"],
) -> None:
    graph = _Graph()
    waiting, exited = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncGenerator[None]:
        del identity
        waiting.set()
        try:
            await asyncio.Event().wait()
            yield
        finally:
            exited.set()

    runtime = build_runtime(
        graph, TinkerFin(run_coordinator=coordinate).with_namespace("company")
    )
    stream = _open(runtime, protocol)
    preflight = asyncio.create_task(stream.messaging_owner_preflight())
    await waiting.wait()
    await stream.aclose()
    with pytest.raises(asyncio.CancelledError):
        await preflight
    assert graph.builds == 0 and exited.is_set()


@pytest.mark.asyncio
async def test_concurrent_agui_abort_delivers_one_terminal(
    build_runtime: Callable[..., AgentRuntime[None]],
) -> None:
    graph = _Graph(blocked=True)
    stream = build_runtime(graph).open_agui_run(
        thread_id="thread", run_id="run", input={"messages": []}
    )
    assert (await anext(stream)).type.value == "RUN_STARTED"
    consumer = asyncio.create_task(anext(stream))
    await graph.started.wait()
    tails = await asyncio.gather(stream.abort(), stream.abort())
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert [event.type.value for tail in tails for event in tail] == ["RUN_ERROR"]
    assert await stream.abort() == []
    assert graph.closed.is_set()


@pytest.mark.asyncio
async def test_lazy_stream_has_one_consumer_claim(
    build_runtime: Callable[..., AgentRuntime[None]],
) -> None:
    stream = _open(build_runtime(_Graph()), "native")
    aiter(stream)
    with pytest.raises(TinkerFinLifecycleError, match="consumed once"):
        aiter(stream)
    await stream.aclose()


@pytest.mark.asyncio
async def test_cancelled_closer_and_repeated_close_preserve_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    closed = asyncio.Event()

    class Graph:
        async def astream(
            self, *args: object, **kwargs: object
        ) -> AsyncIterator[Mapping[str, object]]:
            started.set()
            try:
                await asyncio.Event().wait()
                yield {}
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                closed.set()

    setattr(
        Graph.astream, "__signature__", inspect.signature(CompiledStateGraph.astream)
    )
    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph",
        lambda *args, **kwargs: Graph(),
    )
    runtime = TinkerFin().with_namespace("chosen").build(model="provider:model")
    stream = _open(runtime, "native")
    consumer = asyncio.create_task(anext(stream))
    await started.wait()
    closer = asyncio.create_task(stream.aclose())
    await cleanup_started.wait()
    repeated = asyncio.create_task(stream.aclose())
    closer.cancel()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    with pytest.raises(asyncio.CancelledError):
        await closer
    await repeated
    assert closed.is_set()
