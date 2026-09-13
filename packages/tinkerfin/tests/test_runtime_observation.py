"""Runtime Observation ordering, failure propagation, and terminal ownership."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from ag_ui.core import RunErrorEvent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import AIMessageChunk
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.types import Interrupt, StreamMode

from tinkerfin import (
    AgentRuntime,
    RunIdentity,
    RunObservationError,
    TinkerFin,
    TinkerFinStreamProtocolError,
)
from tinkerfin._observation import RuntimeObservationHub
from tinkerfin.deep_agent import create_graph
from tinkerfin.native_driver import DeepAgentsV2StreamDriver
from tinkerfin.runtime_profile import (
    DeepAgentsRuntimeProfile,
    DeepAgentsV2RuntimeProfile,
)
from tinkerfin_contracts import (
    NativeStateObservation,
    ObservationBoundary,
    RunObservationSession,
    RunResumeSummary,
    RunSourceContext,
    RunTerminalObservation,
    RuntimeObservation,
)
from tinkerfin_native_stream import (
    NativeStreamFrame,
    NativeStreamPart,
    NativeValuesStreamPart,
)

_ProfileCheckpointSaver = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)


class _Session:
    def __init__(
        self,
        *,
        order: list[str] | None = None,
        fail_kind: str | None = None,
    ) -> None:
        self.observations: list[RuntimeObservation] = []
        self.boundaries: list[ObservationBoundary] = []
        self.closed = 0
        self.failure: asyncio.Future[BaseException] | None = None
        self.order = order
        self.fail_kind = fail_kind

    async def observe(self, observation: RuntimeObservation) -> None:
        self.observations.append(observation)
        if self.order is not None:
            self.order.append(f"trace:{observation.kind}")
        if observation.kind == self.fail_kind:
            raise RuntimeError("observer failed")

    async def force(self, boundary: ObservationBoundary) -> None:
        self.boundaries.append(boundary)

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        if self.failure is None:
            self.failure = asyncio.get_running_loop().create_future()
        return self.failure

    async def aclose(self) -> None:
        self.closed += 1


class _Observer:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.contexts: list[RunSourceContext] = []

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        self.contexts.append(context)
        return self.session


class _ControlSession(_Session):
    def __init__(self, *, phase: str, error: BaseException) -> None:
        super().__init__()
        self.phase = phase
        self.error = error

    async def observe(self, observation: RuntimeObservation) -> None:
        if self.phase == "observe" and observation.kind == "run.started":
            raise self.error
        await super().observe(observation)

    async def force(self, boundary: ObservationBoundary) -> None:
        if self.phase == "force":
            raise self.error
        await super().force(boundary)

    async def aclose(self) -> None:
        self.closed += 1
        if self.phase == "close":
            raise self.error


class _OpeningControlObserver:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        del context
        raise self.error


class _BlockingClosedSession(_Session):
    def __init__(self) -> None:
        super().__init__()
        self.closed_observation_started = asyncio.Event()
        self._never = asyncio.Event()

    async def observe(self, observation: RuntimeObservation) -> None:
        await super().observe(observation)
        if observation.kind == "run.closed":
            self.closed_observation_started.set()
            await self._never.wait()


class _ConcurrentAccessSession(_Session):
    def __init__(self) -> None:
        super().__init__()
        self.active_calls = 0
        self.max_active_calls = 0

    async def _enter(self, operation: Callable[[], None]) -> None:
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0)
            operation()
        finally:
            self.active_calls -= 1

    async def observe(self, observation: RuntimeObservation) -> None:
        await self._enter(lambda: self.observations.append(observation))

    async def force(self, boundary: ObservationBoundary) -> None:
        await self._enter(lambda: self.boundaries.append(boundary))


class _Graph:
    def __init__(
        self,
        parts: list[object],
        *,
        pull_gate: asyncio.Event | None = None,
    ) -> None:
        self.parts = parts
        self.pull_gate = pull_gate
        self.started = asyncio.Event()
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
        del (
            input,
            config,
            context,
            stream_mode,
            print_mode,
            output_keys,
            interrupt_before,
            interrupt_after,
            durability,
            control,
            subgraphs,
            debug,
            version,
            kwargs,
        )
        self.started.set()
        try:
            if self.pull_gate is not None:
                await self.pull_gate.wait()
            for part in self.parts:
                yield part
        finally:
            self.closed.set()


def _identity() -> RunIdentity:
    return RunIdentity(
        namespace="test", thread_id="thread-observed", run_id="run-observed"
    )


def _input() -> InputAgentState:
    return InputAgentState(messages=[])


def _source_context() -> RunSourceContext:
    return RunSourceContext(
        identity=_identity(),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )


def _definition(
    definition_factory: Callable[..., AgentRuntime[None]],
    graph: object,
    observer: _Observer,
) -> AgentRuntime[None]:
    return definition_factory(
        graph, tinkerfin=TinkerFin().with_namespace("test").with_observer(observer)
    )


async def test_observe_is_immutable_and_plan_preserves_registration(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    base = TinkerFin().with_namespace("test")
    session = _Session()
    observer = _Observer(session)
    observed = base.with_observer(observer)
    planned = observed.with_plan(enabled=False)

    plain_stream = definition_factory(_Graph([]), tinkerfin=base).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )
    observed_stream = definition_factory(_Graph([]), tinkerfin=planned).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    assert [part async for part in plain_stream] == []
    assert [part async for part in observed_stream] == []
    assert len(observer.contexts) == 1
    with pytest.raises(ValueError, match="same RuntimeObserver"):
        observed.with_observer(observer)


async def test_native_observation_precedes_on_part_and_delivery(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    order: list[str] = []
    session = _Session(order=order)
    observer = _Observer(session)
    part = {
        "type": "values",
        "ns": (),
        "data": {"messages": [], "todos": []},
        "interrupts": (),
    }

    async def on_part(_part: object) -> None:
        order.append("hook")

    stream = _definition(definition_factory, _Graph([part]), observer).open_run(
        thread_id=_identity().thread_id,
        run_id=_identity().run_id,
        on_native_part=on_part,
        input=_input(),
    )
    delivered = [value async for value in stream]
    order.append("delivered")

    assert delivered == [part]
    assert order == [
        "trace:run.started",
        "trace:run.input",
        "trace:native.state",
        "hook",
        "trace:run.terminal",
        "trace:run.closed",
        "delivered",
    ]
    assert cast(object, session.observations[-2]).outcome == "succeeded"  # type: ignore[attr-defined]
    assert session.boundaries == [
        ObservationBoundary.TERMINAL,
        ObservationBoundary.CLOSE,
    ]
    assert session.closed == 1


async def test_malformed_part_never_reaches_trace_native_or_on_part(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    hook_called = False

    async def on_part(_part: object) -> None:
        nonlocal hook_called
        hook_called = True

    malformed = {
        "type": "messages",
        "ns": (),
        "data": (AIMessageChunk(id="message-1", content="partial"),),
    }
    stream = _definition(definition_factory, _Graph([malformed]), observer).open_run(
        thread_id=_identity().thread_id,
        run_id=_identity().run_id,
        on_native_part=on_part,
        input=_input(),
    )

    with pytest.raises(TinkerFinStreamProtocolError):
        await anext(stream)

    assert hook_called is False
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
        "run.terminal",
        "run.closed",
    ]
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "failed"


async def test_root_interrupt_selects_interrupted_terminal(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    part = {
        "type": "values",
        "ns": (),
        "data": {"messages": []},
        "interrupts": (Interrupt(value={"request": "approval"}, id="interrupt-1"),),
    }
    stream = _definition(definition_factory, _Graph([part]), observer).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    assert [value async for value in stream] == [part]
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "interrupted"
    assert terminal.interrupt_ids == ("interrupt-1",)
    assert session.boundaries[-2:] == [
        ObservationBoundary.INTERRUPT,
        ObservationBoundary.CLOSE,
    ]


async def test_root_interrupt_survives_trailing_message_part(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    interrupted = {
        "type": "values",
        "ns": (),
        "data": {"messages": []},
        "interrupts": (Interrupt(value={"request": "approval"}, id="interrupt-1"),),
    }
    trailing = {
        "type": "messages",
        "ns": (),
        "data": (
            AIMessageChunk(id="message-after-interrupt", content=""),
            {"langgraph_node": "model"},
        ),
    }
    stream = _definition(
        definition_factory, _Graph([interrupted, trailing]), observer
    ).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    assert [value async for value in stream] == [interrupted, trailing]
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "interrupted"
    assert terminal.interrupt_ids == ("interrupt-1",)


async def test_empty_root_values_preserves_an_observed_interrupt(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    interrupted = {
        "type": "values",
        "ns": (),
        "data": {"messages": []},
        "interrupts": (Interrupt(value={"request": "approval"}, id="interrupt-1"),),
    }
    continued = {
        "type": "values",
        "ns": (),
        "data": {"messages": []},
        "interrupts": (),
    }
    stream = _definition(
        definition_factory, _Graph([interrupted, continued]), observer
    ).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    assert [value async for value in stream] == [interrupted, continued]
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "interrupted"
    assert terminal.interrupt_ids == ("interrupt-1",)


async def test_explicit_close_selects_cancelled_terminal(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    part = {"type": "values", "ns": (), "data": {"messages": []}}
    stream = _definition(definition_factory, _Graph([part, part]), observer).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    assert await anext(stream) == part
    await stream.aclose()

    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "cancelled"


async def test_cancelled_resume_selects_abandoned_terminal(definition_factory) -> None:
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import interrupt

    from tinkerfin import AgUiResumeRequest

    executed: list[object] = []

    def review(state: MessagesState) -> dict[str, list[AIMessage]]:
        executed.append(
            interrupt(
                {
                    "action_requests": [
                        {"name": "change", "args": {}, "description": "Change a value"}
                    ],
                    "review_configs": [
                        {
                            "action_name": "change",
                            "allowed_decisions": ["approve", "reject"],
                        }
                    ],
                }
            )
        )
        return {"messages": [AIMessage(content="done")]}

    saver = InMemorySaver()

    from tinkerfin._checkpoint import NamespaceCheckpointer

    workflow = StateGraph(MessagesState)
    workflow.add_node("review", review)
    workflow.add_edge(START, "review")
    workflow.add_edge("review", END)
    graph = workflow.compile(checkpointer=NamespaceCheckpointer(saver, "test"))
    builder = TinkerFin(checkpointer=saver).with_namespace("test")
    first = definition_factory(graph, tinkerfin=builder)
    events = [
        event
        async for event in first.open_agui_run(
            thread_id="thread-observed",
            run_id="review",
            input={
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "tool-change",
                                "name": "change",
                                "args": {},
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
        )
    ]
    outcome = events[-1].outcome
    assert outcome.type == "interrupt"
    pending = outcome.interrupts[0]
    session = _Session()
    observer = _Observer(session)
    terminals: list[RunTerminalObservation] = []

    async def record_terminal(value: RunTerminalObservation) -> None:
        terminals.append(value)

    runtime = definition_factory(
        graph,
        tinkerfin=builder.with_observer(observer).with_observer(
            on_terminal=record_terminal
        ),
    )
    request = AgUiResumeRequest.model_validate(
        {"entries": [{"interruptId": pending.id, "status": "cancelled"}]}
    )
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id="thread-observed", run_id="abandon", resume=request
        )
    ]
    assert events[-1].type.value == "RUN_ERROR"
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "abandoned"
    assert [value.outcome for value in terminals] == ["abandoned"]
    assert observer.contexts[0].resume == (
        RunResumeSummary(interrupt_id=pending.id, status="cancelled"),
    )
    assert executed == []


async def test_agui_starts_observation_before_delivering_run_started(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    stream = _definition(definition_factory, _Graph([]), observer).open_agui_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    started = await anext(stream)

    assert started.type.value == "RUN_STARTED"
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
    ]
    await stream.aclose()
    assert [item.kind for item in session.observations[-2:]] == [
        "run.terminal",
        "run.closed",
    ]
    assert session.observations[-2].outcome == "cancelled"  # type: ignore[attr-defined]


async def test_failing_observer_notifies_healthy_observer_then_fails_run(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    failed = _Session(fail_kind="native.state")
    healthy = _Session()
    failed_observer = _Observer(failed)
    healthy_observer = _Observer(healthy)
    tinkerfin = (
        TinkerFin()
        .with_namespace("test")
        .with_observer(failed_observer)
        .with_observer(healthy_observer)
    )
    part = {"type": "values", "ns": (), "data": {"messages": []}}
    stream = definition_factory(_Graph([part]), tinkerfin=tinkerfin).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    with pytest.raises(RunObservationError) as captured:
        await anext(stream)

    assert captured.value.__cause__ is not None
    assert [item.kind for item in healthy.observations] == [
        "run.started",
        "run.input",
        "native.state",
        "run.observer_failed",
        "run.terminal",
        "run.closed",
    ]
    assert healthy.observations[-2].outcome == "failed"  # type: ignore[attr-defined]
    assert failed.closed == healthy.closed == 1


async def test_terminal_observer_failure_does_not_rewrite_the_selected_outcome(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    failed = _Session(fail_kind="run.terminal")
    healthy = _Session()
    tinkerfin = (
        TinkerFin()
        .with_namespace("test")
        .with_observer(_Observer(failed))
        .with_observer(_Observer(healthy))
    )
    stream = definition_factory(_Graph([]), tinkerfin=tinkerfin).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    with pytest.raises(RunObservationError):
        _ = [part async for part in stream]

    assert [item.kind for item in healthy.observations] == [
        "run.started",
        "run.input",
        "run.terminal",
        "run.observer_failed",
        "run.closed",
    ]
    terminals = [item for item in healthy.observations if item.kind == "run.terminal"]
    assert len(terminals) == 1
    assert terminals[0].outcome == "succeeded"  # type: ignore[attr-defined]
    assert healthy.observations[-1].outcome == "succeeded"  # type: ignore[attr-defined]
    assert failed.closed == healthy.closed == 1


async def test_background_observer_failure_cancels_blocked_graph_pull(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    gate = asyncio.Event()
    graph = _Graph([], pull_gate=gate)
    session = _Session()
    observer = _Observer(session)
    stream = _definition(definition_factory, graph, observer).open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )
    pull = asyncio.create_task(anext(stream))
    await graph.started.wait()
    assert session.failure is not None

    session.failure.set_result(RuntimeError("background writer failed"))
    with pytest.raises(RunObservationError):
        await pull

    assert graph.closed.is_set()
    assert session.closed == 1


async def test_observation_close_preserves_caller_cancellation_and_settles_sessions() -> (
    None
):
    session = _BlockingClosedSession()
    observer = _Observer(session)
    context = RunSourceContext(
        identity=_identity(),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )
    hub = RuntimeObservationHub(context=context, observers=(observer,))
    await hub.start()
    await hub.terminal("cancelled", code="cancelled")
    closing = asyncio.create_task(hub.close())
    await session.closed_observation_started.wait()

    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert closing.cancelled() is True
    assert session.closed == 1


async def test_observer_delivery_serializes_observations_and_forces() -> None:
    session = _ConcurrentAccessSession()
    hub = RuntimeObservationHub(
        context=_source_context(), observers=(_Observer(session),)
    )
    await hub.start()
    observed_at = datetime.now(UTC)

    await asyncio.gather(
        hub.observe(
            NativeStateObservation(
                identity=_identity(),
                graph_namespace=(),
                state={"step": 1},
                observed_at=observed_at,
                monotonic_ns=1,
            )
        ),
        hub.force(ObservationBoundary.CALL_STARTED),
        hub.observe(
            NativeStateObservation(
                identity=_identity(),
                graph_namespace=(),
                state={"step": 2},
                observed_at=observed_at,
                monotonic_ns=2,
            )
        ),
    )
    await hub.terminal("succeeded")
    await hub.close()

    assert session.max_active_calls == 1


@pytest.mark.parametrize("error_type", [SystemExit, KeyboardInterrupt])
async def test_observer_open_preserves_process_control(
    error_type: type[BaseException],
) -> None:
    error = error_type("process-control")
    hub = RuntimeObservationHub(
        context=_source_context(), observers=(_OpeningControlObserver(error),)
    )

    with pytest.raises(error_type) as captured:
        await hub.start()

    assert captured.value is error


@pytest.mark.parametrize("phase", ["observe", "force", "failure_waiter", "close"])
@pytest.mark.parametrize("error_type", [SystemExit, KeyboardInterrupt])
async def test_observer_lifecycle_preserves_process_control(
    phase: str,
    error_type: type[BaseException],
) -> None:
    error = error_type("process-control")
    session = _ControlSession(phase=phase, error=error)
    hub = RuntimeObservationHub(
        context=_source_context(), observers=(_Observer(session),)
    )

    if phase == "observe":
        with pytest.raises(error_type) as captured:
            await hub.start()
        assert captured.value is error
        await hub.close()
        return

    await hub.start()
    if phase == "failure_waiter":
        session.failure_waiter().set_result(error)
        operation = hub.wait_failure()
    elif phase == "force":
        operation = hub.force(ObservationBoundary.RESUME_CHECKPOINTED)
    else:
        operation = hub.close()

    with pytest.raises(error_type) as captured:
        await operation

    assert captured.value is error
    if phase != "close":
        await hub.close()


async def test_graph_preparation_failure_records_input_terminal_and_close(
    definition_factory,
) -> None:
    session = _Session()
    observer = _Observer(session)
    runtime = definition_factory(
        RuntimeError("model setup failed"),
        tinkerfin=TinkerFin().with_namespace("test").with_observer(observer),
    )
    stream = runtime.open_agui_run(
        thread_id=_identity().thread_id,
        run_id=_identity().run_id,
        input={"messages": []},
    )

    events = [event async for event in stream]

    assert isinstance(events[-1], RunErrorEvent)
    assert events[-1].code == "runtime_initialization_error"
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
        "run.terminal",
        "run.closed",
    ]
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "failed"
    assert terminal.error_type == "builtins.RuntimeError"
    assert terminal.code == "runtime_initialization_error"
    assert observer.contexts[0].call_tracking_enabled is False


async def test_initialization_failure_keeps_a_later_observer_failure_distinct(
    definition_factory,
) -> None:
    failed = _Session(fail_kind="run.input")
    healthy = _Session()
    runtime = (
        TinkerFin()
        .with_namespace("test")
        .with_observer(_Observer(failed))
        .with_observer(_Observer(healthy))
    )

    agent = definition_factory(
        RuntimeError("test-owned initialization failure"), tinkerfin=runtime
    )
    stream = agent.open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )
    try:
        with pytest.raises(RunObservationError):
            await anext(stream)
    finally:
        await stream.aclose()

    terminals = [
        item
        for item in healthy.observations
        if isinstance(item, RunTerminalObservation)
    ]
    assert len(terminals) == 1
    assert terminals[0].code == "observer_failed"
    assert terminals[0].outcome == "failed"
    assert failed.closed == healthy.closed == 1


async def test_failed_resume_resolution_preserves_resume_input_kind(
    definition_factory,
) -> None:
    from ag_ui.core.types import ResumeEntry

    from tinkerfin import AgUiResumeRequest

    session = _Session()
    observer = _Observer(session)
    request = AgUiResumeRequest(
        entries=(
            ResumeEntry(
                interrupt_id="interrupt-1#0",
                status="resolved",
                payload={"type": "approve"},
            ),
        )
    )
    runtime = definition_factory(
        RuntimeError("checkpoint resolution failed"),
        tinkerfin=TinkerFin().with_namespace("test").with_observer(observer),
    )
    stream = runtime.open_agui_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, resume=request
    )

    events = [event async for event in stream]

    assert isinstance(events[-1], RunErrorEvent)
    run_input = session.observations[1]
    assert run_input.kind == "run.input"
    assert run_input.source.input_kind == "resume"
    assert run_input.source.resume == ()


async def test_agui_reuses_the_runtime_structural_validation_result(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    class CountingDriver(DeepAgentsV2StreamDriver):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def normalize(
            self,
            part: object,
            *,
            context: RunSourceContext,
        ) -> NativeStreamFrame:
            self.calls += 1
            return super().normalize(part, context=context)

    class CountingProfile(DeepAgentsV2RuntimeProfile):
        def __init__(self, driver: CountingDriver) -> None:
            super().__init__()
            self._driver = driver

        @property
        def stream_driver(self) -> CountingDriver:
            return self._driver

    driver = CountingDriver()
    part = {"type": "values", "ns": (), "data": {"messages": []}}
    runtime = definition_factory(
        _Graph([part]),
        tinkerfin=TinkerFin(runtime_profile=CountingProfile(driver)).with_namespace(
            "test"
        ),
    )

    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
        )
    ]

    assert events[-1].type.value == "RUN_FINISHED"
    assert driver.calls == 1


class _FixtureGraph:
    def __init__(self) -> None:
        self.options: dict[str, object] | None = None

    async def astream(
        self,
        input: object,
        config: object | None = None,
        *,
        fixture_mode: str | None = None,
        context: object | None = None,
    ) -> AsyncIterator[object]:
        del input, context
        self.options = {"config": config, "fixture_mode": fixture_mode}
        yield {"fixture_payload": "value"}


class _FixtureStreamDriver:
    def __init__(self) -> None:
        self.validate_calls = 0
        self.normalize_calls = 0

    def bind_invocation(
        self,
        signature: inspect.Signature,
        args: tuple[object, ...],
        options: Mapping[str, object],
        *,
        identity: RunIdentity,
        runtime_profile: str,
    ) -> inspect.BoundArguments:
        bound = signature.bind(*args, **dict(options))
        bound.arguments["config"] = {
            "configurable": {
                "thread_id": identity.thread_id,
                "_tinkerfin_runtime_profile": runtime_profile,
            }
        }
        bound.arguments["fixture_mode"] = "canonical"
        return bound

    def validate(self, part: object) -> NativeValuesStreamPart:
        self.validate_calls += 1
        if part != {"fixture_payload": "value"}:
            raise ValueError("fixture source emitted an unexpected object")
        return NativeValuesStreamPart(
            type="values",
            ns=(),
            data={"messages": [], "fixture": "value"},
            interrupts=(),
        )

    def normalize(
        self,
        part: object,
        *,
        context: RunSourceContext,
    ) -> NativeStreamFrame:
        self.normalize_calls += 1
        canonical = self.validate(part)
        observation = NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={"fixture": "value"},
            messages=(),
            interrupts=(),
            observed_at=datetime.now(UTC),
            monotonic_ns=0,
        )
        return NativeStreamFrame(
            canonical=canonical,
            observations=(observation,),
            replay=NativeStreamPart(
                type="values",
                ns=(),
                data={"fixture": "value"},
            ),
        )


class _FixtureRuntimeProfile:
    def __init__(self, graph: _FixtureGraph) -> None:
        self._graph = graph
        self._driver = _FixtureStreamDriver()
        self.build_thread_ids: list[int] = []

    @property
    def profile_id(self) -> str:
        return "fixture-profile"

    @property
    def astream_signature(self) -> inspect.Signature:
        return inspect.signature(self._graph.astream)

    def graph_stream(self, graph: object) -> Callable[..., object]:
        stream = getattr(graph, "astream", None)
        if not callable(stream):
            raise TypeError("fixture graph must expose astream")
        return stream

    @property
    def stream_driver(self) -> _FixtureStreamDriver:
        return self._driver

    async def stage_resume_intent(
        self,
        checkpointer: _ProfileCheckpointSaver,
        config: RunnableConfig,
        writes: tuple[tuple[str, object], ...],
    ) -> None:
        del checkpointer, config, writes
        raise AssertionError("fixture profile does not stage resume state")

    def pending_resume_values(
        self,
        checkpoint: CheckpointTuple,
        *,
        channel_name: str,
    ) -> tuple[object, ...]:
        del checkpoint, channel_name
        return ()

    def _build(self, *_args: object, **_kwargs: object) -> _FixtureGraph:
        self.build_thread_ids.append(threading.get_ident())
        return self._graph


@pytest.mark.asyncio
async def test_open_run_is_observable_before_output_and_closes_without_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _FixtureGraph()
    profile = _FixtureRuntimeProfile(graph)
    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", profile._build)
    session = _Session()
    tinkerfin = (
        TinkerFin(runtime_profile=profile)
        .with_namespace("test")
        .with_observer(_Observer(session))
    )
    definition = tinkerfin.build(model="provider:model", tools=[])

    stream = definition.open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    assert session.observations == []
    await stream.messaging_owner_preflight()
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
    ]
    assert graph.options is None
    await stream.aclose()
    assert [item.kind for item in session.observations[-2:]] == [
        "run.terminal",
        "run.closed",
    ]
    terminal = session.observations[-2]
    assert isinstance(terminal, RunTerminalObservation)
    assert terminal.outcome == "cancelled"
    assert session.closed == 1


@pytest.mark.asyncio
async def test_open_agui_run_is_observable_before_its_first_public_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _FixtureGraph()
    profile = _FixtureRuntimeProfile(graph)
    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", profile._build)
    session = _Session()
    tinkerfin = (
        TinkerFin(runtime_profile=profile)
        .with_namespace("test")
        .with_observer(_Observer(session))
    )
    definition = tinkerfin.build(model="provider:model", tools=[])

    stream = definition.open_agui_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
    )

    assert session.observations == []
    await stream.messaging_owner_preflight()
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
    ]
    assert graph.options is None
    await stream.aclose()
    assert [item.kind for item in session.observations[-2:]] == [
        "run.terminal",
        "run.closed",
    ]
    terminal = session.observations[-2]
    assert isinstance(terminal, RunTerminalObservation)
    assert terminal.outcome == "cancelled"
    assert session.closed == 1


async def test_profile_maps_a_non_v2_source_once_for_observer_and_agui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _FixtureGraph()
    profile = _FixtureRuntimeProfile(graph)
    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", profile._build)
    assert isinstance(profile, DeepAgentsRuntimeProfile)
    event_loop_thread = threading.get_ident()
    session = _Session()
    definition = (
        TinkerFin(runtime_profile=profile)
        .with_namespace("test")
        .with_observer(_Observer(session))
        .build(model="provider:model", tools=[])
    )
    runtime = definition

    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id=_identity().thread_id, run_id=_identity().run_id, input=_input()
        )
    ]

    assert graph.options is not None
    assert graph.options["fixture_mode"] == "canonical"
    raw_config = graph.options["config"]
    assert isinstance(raw_config, Mapping)
    configurable = raw_config["configurable"]
    assert isinstance(configurable, Mapping)
    assert configurable["thread_id"] == "thread-observed"
    assert configurable["run_id"] == "run-observed"
    assert configurable["_tinkerfin_runtime_profile"] == "fixture-profile"
    assert not ({"version", "stream_mode", "subgraphs"} & set(graph.options))
    assert profile.stream_driver.validate_calls == 1
    assert profile.stream_driver.normalize_calls == 1
    assert profile.build_thread_ids
    assert all(thread_id != event_loop_thread for thread_id in profile.build_thread_ids)
    assert any(event.type.value == "STATE_SNAPSHOT" for event in events)
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
        "native.state",
        "run.terminal",
        "run.closed",
    ]


async def test_profile_uses_its_stream_signature_for_direct_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _FixtureGraph()
    profile = _FixtureRuntimeProfile(graph)
    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", profile._build)
    definition = (
        TinkerFin(runtime_profile=profile)
        .with_namespace("test")
        .build(model="provider:model", tools=[])
    )

    direct = await create_graph(definition)
    state = await direct.ainvoke(_input())

    assert state == {"messages": [], "fixture": "value"}
    assert graph.options == {"config": None, "fixture_mode": None}
    assert profile.stream_driver.validate_calls == 1
    assert profile.stream_driver.normalize_calls == 0
