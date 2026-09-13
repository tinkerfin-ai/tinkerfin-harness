"""Public builder, lazy execution, delivery admission, and close contracts."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Literal, cast

import pytest
from ag_ui.core import BaseEvent
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Interrupt

from tinkerfin import (
    AgentRuntime,
    AgUiRunStream,
    NativeRunStream,
    RunObservationError,
    TinkerFin,
    TinkerFinLifecycleError,
)
from tinkerfin.coordination import RunCoordinationError
from tinkerfin_contracts import (
    RunIdentity,
    RunObservationSession,
    RunSourceContext,
    RunTerminalObservation,
)
from tinkerfin_messaging import MemoryBackend, Messaging


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


@pytest.mark.parametrize("stream_type", [NativeRunStream, AgUiRunStream])
def test_public_run_types_require_the_runtime_factory(
    stream_type: type[NativeRunStream] | type[AgUiRunStream],
) -> None:
    assert not inspect.signature(stream_type).parameters
    with pytest.raises(TypeError, match="use AgentRuntime"):
        stream_type()


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


@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_admission_failure_uses_the_runtime_error_contract_without_building(
    build_runtime: Callable[..., AgentRuntime[None]],
    protocol: Literal["native", "agui"],
) -> None:
    graph = _Graph()
    attempts = 0

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncGenerator[None]:
        nonlocal attempts
        del identity
        attempts += 1
        raise OSError("coordinator unavailable")
        yield

    runtime = build_runtime(
        graph, TinkerFin(run_coordinator=coordinate).with_namespace("company")
    )
    stream = _open(runtime, protocol)
    if protocol == "native":
        with pytest.raises(RunCoordinationError):
            await anext(stream)
    else:
        assert isinstance(stream, AgUiRunStream)
        events = [event async for event in stream]
        assert [event.type for event in events] == ["RUN_STARTED", "RUN_ERROR"]
        assert isinstance(stream.error, RunCoordinationError)
    await stream.aclose()
    assert graph.builds == 0 and attempts == 1


def test_builder_and_runtime_have_distinct_real_public_methods(
    build_runtime: Callable[..., AgentRuntime[None]],
) -> None:
    builder = TinkerFin()
    with pytest.raises(ValueError, match="with_namespace"):
        builder.build(model="provider:model")
    runtime = build_runtime(_Graph(), builder.with_namespace("customer"))
    assert type(runtime).__name__ == "AgentRuntime"
    assert {name for name in dir(builder) if not name.startswith("_")} == {
        "with_namespace",
        "with_observer",
        "with_attachments",
        "with_plan",
        "build",
    }
    assert {name for name in dir(runtime) if not name.startswith("_")} == {
        "agui",
        "namespace",
        "thread_identity",
        "run_identity",
        "ainvoke",
        "open_run",
        "open_agui_run",
    }
    assert runtime.namespace == "customer"
    assert runtime.run_identity("thread", "run").thread == runtime.thread_identity(
        "thread"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_build_open_and_unused_close_do_not_prepare_resources(
    build_runtime: Callable[..., AgentRuntime[None]],
    protocol: Literal["native", "agui"],
) -> None:
    graph = _Graph()
    terminals: list[RunTerminalObservation] = []

    async def record(terminal: RunTerminalObservation) -> None:
        terminals.append(terminal)

    before = asyncio.all_tasks()
    runtime = build_runtime(
        graph, TinkerFin().with_namespace("opaque").with_observer(on_terminal=record)
    )
    stream = _open(runtime, protocol)
    assert stream.messaging_identity.namespace == "opaque"
    await stream.aclose()
    await stream.aclose()
    assert asyncio.all_tasks() == before
    assert graph.builds == 0
    assert terminals == []


@pytest.mark.asyncio
async def test_preflight_and_first_pull_prepare_exactly_once(
    build_runtime: Callable[..., AgentRuntime[None]],
) -> None:
    graph = _Graph()
    stream = _open(build_runtime(graph), "native")
    results = await asyncio.gather(
        stream.messaging_owner_preflight(),
        stream.messaging_owner_preflight(),
        anext(stream),
    )
    assert results[-1] == {
        "type": "values",
        "ns": (),
        "data": {"answer": 42},
        "interrupts": (),
    }
    assert graph.builds == 1
    await stream.aclose()
    assert graph.closed.is_set()


@pytest.mark.asyncio
async def test_messaging_replay_does_not_prepare_a_candidate(
    build_runtime: Callable[..., AgentRuntime[None]],
) -> None:
    graph = _Graph()
    runtime = build_runtime(graph)
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="runtime-api")
        first = await channel.open_sse(_open(runtime, "agui"))
        expected = [frame async for frame in first]
        candidate = _open(runtime, "agui")
        replay = await channel.open_sse(candidate, after=0)
        assert [frame async for frame in replay] == expected
    assert graph.builds == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_external_close_joins_the_active_consumer(
    build_runtime: Callable[..., AgentRuntime[None]],
    protocol: Literal["native", "agui"],
) -> None:
    graph = _Graph(blocked=True)
    stream = _open(build_runtime(graph), protocol)
    if protocol == "agui":
        await anext(stream)
    consumer = asyncio.create_task(anext(stream))
    await graph.started.wait()
    await stream.aclose()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert graph.closed.is_set()
    await stream.aclose()


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
@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_native_callback_can_request_close_without_losing_current_item(
    build_runtime: Callable[..., AgentRuntime[None]],
    protocol: Literal["native", "agui"],
) -> None:
    graph = _Graph()
    runtime = build_runtime(graph)
    observed: list[Mapping[str, object]] = []

    async def close_on_part(part: Mapping[str, object]) -> None:
        observed.append(part)
        await asyncio.create_task(stream.aclose())

    stream = (
        runtime.open_run(
            thread_id="thread",
            run_id="run",
            input={"messages": []},
            on_native_part=close_on_part,
        )
        if protocol == "native"
        else runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            input={"messages": []},
            on_native_part=close_on_part,
        )
    )
    delivered = [part async for part in stream]
    assert len(observed) == 1
    assert len(delivered) == (1 if protocol == "native" else 2)
    if protocol == "agui":
        assert [
            event.type.value for event in delivered if isinstance(event, BaseEvent)
        ] == ["RUN_STARTED", "STATE_SNAPSHOT"]
    assert stream.error is None
    assert graph.closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_callback", [False, True])
async def test_terminal_callback_runs_once_and_can_request_close(
    build_runtime: Callable[..., AgentRuntime[None]], fail_callback: bool
) -> None:
    graph = _Graph()
    terminals: list[RunTerminalObservation] = []

    async def record(terminal: RunTerminalObservation) -> None:
        terminals.append(terminal)
        await stream.aclose()
        if fail_callback:
            raise ValueError("callback failed")

    runtime = build_runtime(
        graph, TinkerFin().with_namespace("business").with_observer(on_terminal=record)
    )
    stream = _open(runtime, "native")
    if fail_callback:
        with pytest.raises(RunObservationError):
            [part async for part in stream]
    else:
        assert len([part async for part in stream]) == 1
    assert [terminal.outcome for terminal in terminals] == ["succeeded"]
    assert graph.closed.is_set()


def test_input_branches_and_execution_options_are_validated_before_preparation(
    build_runtime: Callable[..., AgentRuntime[None]],
) -> None:
    graph = _Graph()
    runtime = build_runtime(graph)
    with pytest.raises(ValueError, match="exactly one"):
        cast(Callable[..., object], runtime.open_agui_run)(
            thread_id="thread", run_id="run"
        )
    with pytest.raises(ValueError, match="exactly one"):
        cast(Callable[..., object], runtime.open_agui_run)(
            thread_id="thread", run_id="run", messages=[], input={"messages": []}
        )
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        cast(Callable[..., object], runtime.open_run)(
            thread_id="thread", run_id="run", input={"messages": []}, agent=runtime
        )
    assert graph.builds == 0


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
@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_delayed_callback_child_closes_resources_before_returning(
    build_runtime: Callable[..., AgentRuntime[None]],
    protocol: Literal["native", "agui"],
) -> None:
    graph = _Graph()
    runtime = build_runtime(graph)
    release = asyncio.Event()
    child: asyncio.Task[None] | None = None

    async def delayed_close() -> None:
        await release.wait()
        await stream.aclose()
        assert graph.closed.is_set()

    async def observe(part: Mapping[str, object]) -> None:
        nonlocal child
        child = asyncio.create_task(delayed_close())

    stream = (
        runtime.open_run(
            thread_id="thread",
            run_id="run",
            input={"messages": []},
            on_native_part=observe,
        )
        if protocol == "native"
        else runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            input={"messages": []},
            on_native_part=observe,
        )
    )
    if protocol == "agui":
        await anext(stream)
    await anext(stream)
    release.set()
    assert child is not None
    await child


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


class _WorkerControl(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, RuntimeError("worker evidence"), _WorkerControl("worker control")]
)
async def test_cancelled_preparation_joins_sync_worker_and_keeps_its_failure(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException | None
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    released = threading.Event()
    finished = threading.Event()

    def create(*args: object, **kwargs: object) -> _Graph:
        loop.call_soon_threadsafe(entered.set)
        released.wait()
        finished.set()
        if failure is not None:
            raise failure
        return _Graph()

    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", create)
    stream = _open(
        TinkerFin().with_namespace("chosen").build(model="provider:model"), "native"
    )
    consumer = asyncio.create_task(anext(stream))
    try:
        await entered.wait()
        consumer.cancel("caller evidence")
    finally:
        released.set()
    if isinstance(failure, _WorkerControl):
        with pytest.raises(_WorkerControl) as caught:
            await consumer
        assert caught.value is failure
    else:
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await consumer
        if failure is not None:
            assert any("worker evidence" in note for note in cancelled.value.__notes__)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_build_copies_configuration_containers_without_copying_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tinkerfin._agent_spec import AgentSpec

    received: list[AgentSpec[None]] = []
    graph = _Graph()

    def create(spec: AgentSpec[None], **kwargs: object) -> _Graph:
        received.append(spec)
        return graph

    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", create)
    tools: list[Callable[..., object]] = []
    builder = TinkerFin().with_namespace("original")
    runtime = builder.build(model="provider:model", tools=tools)
    tools.append(lambda: None)
    assert await runtime.ainvoke(
        thread_id="thread", run_id="run", input={"messages": []}
    ) == {"answer": 42}
    assert received[0].tools == ()
    derived = builder.with_namespace("other").build(model="provider:model")
    assert runtime.namespace == "original"
    assert derived.namespace == "other"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "interrupted", "failed", "cancelled"])
async def test_terminal_callback_preserves_the_selected_native_outcome(
    build_runtime: Callable[..., AgentRuntime[None]],
    outcome: str,
) -> None:
    terminals: list[RunTerminalObservation] = []

    async def record(value: RunTerminalObservation) -> None:
        terminals.append(value)

    graph = _Graph(
        blocked=outcome == "cancelled",
        failure=RuntimeError("execution failed") if outcome == "failed" else None,
        interrupts=(Interrupt(value={"approval": True}, id="pending"),)
        if outcome == "interrupted"
        else (),
    )
    runtime = build_runtime(
        graph, TinkerFin().with_namespace("customer").with_observer(on_terminal=record)
    )
    stream = runtime.open_run(thread_id="thread", run_id="run", input={"messages": []})
    if outcome == "cancelled":
        consumer = asyncio.create_task(anext(stream))
        await graph.started.wait()
        await stream.aclose()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    elif outcome == "failed":
        with pytest.raises(RuntimeError, match="execution failed"):
            [part async for part in stream]
    else:
        assert len([part async for part in stream]) == 1
    await stream.aclose()
    assert [item.outcome for item in terminals] == [outcome]
    assert graph.closed.is_set()


@pytest.mark.parametrize("exit_mode", ["consumer_cancel", "external_close"])
async def test_initialization_failure_survives_cancel_during_error_stream_startup(
    definition_factory: Callable[..., AgentRuntime[None]], exit_mode: str
) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()
    original = RuntimeError("initialization failed before observation opened")

    class BlockingObserver:
        async def open_run(self, context: RunSourceContext) -> RunObservationSession:
            del context
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            raise AssertionError("observer opening must be cancelled")

    runtime = definition_factory(
        original,
        tinkerfin=TinkerFin().with_namespace("test").with_observer(BlockingObserver()),
    )
    stream = runtime.open_agui_run(
        thread_id="thread", run_id="run", input={"messages": []}
    )
    consumer = asyncio.create_task(anext(stream))
    public_errors: list[BaseException] = []
    try:
        await entered.wait()
        if exit_mode == "consumer_cancel":
            consumer.cancel()
        else:
            try:
                await stream.aclose()
            except BaseException as error:  # noqa: BLE001 - inspect public cancellation evidence
                public_errors.append(error)
        try:
            await consumer
        except BaseException as error:  # noqa: BLE001 - inspect public cancellation evidence
            public_errors.append(error)
    finally:
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        try:
            await stream.aclose()
        except BaseException as error:  # noqa: BLE001 - inspect public cleanup evidence
            public_errors.append(error)
    assert closed.is_set()
    if stream.error is not None:
        public_errors.append(stream.error)
    seen: set[int] = set()
    evidence: list[str] = []
    while public_errors:
        error = public_errors.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        evidence.extend([str(error), *getattr(error, "__notes__", ())])
        public_errors.extend(
            item for item in (error.__cause__, error.__context__) if item is not None
        )
    assert any(str(original) in text for text in evidence)
