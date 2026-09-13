"""Durable resume checkpoint callback and retry contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from ag_ui.core import BaseEvent, RunFinishedEvent
from ag_ui.core.types import ResumeEntry
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from tinkerfin import (
    AgUiResumeCheckpoint,
    AgUiResumeRequest,
    RunIdentity,
    TinkerFin,
)
from tinkerfin._agui_lineage_state import LINEAGE_METADATA_KEY, RESUME_METADATA_KEY
from tinkerfin._checkpoint import NamespaceCheckpointer
from tinkerfin.runtime_profile import DeepAgentsV2RuntimeProfile
from tinkerfin_contracts import (
    ObservationBoundary,
    PreparedWorkspace,
    RunObservationSession,
    RunSourceContext,
    RuntimeObservation,
)


class _SetupWorkspace:
    def __init__(self, before_build: Callable[[], Awaitable[None]]) -> None:
        self._before_build = before_build

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[None, StateBackend]]:
        await self._before_build()
        yield PreparedWorkspace(None, StateBackend())


class _ResumeState(MessagesState, total=False):
    result: str


class _OrderSession:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.failure: asyncio.Future[BaseException] | None = None

    async def observe(self, observation: RuntimeObservation) -> None:
        self.order.append(f"trace:{observation.kind}")

    async def force(self, boundary: ObservationBoundary) -> None:
        self.order.append(f"force:{boundary.value}")

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        if self.failure is None:
            self.failure = asyncio.get_running_loop().create_future()
        return self.failure

    async def aclose(self) -> None:
        return None


class _OrderObserver:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        del context
        return _OrderSession(self.order)


def _install_resume_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    MemorySaver,
    list[CompiledStateGraph[Any, Any, Any, Any]],
    list[object],
]:
    saver = MemorySaver()
    graphs: list[CompiledStateGraph[Any, Any, Any, Any]] = []
    executions: list[object] = []

    def build(*_args: object, **_kwargs: object):
        async def reviewed(state: _ResumeState) -> dict[str, object]:
            del state
            answer = interrupt(
                {
                    "action_requests": [{"name": "continue_run", "args": {}}],
                    "review_configs": [
                        {
                            "action_name": "continue_run",
                            "allowed_decisions": ["approve"],
                        }
                    ],
                }
            )
            executions.append(answer)
            return {
                "result": "done",
                "messages": [
                    ToolMessage(content="continued", tool_call_id="continue-call")
                ],
            }

        builder = StateGraph(_ResumeState)
        builder.add_node("reviewed", reviewed)
        builder.add_edge(START, "reviewed")
        builder.add_edge("reviewed", END)
        graph = builder.compile(checkpointer=NamespaceCheckpointer(saver, "test"))
        graphs.append(graph)
        return graph

    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph",
        build,
    )
    return saver, graphs, executions


async def _create_interrupted_parent(
    definition: object,
    graphs: list[CompiledStateGraph[Any, Any, Any, Any]],
) -> str:
    identity = RunIdentity(namespace="test", thread_id="thread-1", run_id="run-parent")
    runtime = cast(Any, definition)
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input={
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "continue_run", "args": {}, "id": "continue-call"}
                        ],
                    )
                ]
            },
        )
    ]
    assert events[-1].type.value == "RUN_FINISHED"
    snapshot = await graphs[-1].aget_state(
        {"configurable": {"thread_id": identity.thread_id}}
    )
    assert len(snapshot.interrupts) == 1
    return snapshot.interrupts[0].id


def _resume_request(interrupt_id: str) -> tuple[RunIdentity, AgUiResumeRequest]:
    identity = RunIdentity(namespace="test", thread_id="thread-1", run_id="run-resume")
    return identity, AgUiResumeRequest(
        entries=(
            ResumeEntry(
                interrupt_id=interrupt_id,
                status="resolved",
                payload={"type": "approve"},
            ),
        )
    )


def _assert_private_marker_absent(events: list[BaseEvent]) -> None:
    serialized = json.dumps(
        [
            event.model_dump(mode="json", by_alias=True, exclude_none=False)
            for event in events
        ],
        ensure_ascii=False,
    )
    assert RESUME_METADATA_KEY not in serialized
    assert LINEAGE_METADATA_KEY not in serialized


@pytest.mark.asyncio
async def test_open_agui_run_reuses_one_graph_for_resume_resolution_and_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = MemorySaver()
    tinkerfin = TinkerFin(checkpointer=saver).with_namespace("test")
    graphs: list[CompiledStateGraph[Any, Any, Any, Any]] = []
    resumed: list[object] = []

    def build(*_args: object, **_kwargs: object):
        async def reviewed(state: MessagesState) -> dict[str, object]:
            del state
            decision = interrupt(
                {
                    "action_requests": [{"name": "write_file", "args": {"path": "a"}}],
                    "review_configs": [
                        {
                            "action_name": "write_file",
                            "allowed_decisions": ["approve"],
                        }
                    ],
                }
            )
            resumed.append(decision)
            return {}

        builder = StateGraph(MessagesState)
        builder.add_node("reviewed", reviewed)
        builder.add_edge(START, "reviewed")
        builder.add_edge("reviewed", END)
        graph = builder.compile(checkpointer=NamespaceCheckpointer(saver, "test"))
        graphs.append(graph)
        return graph

    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph",
        build,
    )
    definition = tinkerfin.build(model="provider:model", tools=[])
    parent_identity = RunIdentity(
        namespace="test", thread_id="thread-managed", run_id="run-parent"
    )
    review_message = AIMessage(
        id="review-message",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "a"},
                "id": "call-a",
                "type": "tool_call",
            }
        ],
    )
    parent = definition.open_agui_run(
        thread_id=parent_identity.thread_id,
        run_id=parent_identity.run_id,
        input={"messages": [review_message]},
    )
    parent_events = [event async for event in parent]
    terminal = parent_events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    public_interrupt_id = terminal.outcome.interrupts[0].id

    request = AgUiResumeRequest(
        entries=(
            ResumeEntry.model_validate(
                {
                    "interruptId": public_interrupt_id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ),
        )
    )
    resume_identity = RunIdentity(
        namespace="test",
        thread_id=parent_identity.thread_id,
        run_id="run-resume",
    )
    checkpoints: list[AgUiResumeCheckpoint] = []

    async def checkpointed(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)

    resumed_stream = definition.open_agui_run(
        thread_id=resume_identity.thread_id,
        run_id=resume_identity.run_id,
        resume=request,
        parent_run_id=parent_identity.run_id,
        on_resume_saved=checkpointed,
    )
    resumed_events = [event async for event in resumed_stream]

    assert resumed_events[-1].type.value == "RUN_FINISHED"
    assert len(graphs) == 2
    assert len(checkpoints) == 1
    assert resumed == [{"decisions": [{"type": "approve"}]}]

    releases = 0

    async def fail_agent_setup() -> None:
        raise RuntimeError("retry factory failed")

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1

    retry_runtime = (
        TinkerFin(
            checkpointer=saver,
        )
        .with_namespace("test")
        .build(backend=_SetupWorkspace(fail_agent_setup), model="provider:model")
    )
    retry = retry_runtime.open_agui_run(
        thread_id=resume_identity.thread_id,
        run_id=resume_identity.run_id,
        resume=request,
        parent_run_id=parent_identity.run_id,
        on_resume_not_saved=release_unprepared,
    )
    retry_events = [event async for event in retry]

    assert retry_events[-1].type.value == "RUN_ERROR"
    assert releases == 0


@pytest.mark.asyncio
async def test_open_agui_run_releases_preparation_failure_once() -> None:
    saver = MemorySaver()
    identity = RunIdentity(
        namespace="test", thread_id="thread-unprepared", run_id="run-resume"
    )
    request = AgUiResumeRequest(
        entries=(
            ResumeEntry.model_validate(
                {
                    "interruptId": "interrupt-1#0",
                    "status": "cancelled",
                }
            ),
        )
    )
    releases = 0

    async def fail_agent_setup() -> None:
        raise RuntimeError("model setup failed")

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1

    runtime = (
        TinkerFin(
            checkpointer=saver,
        )
        .with_namespace("test")
        .build(backend=_SetupWorkspace(fail_agent_setup), model="provider:model")
    )
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        resume=request,
        on_resume_not_saved=release_unprepared,
    )
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert releases == 1


async def test_resume_preparation_uses_the_configured_saver() -> None:
    override_saver = MemorySaver()
    identity = RunIdentity(
        namespace="test", thread_id="thread-declared-saver", run_id="run-resume"
    )
    request = AgUiResumeRequest(
        entries=(
            ResumeEntry.model_validate(
                {
                    "interruptId": "interrupt-1#0",
                    "status": "cancelled",
                }
            ),
        )
    )
    releases = 0

    async def fail_agent_setup() -> None:
        raise RuntimeError("model setup failed")

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1

    runtime = (
        TinkerFin(
            checkpointer=override_saver,
        )
        .with_namespace("test")
        .build(backend=_SetupWorkspace(fail_agent_setup), model="provider:model")
    )
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        resume=request,
        on_resume_not_saved=release_unprepared,
    )
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert releases == 1


async def test_execution_cannot_replace_the_bound_checkpoint_saver() -> None:
    runtime = (
        TinkerFin(checkpointer=MemorySaver())
        .with_namespace("test")
        .build(model="provider:model")
    )
    request = AgUiResumeRequest(
        entries=(ResumeEntry(interrupt_id="pending", status="cancelled"),)
    )
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            resume=request,
            resume_checkpointer=MemorySaver(),
        )  # pyright: ignore[reportCallIssue]


class _ReleaseControl(BaseException):
    """Represent process control delivered by the resume claim owner."""


@pytest.mark.parametrize("release_kind", ["success", "ordinary", "control"])
async def test_open_agui_run_settles_preparation_cancellation_once(
    release_kind: str,
) -> None:
    saver = MemorySaver()
    identity = RunIdentity(
        namespace="test", thread_id="thread-cancel-setup", run_id="run-resume"
    )
    request = AgUiResumeRequest(
        entries=(
            ResumeEntry.model_validate(
                {
                    "interruptId": "interrupt-1#0",
                    "status": "cancelled",
                }
            ),
        )
    )
    entered = asyncio.Event()
    blocked = asyncio.Event()
    releases = 0
    release_error = (
        _ReleaseControl("host release interrupted")
        if release_kind == "control"
        else RuntimeError("host release failed")
    )
    release_cause = OSError("release original cause")
    release_error.__cause__ = release_cause

    async def create_agent() -> None:
        entered.set()
        await blocked.wait()
        raise AssertionError("cancelled Agent setup resumed unexpectedly")

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1
        if release_kind != "success":
            raise release_error

    runtime = (
        TinkerFin(
            checkpointer=saver,
        )
        .with_namespace("test")
        .build(backend=_SetupWorkspace(create_agent), model="provider:model")
    )
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        resume=request,
        on_resume_not_saved=release_unprepared,
    )
    task = asyncio.create_task(stream.messaging_owner_preflight())
    await entered.wait()
    task.cancel()
    task.cancel()

    expected = _ReleaseControl if release_kind == "control" else asyncio.CancelledError
    with pytest.raises(expected) as captured:
        await task

    assert releases == 1
    if release_kind == "control":
        assert captured.value is release_error
    if release_kind != "success":
        pending_errors: list[BaseException] = [captured.value]
        retained: set[int] = set()
        while pending_errors:
            error = pending_errors.pop()
            if id(error) in retained:
                continue
            retained.add(id(error))
            pending_errors.extend(
                item
                for item in (error.__cause__, error.__context__)
                if item is not None
            )
            if isinstance(error, BaseExceptionGroup):
                pending_errors.extend(error.exceptions)
        assert id(release_error) in retained
        assert id(release_cause) in retained
    await stream.aclose()
    await stream.aclose()


async def test_open_agui_run_retains_marker_probe_across_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = MemorySaver()
    identity = RunIdentity(
        namespace="test", thread_id="thread-cancel-probe", run_id="run-resume"
    )
    request = AgUiResumeRequest(
        entries=(
            ResumeEntry.model_validate(
                {
                    "interruptId": "interrupt-1#0",
                    "status": "cancelled",
                }
            ),
        )
    )
    factory_started = asyncio.Event()
    factory_blocked = asyncio.Event()
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    releases = 0

    async def create_agent() -> None:
        factory_started.set()
        await factory_blocked.wait()
        raise AssertionError("cancelled Agent setup resumed unexpectedly")

    async def marker_is_durable(*_args: object, **_kwargs: object) -> bool:
        probe_started.set()
        await release_probe.wait()
        return False

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1

    monkeypatch.setattr(
        "tinkerfin._agui_lineage.agui_resume_marker_is_durable",
        marker_is_durable,
    )
    runtime = (
        TinkerFin(
            checkpointer=saver,
        )
        .with_namespace("test")
        .build(backend=_SetupWorkspace(create_agent), model="provider:model")
    )
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        resume=request,
        on_resume_not_saved=release_unprepared,
    )
    task = asyncio.create_task(stream.messaging_owner_preflight())
    await factory_started.wait()
    task.cancel("first")
    await probe_started.wait()
    task.cancel("second")
    release_probe.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert releases == 1


@pytest.mark.asyncio
async def test_runtime_resolves_resume_from_checkpoint_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = MemorySaver()
    resumed: list[object] = []

    def build(*_args: object, **_kwargs: object):
        async def reviewed(state: MessagesState) -> dict[str, object]:
            del state
            decision = interrupt(
                {
                    "action_requests": [
                        {"name": "write_file", "args": {"path": "a"}},
                        {"name": "write_file", "args": {"path": "b"}},
                    ],
                    "review_configs": [
                        {
                            "action_name": "write_file",
                            "allowed_decisions": ["approve"],
                        },
                        {
                            "action_name": "write_file",
                            "allowed_decisions": ["approve"],
                        },
                    ],
                }
            )
            resumed.append(decision)
            return {}

        builder = StateGraph(MessagesState)
        builder.add_node("reviewed", reviewed)
        builder.add_edge(START, "reviewed")
        builder.add_edge("reviewed", END)
        return builder.compile(checkpointer=NamespaceCheckpointer(saver, "test"))

    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph",
        build,
    )
    definition = (
        TinkerFin().with_namespace("test").build(model="provider:model", tools=[])
    )
    parent_identity = RunIdentity(
        namespace="test", thread_id="thread-native-resume", run_id="run-parent"
    )
    review_message = AIMessage(
        id="review-message",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "a"},
                "id": "call-a",
                "type": "tool_call",
            },
            {
                "name": "write_file",
                "args": {"path": "b"},
                "id": "call-b",
                "type": "tool_call",
            },
        ],
    )
    parent = definition
    parent_events = [
        event
        async for event in parent.open_agui_run(
            thread_id=parent_identity.thread_id,
            run_id=parent_identity.run_id,
            input={"messages": [review_message]},
        )
    ]
    terminal = parent_events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    outcome = terminal.outcome
    assert outcome is not None and outcome.type == "interrupt"
    public_ids = tuple(item.id for item in outcome.interrupts)

    resume_identity = RunIdentity(
        namespace="test",
        thread_id=parent_identity.thread_id,
        run_id="run-resume",
    )
    request = AgUiResumeRequest(
        entries=tuple(
            ResumeEntry.model_validate(
                {
                    "interruptId": interrupt_id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            )
            for interrupt_id in reversed(public_ids)
        )
    )
    binding = request

    failed_checkpoints: list[AgUiResumeCheckpoint] = []

    async def fail_after_staging(checkpoint: AgUiResumeCheckpoint) -> None:
        failed_checkpoints.append(checkpoint)
        raise RuntimeError("host settlement unavailable")

    failed_runtime = definition
    failed_events = [
        event
        async for event in failed_runtime.open_agui_run(
            thread_id=resume_identity.thread_id,
            run_id=resume_identity.run_id,
            parent_run_id=parent_identity.run_id,
            resume=binding,
            on_resume_saved=fail_after_staging,
        )
    ]

    assert failed_events[-1].type.value == "RUN_ERROR"
    assert resumed == []
    assert len(failed_checkpoints) == 1

    recovered = request
    recovered_checkpoints: list[AgUiResumeCheckpoint] = []

    async def checkpointed(checkpoint: AgUiResumeCheckpoint) -> None:
        recovered_checkpoints.append(checkpoint)

    resumed_runtime = definition
    resumed_events = [
        event
        async for event in resumed_runtime.open_agui_run(
            thread_id=resume_identity.thread_id,
            run_id=resume_identity.run_id,
            resume=recovered,
            on_resume_saved=checkpointed,
        )
    ]

    assert resumed_events[-1].type.value == "RUN_FINISHED"
    assert recovered_checkpoints == failed_checkpoints
    assert recovered_checkpoints[0].parent_run_id == parent_identity.run_id
    assert resumed == [{"decisions": [{"type": "approve"}, {"type": "approve"}]}]


@pytest.mark.asyncio
async def test_checkpoint_callback_failure_retries_without_reexecuting_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, graphs, executions = _install_resume_graph(monkeypatch)
    definition = (
        TinkerFin().with_namespace("test").build(model="provider:model", tools=[])
    )
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_request(interrupt_id)
    checkpoints: list[AgUiResumeCheckpoint] = []
    initialization_releases = 0

    async def release_unprepared() -> None:
        nonlocal initialization_releases
        initialization_releases += 1

    async def fail_after_recording(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)
        raise RuntimeError("checkpoint observer unavailable")

    failed_runtime = cast(Any, definition)
    failed_stream = failed_runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_saved=fail_after_recording,
        on_resume_not_saved=release_unprepared,
    )
    failed_events = [event async for event in failed_stream]

    assert [event.type.value for event in failed_events] == [
        "RUN_STARTED",
        "RUN_ERROR",
    ]
    assert isinstance(failed_stream.error, RuntimeError)
    assert executions == []
    assert len(checkpoints) == 1
    _assert_private_marker_absent(failed_events)

    trace: list[str] = []

    async def checkpointed(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)
        trace.append("checkpointed")

    async def observe(_part: Mapping[str, object]) -> None:
        trace.append("part")

    retry_runtime = cast(Any, definition)
    retry_stream = retry_runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_saved=checkpointed,
        on_resume_not_saved=release_unprepared,
        on_native_part=observe,
    )
    retry_events = [event async for event in retry_stream]

    assert retry_events[-1].type.value == "RUN_FINISHED"
    assert retry_stream.error is None
    assert executions == [{"decisions": [{"type": "approve"}]}]
    assert checkpoints[0] == checkpoints[1]
    assert trace[0] == "checkpointed"
    assert "part" in trace
    assert initialization_releases == 0
    _assert_private_marker_absent(retry_events)


@pytest.mark.asyncio
async def test_resume_staging_failure_releases_unprepared_host_claim_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, graphs, executions = _install_resume_graph(monkeypatch)
    definition = (
        TinkerFin().with_namespace("test").build(model="provider:model", tools=[])
    )
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_request(interrupt_id)
    original_stage = DeepAgentsV2RuntimeProfile.stage_resume_intent
    releases = 0
    checkpoints: list[AgUiResumeCheckpoint] = []

    async def fail_stage(
        _profile: DeepAgentsV2RuntimeProfile,
        _checkpointer: object,
        _config: object,
        _writes: object,
    ) -> None:
        raise RuntimeError("resume staging unavailable")

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1

    async def checkpointed(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)

    monkeypatch.setattr(DeepAgentsV2RuntimeProfile, "stage_resume_intent", fail_stage)
    runtime = cast(Any, definition)
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_saved=checkpointed,
        on_resume_not_saved=release_unprepared,
    )
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert releases == 1
    assert checkpoints == []
    assert executions == []

    monkeypatch.setattr(
        DeepAgentsV2RuntimeProfile,
        "stage_resume_intent",
        original_stage,
    )
    retry_checkpoints: list[AgUiResumeCheckpoint] = []

    async def retry_checkpointed(checkpoint: AgUiResumeCheckpoint) -> None:
        retry_checkpoints.append(checkpoint)

    retry = cast(Any, definition)
    retry_events = [
        event
        async for event in retry.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            parent_run_id="run-parent",
            resume=binding,
            on_resume_saved=retry_checkpointed,
            on_resume_not_saved=release_unprepared,
        )
    ]

    assert retry_events[-1].type.value == "RUN_FINISHED"
    assert releases == 1
    assert len(retry_checkpoints) == 1
    assert executions == [{"decisions": [{"type": "approve"}]}]


@pytest.mark.asyncio
async def test_resume_staging_cancellation_releases_unprepared_host_claim_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _saver, graphs, executions = _install_resume_graph(monkeypatch)
    definition = (
        TinkerFin().with_namespace("test").build(model="provider:model", tools=[])
    )
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_request(interrupt_id)
    entered = asyncio.Event()
    allow_stage_settlement = asyncio.Event()
    releases = 0

    async def block_stage(
        _profile: DeepAgentsV2RuntimeProfile,
        _checkpointer: object,
        _config: object,
        _writes: object,
    ) -> None:
        entered.set()
        await allow_stage_settlement.wait()

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1

    monkeypatch.setattr(DeepAgentsV2RuntimeProfile, "stage_resume_intent", block_stage)
    runtime = cast(Any, definition)
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_not_saved=release_unprepared,
    )
    assert (await anext(stream)).type.value == "RUN_STARTED"
    pull = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), timeout=5)

    pull.cancel()
    allow_stage_settlement.set()
    result = (await asyncio.gather(pull, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    await stream.aclose()

    assert releases == 1
    assert executions == []


@pytest.mark.asyncio
async def test_resume_post_write_cancellation_keeps_the_durable_host_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, graphs, executions = _install_resume_graph(monkeypatch)
    definition = (
        TinkerFin().with_namespace("test").build(model="provider:model", tools=[])
    )
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_request(interrupt_id)
    original_stage = DeepAgentsV2RuntimeProfile.stage_resume_intent
    releases = 0

    async def write_then_cancel(
        profile: DeepAgentsV2RuntimeProfile,
        checkpointer: Any,
        config: RunnableConfig,
        writes: tuple[tuple[str, object], ...],
    ) -> None:
        await original_stage(profile, checkpointer, config, writes)
        raise asyncio.CancelledError

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1

    monkeypatch.setattr(
        DeepAgentsV2RuntimeProfile,
        "stage_resume_intent",
        write_then_cancel,
    )
    runtime = cast(Any, definition)
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_not_saved=release_unprepared,
    )
    assert (await anext(stream)).type.value == "RUN_STARTED"

    with pytest.raises(asyncio.CancelledError):
        await anext(stream)
    await stream.aclose()

    head = await NamespaceCheckpointer(saver, "test").aget_tuple(
        {"configurable": {"thread_id": identity.thread_id}}
    )
    assert head is not None
    assert any(
        channel == RESUME_METADATA_KEY
        for _task_id, channel, _value in head.pending_writes or ()
    )
    assert releases == 0
    assert executions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("factory failed"), SystemExit("stop")])
async def test_resume_graph_factory_failure_uses_pre_marker_settlement(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    saver, graphs, _executions = _install_resume_graph(monkeypatch)
    definition = (
        TinkerFin().with_namespace("test").build(model="provider:model", tools=[])
    )
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_request(interrupt_id)
    releases = 0

    def fail_factory(*_args: object, **_kwargs: object) -> object:
        raise error

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1

    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph",
        fail_factory,
    )
    failing_definition = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .build(model="provider:model", tools=[])
    )
    runtime = cast(Any, failing_definition)
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_not_saved=release_unprepared,
    )
    if isinstance(error, Exception):
        first = await anext(stream)
        second = await anext(stream)
        assert first.type.value == "RUN_STARTED"
        assert second.type.value == "RUN_ERROR"
    else:
        with pytest.raises(type(error)) as captured:
            await anext(stream)
        assert captured.value is error
    await stream.aclose()
    assert releases == 1


@pytest.mark.asyncio
async def test_resume_graph_factory_failure_preserves_a_durable_retry_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, graphs, _executions = _install_resume_graph(monkeypatch)
    definition = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .build(model="provider:model", tools=[])
    )
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_request(interrupt_id)

    async def fail_after_marker(_checkpoint: AgUiResumeCheckpoint) -> None:
        raise RuntimeError("host settlement unavailable")

    first = cast(Any, definition)
    first_events = [
        event
        async for event in first.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            parent_run_id="run-parent",
            resume=binding,
            on_resume_saved=fail_after_marker,
        )
    ]
    assert first_events[-1].type.value == "RUN_ERROR"

    def fail_factory(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("retry factory failed")

    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph",
        fail_factory,
    )
    releases = 0

    async def release_unprepared() -> None:
        nonlocal releases
        releases += 1

    retry_definition = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .build(model="provider:model", tools=[])
    )
    retry = cast(Any, retry_definition)
    retry_events = [
        event
        async for event in retry.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            parent_run_id="run-parent",
            resume=binding,
            on_resume_not_saved=release_unprepared,
        )
    ]

    assert retry_events[-1].type.value == "RUN_ERROR"
    assert releases == 0


@pytest.mark.asyncio
async def test_resume_staging_keeps_primary_failure_when_claim_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _saver, graphs, _executions = _install_resume_graph(monkeypatch)
    definition = (
        TinkerFin().with_namespace("test").build(model="provider:model", tools=[])
    )
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_request(interrupt_id)
    releases = 0

    async def fail_stage(
        _profile: DeepAgentsV2RuntimeProfile,
        _checkpointer: object,
        _config: object,
        _writes: object,
    ) -> None:
        raise RuntimeError("resume staging unavailable")

    async def fail_release() -> None:
        nonlocal releases
        releases += 1
        raise RuntimeError("claim release unavailable")

    monkeypatch.setattr(DeepAgentsV2RuntimeProfile, "stage_resume_intent", fail_stage)
    runtime = cast(Any, definition)
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_not_saved=fail_release,
    )
    events = [event async for event in stream]

    assert events[-1].type.value == "RUN_ERROR"
    assert releases == 1
    assert stream.error is not None
    assert any(
        "claim release unavailable" in note
        for note in getattr(stream.error, "__notes__", ())
    )


@pytest.mark.asyncio
async def test_close_during_checkpoint_callback_keeps_exactly_once_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _saver, graphs, executions = _install_resume_graph(monkeypatch)
    definition = (
        TinkerFin().with_namespace("test").build(model="provider:model", tools=[])
    )
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_request(interrupt_id)
    checkpoints: list[AgUiResumeCheckpoint] = []
    checkpoint_entered = asyncio.Event()

    async def checkpointed(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)
        checkpoint_entered.set()
        await asyncio.Event().wait()

    runtime = cast(Any, definition)
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_saved=checkpointed,
    )
    first = await anext(stream)
    assert first.type.value == "RUN_STARTED"
    next_event = asyncio.create_task(anext(stream))
    await asyncio.wait_for(checkpoint_entered.wait(), timeout=5)
    assert len(checkpoints) == 1
    assert executions == []
    next_event.cancel()
    outcome = (await asyncio.gather(next_event, return_exceptions=True))[0]
    assert isinstance(outcome, asyncio.CancelledError)
    await stream.aclose()
    assert executions == []

    async def checkpoint_retry(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)

    retry = cast(Any, definition)
    events = [
        event
        async for event in retry.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            parent_run_id="run-parent",
            resume=binding,
            on_resume_saved=checkpoint_retry,
        )
    ]

    assert events[-1].type.value == "RUN_FINISHED"
    assert executions == [{"decisions": [{"type": "approve"}]}]
    assert len(checkpoints) == 2
    assert checkpoints[0] == checkpoints[1]
    _assert_private_marker_absent(events)


@pytest.mark.asyncio
async def test_trace_resume_checkpoint_forces_before_user_callback_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, graphs, executions = _install_resume_graph(monkeypatch)
    order: list[str] = []
    definition = cast(
        Any, TinkerFin().with_namespace("test").with_observer(_OrderObserver(order))
    ).build(model="provider:model", tools=[])
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    order.clear()
    identity, binding = _resume_request(interrupt_id)

    async def checkpointed(_checkpoint: AgUiResumeCheckpoint) -> None:
        head = await NamespaceCheckpointer(saver, "test").aget_tuple(
            {"configurable": {"thread_id": identity.thread_id}}
        )
        assert head is not None
        assert head.metadata.get("source") == "loop"
        private_channels = {
            channel
            for _task_id, channel, _value in head.pending_writes or ()
            if channel == RESUME_METADATA_KEY
        }
        assert private_channels == {RESUME_METADATA_KEY}
        assert executions == []
        order.append("callback")

    async def on_part(_part: Mapping[str, object]) -> None:
        order.append("part")

    runtime = cast(Any, definition)
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            parent_run_id="run-parent",
            resume=binding,
            on_resume_saved=checkpointed,
            on_native_part=on_part,
        )
    ]

    assert events[-1].type.value == "RUN_FINISHED"
    trace_index = order.index("trace:run.resume_checkpointed")
    force_index = order.index("force:resume_checkpointed")
    callback_index = order.index("callback")
    part_index = order.index("part")
    assert trace_index < force_index < callback_index < part_index
