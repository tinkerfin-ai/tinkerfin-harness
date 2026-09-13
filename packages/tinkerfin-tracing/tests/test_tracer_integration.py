"""End-to-end Runtime Observer, semantic Ledger, query, follow, and Projection tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from ag_ui.core import RunFinishedEvent
from langchain.agents.middleware.types import InputAgentState
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Interrupt
from pydantic import BaseModel

from tinkerfin import (
    AgentRuntime,
    RunObservationError,
    TinkerFin,
)
from tinkerfin.runtime_profile import (
    DeepAgentsV2RuntimeProfile,
    DeepAgentsV3RuntimeProfile,
)
from tinkerfin_contracts import (
    ModelCallObservation,
    NativeMessageRecord,
    NativeTaskObservation,
    RunClosedObservation,
    RunIdentity,
    RunInputKind,
    RunInputObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RuntimeObservation,
    ThreadIdentity,
    ToolExecutionObservation,
)
from tinkerfin_tracing import (
    AmbiguousTraceHead,
    InMemoryTraceStore,
    MessageFact,
    ReasoningCapturePolicy,
    ReasoningFact,
    RunFact,
    StateRevisionFact,
    SubagentFact,
    ToolFact,
    TraceEvent,
    TraceGraph,
    TraceLimits,
    TraceProjectionFailed,
    Tracer,
    TraceSemanticFact,
    TraceThreadKey,
    TurnFact,
)
from tinkerfin_tracing.examples import FactCountProjection, FactCountResult
from tinkerfin_tracing.graph import (
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
)
from tinkerfin_tracing.projection import (
    advance_core_projection_state,
    empty_core_projection_state,
    project_core,
    project_core_checkpoint,
)


class _ProviderReasoningExtractor:
    @property
    def name(self) -> str:
        return "fixture.provider_reasoning"

    def extract(
        self,
        message: BaseMessage,
        *,
        provider: str | None,
    ) -> str | None:
        if provider != "deepseek":
            return None
        value = message.additional_kwargs.get("reasoning_content")
        return value if isinstance(value, str) and value else None


class _Graph:
    def __init__(self, parts: Sequence[Mapping[str, object]]) -> None:
        self.parts = tuple(parts)

    async def astream(
        self,
        *_args: object,
        **_options: object,
    ) -> AsyncIterator[Mapping[str, object]]:
        for part in self.parts:
            yield part


class _SubagentToolBindingModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> _SubagentToolBindingModel:
        del tools, kwargs
        return self


@tool
def reviewed_child_tool(value: str) -> str:
    """Return a value after the child Tool review."""

    return value


class _ReadCountingStore(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls: list[tuple[int, int, int]] = []

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        self.read_calls.append((after_seq, as_of_seq, limit))
        return await super().read_events(
            key,
            after_seq=after_seq,
            as_of_seq=as_of_seq,
            limit=limit,
        )


async def _record_run(
    tracer: Tracer,
    *,
    run_id: str,
    parent_run_id: str | None = None,
    input_kind: RunInputKind = "ordinary",
) -> None:
    identity = RunIdentity(namespace="test", thread_id="thread-lineage", run_id=run_id)
    context = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        input={"messages": [{"role": "user", "id": f"user-{run_id}"}]},
        config={},
    )
    session = await tracer.open_run(context)
    now = datetime.now(UTC)
    observations: tuple[RuntimeObservation, ...] = (
        RunStartedObservation(identity=identity, observed_at=now, monotonic_ns=1),
        RunInputObservation(
            identity=identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        ),
        RunTerminalObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=3,
        ),
        RunClosedObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=4,
        ),
    )
    for observation in observations:
        await session.observe(observation)
    await session.aclose()


async def test_model_context_spans_preparation_without_duplicating_model_request() -> (
    None
):
    tracer = Tracer(store=InMemoryTraceStore())
    identity = RunIdentity(
        namespace="test", thread_id="thread-context-time", run_id="run-context-time"
    )
    source = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={
            "messages": [{"role": "user", "id": "context-user", "content": "question"}]
        },
        config={},
    )
    session = await tracer.open_run(source)
    started = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
    observations: tuple[RuntimeObservation, ...] = (
        RunStartedObservation(
            identity=identity,
            observed_at=started,
            monotonic_ns=1,
        ),
        RunInputObservation(
            identity=identity,
            source=source,
            observed_at=started + timedelta(milliseconds=1),
            monotonic_ns=2,
        ),
        ModelCallObservation(
            identity=identity,
            phase="started",
            call_id="model-first",
            messages=(
                NativeMessageRecord(message_type="system", content="final system"),
                NativeMessageRecord(message_type="human", content="question"),
            ),
            observed_at=started + timedelta(milliseconds=20),
            monotonic_ns=3,
        ),
        ModelCallObservation(
            identity=identity,
            phase="completed",
            call_id="model-first",
            observed_at=started + timedelta(milliseconds=120),
            monotonic_ns=4,
        ),
        ToolExecutionObservation(
            identity=identity,
            phase="started",
            execution_id="tool-first",
            tool_call_id="tool-call-first",
            tool_name="search",
            input={"query": "context"},
            observed_at=started + timedelta(milliseconds=130),
            monotonic_ns=5,
        ),
        ToolExecutionObservation(
            identity=identity,
            phase="completed",
            execution_id="tool-first",
            tool_call_id="tool-call-first",
            tool_name="search",
            output="result",
            observed_at=started + timedelta(milliseconds=160),
            monotonic_ns=6,
        ),
        ModelCallObservation(
            identity=identity,
            phase="started",
            call_id="model-second",
            messages=(
                NativeMessageRecord(message_type="system", content="updated system"),
                NativeMessageRecord(message_type="human", content="question"),
            ),
            observed_at=started + timedelta(milliseconds=175),
            monotonic_ns=7,
        ),
        ModelCallObservation(
            identity=identity,
            phase="completed",
            call_id="model-second",
            observed_at=started + timedelta(milliseconds=220),
            monotonic_ns=8,
        ),
        RunTerminalObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=started + timedelta(milliseconds=230),
            monotonic_ns=9,
        ),
        RunClosedObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=started + timedelta(milliseconds=231),
            monotonic_ns=10,
        ),
    )
    for observation in observations:
        await session.observe(observation)
    await session.aclose()

    graph = await tracer.query(identity.thread, limit=100)
    contexts = [node for node in graph.nodes if node.kind is TraceGraphNodeKind.CONTEXT]

    assert [
        (node.started_at, node.completed_at, node.content) for node in contexts
    ] == [
        (
            started + timedelta(milliseconds=1),
            started + timedelta(milliseconds=20),
            "final system",
        ),
        (
            started + timedelta(milliseconds=160),
            started + timedelta(milliseconds=175),
            "updated system",
        ),
    ]
    model_requests = [
        node.request for node in graph.nodes if node.kind is TraceGraphNodeKind.MODEL
    ]
    assert len(model_requests) == 2


async def test_resumed_subagent_context_excludes_prior_wait_time() -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    thread_id = "thread-resumed-subagent-context"
    parent_identity = RunIdentity(
        namespace="test", thread_id=thread_id, run_id="run-parent"
    )
    parent_source = RunSourceContext(
        identity=parent_identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"role": "user", "content": "delegate"}]},
        config={},
    )
    parent = await tracer.open_run(parent_source)
    started = datetime(2026, 9, 5, 1, 0, tzinfo=UTC)
    child_namespace = ("tools:delegation",)
    parent_observations: tuple[RuntimeObservation, ...] = (
        RunStartedObservation(
            identity=parent_identity,
            observed_at=started,
            monotonic_ns=1_000,
        ),
        RunInputObservation(
            identity=parent_identity,
            source=parent_source,
            observed_at=started + timedelta(milliseconds=1),
            monotonic_ns=1_001,
        ),
        NativeTaskObservation(
            identity=parent_identity,
            graph_namespace=(),
            phase="start",
            task_id="delegation",
            name="tools",
            input=[
                {
                    "name": "task",
                    "id": "task-call",
                    "args": {
                        "description": "Delegate the work",
                        "subagent_type": "researcher",
                    },
                }
            ],
            observed_at=started + timedelta(milliseconds=5),
            monotonic_ns=1_005,
        ),
        NativeTaskObservation(
            identity=parent_identity,
            graph_namespace=child_namespace,
            phase="start",
            task_id="child-model-task",
            name="model",
            input={},
            observed_at=started + timedelta(milliseconds=8),
            monotonic_ns=1_008,
        ),
        RunTerminalObservation(
            identity=parent_identity,
            outcome="interrupted",
            observed_at=started + timedelta(milliseconds=20),
            monotonic_ns=1_020,
        ),
        RunClosedObservation(
            identity=parent_identity,
            outcome="interrupted",
            observed_at=started + timedelta(milliseconds=21),
            monotonic_ns=1_021,
        ),
    )
    for observation in parent_observations:
        await parent.observe(observation)
    await parent.aclose()

    resumed_identity = RunIdentity(
        namespace="test", thread_id=thread_id, run_id="run-resumed"
    )
    resumed_source = RunSourceContext(
        identity=resumed_identity,
        runtime_profile="deepagents-v2",
        input_kind="resume",
        parent_run_id=parent_identity.run_id,
        input=None,
        config={},
    )
    resumed = await tracer.open_run(resumed_source)
    resumed_at = started + timedelta(minutes=5)
    resumed_observations: tuple[RuntimeObservation, ...] = (
        RunStartedObservation(
            identity=resumed_identity,
            observed_at=resumed_at,
            monotonic_ns=1,
        ),
        RunInputObservation(
            identity=resumed_identity,
            source=resumed_source,
            observed_at=resumed_at + timedelta(milliseconds=1),
            monotonic_ns=2,
        ),
        ModelCallObservation(
            identity=resumed_identity,
            graph_namespace=child_namespace,
            phase="started",
            call_id="resumed-child-model",
            messages=(
                NativeMessageRecord(
                    message_type="system",
                    content="resumed child system",
                ),
            ),
            observed_at=resumed_at + timedelta(milliseconds=18),
            monotonic_ns=3,
        ),
        ModelCallObservation(
            identity=resumed_identity,
            graph_namespace=child_namespace,
            phase="completed",
            call_id="resumed-child-model",
            observed_at=resumed_at + timedelta(milliseconds=40),
            monotonic_ns=4,
        ),
        NativeTaskObservation(
            identity=resumed_identity,
            graph_namespace=(),
            phase="result",
            task_id="delegation",
            name="tools",
            result={},
            observed_at=resumed_at + timedelta(milliseconds=45),
            monotonic_ns=5,
        ),
        RunTerminalObservation(
            identity=resumed_identity,
            outcome="succeeded",
            observed_at=resumed_at + timedelta(milliseconds=50),
            monotonic_ns=6,
        ),
        RunClosedObservation(
            identity=resumed_identity,
            outcome="succeeded",
            observed_at=resumed_at + timedelta(milliseconds=51),
            monotonic_ns=7,
        ),
    )
    for observation in resumed_observations:
        await resumed.observe(observation)
    await resumed.aclose()

    graph = await tracer.query(
        ThreadIdentity(namespace="test", thread_id=thread_id),
        head_run_id=resumed_identity.run_id,
        limit=100,
    )
    context = next(
        node
        for node in graph.nodes
        if node.kind is TraceGraphNodeKind.CONTEXT
        and node.content == "resumed child system"
    )

    assert context.started_at == resumed_at + timedelta(milliseconds=1)
    assert context.completed_at == resumed_at + timedelta(milliseconds=18)
    assert context.content == "resumed child system"
    assert context.parent_subagent_id is not None


async def test_parallel_subagents_keep_independent_context_boundaries() -> None:
    tracer = Tracer(store=InMemoryTraceStore())
    identity = RunIdentity(
        namespace="test", thread_id="thread-parallel-context", run_id="run-parallel"
    )
    source = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"role": "user", "content": "delegate twice"}]},
        config={},
    )
    session = await tracer.open_run(source)
    started = datetime(2026, 9, 5, 1, 30, tzinfo=UTC)
    first_namespace = ("tools:delegation:0",)
    second_namespace = ("tools:delegation:1",)
    observations: tuple[RuntimeObservation, ...] = (
        RunStartedObservation(
            identity=identity,
            observed_at=started,
            monotonic_ns=1,
        ),
        RunInputObservation(
            identity=identity,
            source=source,
            observed_at=started + timedelta(milliseconds=1),
            monotonic_ns=2,
        ),
        NativeTaskObservation(
            identity=identity,
            graph_namespace=(),
            phase="start",
            task_id="delegation",
            name="tools",
            input=[
                {
                    "name": "task",
                    "id": "task-first",
                    "args": {
                        "description": "First task",
                        "subagent_type": "researcher",
                    },
                },
                {
                    "name": "task",
                    "id": "task-second",
                    "args": {
                        "description": "Second task",
                        "subagent_type": "reviewer",
                    },
                },
            ],
            observed_at=started + timedelta(milliseconds=5),
            monotonic_ns=3,
        ),
        NativeTaskObservation(
            identity=identity,
            graph_namespace=first_namespace,
            phase="start",
            task_id="first-model-task",
            name="model",
            input={},
            observed_at=started + timedelta(milliseconds=10),
            monotonic_ns=4,
        ),
        NativeTaskObservation(
            identity=identity,
            graph_namespace=second_namespace,
            phase="start",
            task_id="second-model-task",
            name="model",
            input={},
            observed_at=started + timedelta(milliseconds=15),
            monotonic_ns=5,
        ),
        ModelCallObservation(
            identity=identity,
            graph_namespace=second_namespace,
            phase="started",
            call_id="second-model",
            messages=(
                NativeMessageRecord(message_type="system", content="second system"),
            ),
            observed_at=started + timedelta(milliseconds=25),
            monotonic_ns=6,
        ),
        ModelCallObservation(
            identity=identity,
            graph_namespace=first_namespace,
            phase="started",
            call_id="first-model",
            messages=(
                NativeMessageRecord(message_type="system", content="first system"),
            ),
            observed_at=started + timedelta(milliseconds=30),
            monotonic_ns=7,
        ),
        ModelCallObservation(
            identity=identity,
            graph_namespace=second_namespace,
            phase="completed",
            call_id="second-model",
            observed_at=started + timedelta(milliseconds=40),
            monotonic_ns=8,
        ),
        ModelCallObservation(
            identity=identity,
            graph_namespace=first_namespace,
            phase="completed",
            call_id="first-model",
            observed_at=started + timedelta(milliseconds=45),
            monotonic_ns=9,
        ),
        NativeTaskObservation(
            identity=identity,
            graph_namespace=(),
            phase="result",
            task_id="delegation",
            name="tools",
            result={},
            observed_at=started + timedelta(milliseconds=50),
            monotonic_ns=10,
        ),
        RunTerminalObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=started + timedelta(milliseconds=51),
            monotonic_ns=11,
        ),
        RunClosedObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=started + timedelta(milliseconds=52),
            monotonic_ns=12,
        ),
    )
    for observation in observations:
        await session.observe(observation)
    await session.aclose()

    graph = await tracer.query(identity.thread, limit=100)
    contexts = {
        node.content: node
        for node in graph.nodes
        if node.kind is TraceGraphNodeKind.CONTEXT
    }

    assert contexts["first system"].started_at == started + timedelta(milliseconds=10)
    assert contexts["second system"].started_at == started + timedelta(milliseconds=15)
    assert contexts["first system"].parent_subagent_id is not None
    assert contexts["second system"].parent_subagent_id is not None
    assert (
        contexts["first system"].parent_subagent_id
        != contexts["second system"].parent_subagent_id
    )


async def test_non_subagent_graph_context_uses_the_turn_root_boundary() -> None:
    tracer = Tracer(store=InMemoryTraceStore())
    identity = RunIdentity(
        namespace="test", thread_id="thread-planning-context", run_id="run-planning"
    )
    source = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"role": "user", "content": "make a plan"}]},
        config={},
    )
    session = await tracer.open_run(source)
    started = datetime(2026, 9, 5, 1, 45, tzinfo=UTC)
    planning_namespace = ("planning:model",)
    observations: tuple[RuntimeObservation, ...] = (
        RunStartedObservation(
            identity=identity,
            observed_at=started,
            monotonic_ns=1,
        ),
        RunInputObservation(
            identity=identity,
            source=source,
            observed_at=started + timedelta(milliseconds=1),
            monotonic_ns=2,
        ),
        ModelCallObservation(
            identity=identity,
            graph_namespace=planning_namespace,
            phase="started",
            call_id="planning-model",
            messages=(
                NativeMessageRecord(message_type="system", content="planning system"),
            ),
            observed_at=started + timedelta(milliseconds=15),
            monotonic_ns=3,
        ),
        ModelCallObservation(
            identity=identity,
            graph_namespace=planning_namespace,
            phase="completed",
            call_id="planning-model",
            observed_at=started + timedelta(milliseconds=40),
            monotonic_ns=4,
        ),
        RunTerminalObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=started + timedelta(milliseconds=41),
            monotonic_ns=5,
        ),
        RunClosedObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=started + timedelta(milliseconds=42),
            monotonic_ns=6,
        ),
    )
    for observation in observations:
        await session.observe(observation)
    await session.aclose()

    graph = await tracer.query(identity.thread, limit=100)
    context = next(
        node for node in graph.nodes if node.kind is TraceGraphNodeKind.CONTEXT
    )

    assert context.graph_namespace == planning_namespace
    assert context.parent_subagent_id is None
    assert context.started_at == started + timedelta(milliseconds=1)
    assert context.completed_at == started + timedelta(milliseconds=15)


async def test_cancelled_model_keeps_completed_context_without_system_message() -> None:
    tracer = Tracer(store=InMemoryTraceStore())
    identity = RunIdentity(
        namespace="test", thread_id="thread-cancelled-context", run_id="run-cancelled"
    )
    source = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"role": "user", "content": "question"}]},
        config={},
    )
    session = await tracer.open_run(source)
    started = datetime(2026, 9, 5, 2, 0, tzinfo=UTC)
    observations: tuple[RuntimeObservation, ...] = (
        RunStartedObservation(
            identity=identity,
            observed_at=started,
            monotonic_ns=1,
        ),
        RunInputObservation(
            identity=identity,
            source=source,
            observed_at=started + timedelta(milliseconds=1),
            monotonic_ns=2,
        ),
        ModelCallObservation(
            identity=identity,
            phase="started",
            call_id="cancelled-model",
            messages=(NativeMessageRecord(message_type="human", content="question"),),
            observed_at=started + timedelta(milliseconds=12),
            monotonic_ns=3,
        ),
        ModelCallObservation(
            identity=identity,
            phase="cancelled",
            call_id="cancelled-model",
            observed_at=started + timedelta(milliseconds=30),
            monotonic_ns=4,
        ),
        RunTerminalObservation(
            identity=identity,
            outcome="cancelled",
            observed_at=started + timedelta(milliseconds=31),
            monotonic_ns=5,
        ),
        RunClosedObservation(
            identity=identity,
            outcome="cancelled",
            observed_at=started + timedelta(milliseconds=32),
            monotonic_ns=6,
        ),
    )
    for observation in observations:
        await session.observe(observation)
    await session.aclose()

    graph = await tracer.query(identity.thread, limit=100)
    context = next(
        node for node in graph.nodes if node.kind is TraceGraphNodeKind.CONTEXT
    )
    model = next(node for node in graph.nodes if node.kind is TraceGraphNodeKind.MODEL)

    assert context.status is TraceGraphNodeStatus.SUCCEEDED
    assert context.started_at == started + timedelta(milliseconds=1)
    assert context.completed_at == started + timedelta(milliseconds=12)
    assert context.content is None
    assert context.content_omitted is False
    assert model.status is TraceGraphNodeStatus.CANCELLED


async def test_model_retry_starts_context_at_the_failed_attempt_boundary() -> None:
    tracer = Tracer(store=InMemoryTraceStore())
    identity = RunIdentity(
        namespace="test", thread_id="thread-retry-context", run_id="run-retry"
    )
    source = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"role": "user", "content": "retry"}]},
        config={},
    )
    session = await tracer.open_run(source)
    started = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)
    observations: tuple[RuntimeObservation, ...] = (
        RunStartedObservation(
            identity=identity,
            observed_at=started,
            monotonic_ns=1,
        ),
        RunInputObservation(
            identity=identity,
            source=source,
            observed_at=started + timedelta(milliseconds=1),
            monotonic_ns=2,
        ),
        ModelCallObservation(
            identity=identity,
            phase="started",
            call_id="model-attempt-1",
            messages=(
                NativeMessageRecord(message_type="system", content="attempt one"),
            ),
            observed_at=started + timedelta(milliseconds=10),
            monotonic_ns=3,
        ),
        ModelCallObservation(
            identity=identity,
            phase="failed",
            call_id="model-attempt-1",
            error_type="builtins.RuntimeError",
            observed_at=started + timedelta(milliseconds=25),
            monotonic_ns=4,
        ),
        ModelCallObservation(
            identity=identity,
            phase="started",
            call_id="model-attempt-2",
            messages=(
                NativeMessageRecord(message_type="system", content="attempt two"),
            ),
            observed_at=started + timedelta(milliseconds=40),
            monotonic_ns=5,
        ),
        ModelCallObservation(
            identity=identity,
            phase="completed",
            call_id="model-attempt-2",
            observed_at=started + timedelta(milliseconds=60),
            monotonic_ns=6,
        ),
        RunTerminalObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=started + timedelta(milliseconds=61),
            monotonic_ns=7,
        ),
        RunClosedObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=started + timedelta(milliseconds=62),
            monotonic_ns=8,
        ),
    )
    for observation in observations:
        await session.observe(observation)
    await session.aclose()

    graph = await tracer.query(identity.thread, limit=100)
    contexts = sorted(
        (node for node in graph.nodes if node.kind is TraceGraphNodeKind.CONTEXT),
        key=lambda node: node.started_at,
    )

    assert [
        (node.started_at, node.completed_at, node.content) for node in contexts
    ] == [
        (
            started + timedelta(milliseconds=1),
            started + timedelta(milliseconds=10),
            "attempt one",
        ),
        (
            started + timedelta(milliseconds=25),
            started + timedelta(milliseconds=40),
            "attempt two",
        ),
    ]


@pytest.mark.parametrize(
    "runtime_profile",
    [DeepAgentsV2RuntimeProfile(), DeepAgentsV3RuntimeProfile()],
    ids=["v2-astream", "v3-astream-events"],
)
async def test_managed_ainvoke_builds_the_same_context_boundary(
    runtime_profile: DeepAgentsV2RuntimeProfile | DeepAgentsV3RuntimeProfile,
) -> None:
    tracer = Tracer(store=InMemoryTraceStore())
    tinkerfin = (
        TinkerFin(runtime_profile=runtime_profile)
        .with_namespace("test")
        .with_observer(tracer)
    )
    definition = tinkerfin.build(
        model=_SubagentToolBindingModel(responses=[AIMessage(content="done")]),
        tools=[],
        system_prompt="Use concise answers.",
    )
    identity = RunIdentity(
        namespace="test",
        thread_id=f"thread-context-{runtime_profile.profile_id}",
        run_id=f"run-context-{runtime_profile.profile_id}",
    )

    await definition.ainvoke(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input={"messages": [HumanMessage(content="question", id="user-question")]},
    )

    graph = await tracer.query(identity.thread, limit=100)
    context = next(
        node for node in graph.nodes if node.kind is TraceGraphNodeKind.CONTEXT
    )
    model = next(node for node in graph.nodes if node.kind is TraceGraphNodeKind.MODEL)

    assert context.completed_at is not None
    assert context.completed_at == model.started_at
    assert context.started_at <= context.completed_at
    assert isinstance(context.content, str)
    assert "Use concise answers." in context.content
    assert any(
        node.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE and node.content == "done"
        for node in graph.nodes
    )


@pytest.mark.parametrize(
    "runtime_profile",
    [DeepAgentsV2RuntimeProfile(), DeepAgentsV3RuntimeProfile()],
    ids=["v2-astream", "v3-astream-events"],
)
async def test_managed_ainvoke_stores_one_delegated_task_payload(
    runtime_profile: DeepAgentsV2RuntimeProfile | DeepAgentsV3RuntimeProfile,
) -> None:
    tracer = Tracer(store=InMemoryTraceStore())
    tinkerfin = (
        TinkerFin(runtime_profile=runtime_profile)
        .with_namespace("test")
        .with_observer(tracer)
    )
    task_description = "Complete the delegated work"
    definition = tinkerfin.build(
        model=_SubagentToolBindingModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": task_description,
                                "subagent_type": "researcher",
                            },
                            "id": "call-task",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="root done"),
            ]
        ),
        tools=[],
        subagents=[
            {
                "name": "researcher",
                "description": "Complete delegated work",
                "system_prompt": "Return the result.",
                "model": _SubagentToolBindingModel(
                    responses=[AIMessage(content="child done")]
                ),
                "tools": [],
            }
        ],
    )
    identity = RunIdentity(
        namespace="test",
        thread_id=f"thread-subagent-{runtime_profile.profile_id}",
        run_id=f"run-subagent-{runtime_profile.profile_id}",
    )

    await definition.ainvoke(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input={"messages": [HumanMessage(content="delegate", id="user-delegate")]},
    )

    thread = await tracer.get(identity.thread)
    events = (await thread.events(limit=100)).items
    subagent = next(
        event.fact
        for event in events
        if isinstance(event.fact, SubagentFact) and event.fact.phase == "started"
    )
    scoped_user_messages = [
        event.fact
        for event in events
        if isinstance(event.fact, MessageFact)
        and event.fact.graph_namespace == subagent.graph_namespace
        and event.fact.role == "user"
    ]
    subagent_node = next(
        node for node in thread.graph.nodes if node.kind is TraceGraphNodeKind.SUBAGENT
    )
    input_node = next(
        node
        for node in thread.graph.nodes
        if node.kind is TraceGraphNodeKind.HUMAN_MESSAGE
        and node.parent_subagent_id == subagent_node.id
    )

    assert subagent.input is not None
    assert subagent.input.value == {
        "description": task_description,
        "subagent_type": "researcher",
    }
    assert scoped_user_messages == []
    assert input_node.content == task_description


setattr(
    _Graph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


def _definition(
    monkeypatch: pytest.MonkeyPatch,
    graph: _Graph,
    *,
    tracer: Tracer,
    runtime_profile: DeepAgentsV2RuntimeProfile | None = None,
) -> AgentRuntime[None]:
    def build(*_args: object, **_kwargs: object) -> _Graph:
        return graph

    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph",
        build,
    )
    tinkerfin = (
        TinkerFin().with_namespace("test")
        if runtime_profile is None
        else TinkerFin(runtime_profile=runtime_profile).with_namespace("test")
    )
    factory = cast(
        Callable[..., object],
        tinkerfin.with_observer(tracer).build,
    )
    definition = factory(model="provider:model", tools=[])
    if not isinstance(definition, AgentRuntime):
        raise TypeError("patched factory must return AgentRuntime")
    return definition


def _parts() -> tuple[Mapping[str, object], ...]:
    tool_call = {
        "name": "write_todos",
        "args": {
            "todos": [
                {"content": "Inspect", "status": "in_progress"},
                {"content": "Verify", "status": "pending"},
            ]
        },
        "id": "call-write-todos",
        "type": "tool_call",
    }
    return (
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(id="assistant-1", content="hello "),
                {"langgraph_node": "model", "lc_agent_name": "main"},
            ),
        },
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-1",
                    content="world",
                    tool_call_chunks=[
                        {
                            "name": "write_todos",
                            "args": (
                                '{"todos":[{"content":"Inspect","status":'
                                '"in_progress"},{"content":"Verify","status":"pending"}]}'
                            ),
                            "id": "call-write-todos",
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                ),
                {"langgraph_node": "model", "lc_agent_name": "main"},
            ),
        },
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-tools",
                "name": "tools",
                "input": [tool_call],
                "triggers": ("branch:to:tools",),
            },
        },
        {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage(
                    id="tool-message-1",
                    name="write_todos",
                    content="Updated todo list",
                    tool_call_id="call-write-todos",
                ),
                {"langgraph_node": "tools", "lc_agent_name": "main"},
            ),
        },
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-tools",
                "name": "tools",
                "error": None,
                "result": {"messages": []},
                "interrupts": [],
            },
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    HumanMessage(id="user-1", content="do work"),
                    AIMessage(
                        id="assistant-1",
                        content="hello world",
                        tool_calls=[tool_call],
                    ),
                    ToolMessage(
                        id="tool-message-1",
                        name="write_todos",
                        content="Updated todo list",
                        tool_call_id="call-write-todos",
                    ),
                ],
                "todos": tool_call["args"]["todos"],
            },
            "interrupts": (),
        },
    )


async def test_managed_run_is_queryable_before_native_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ready boundary commits Run input before the first Graph pull."""

    graph = _Graph(_parts())

    def build(*_args: object, **_kwargs: object) -> _Graph:
        return graph

    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph",
        build,
    )
    tracer = Tracer()
    tinkerfin = TinkerFin().with_namespace("test").with_observer(tracer)
    definition = tinkerfin.build(model="provider:model", tools=[])
    identity = RunIdentity(
        namespace="test", thread_id="thread-ready", run_id="run-ready"
    )

    stream = definition.open_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input=InputAgentState(
            messages=[HumanMessage(id="user-ready", content="Start the work")]
        ),
    )

    await stream.messaging_owner_preflight()
    thread = await tracer.get(identity.thread, head_run_id=identity.run_id)
    assert thread.head_run_id == identity.run_id
    assert [(message.role, message.content) for message in thread.messages] == [
        ("user", "Start the work")
    ]
    assert thread.status.execution == "running"
    query = await tracer.query(identity.thread, head_run_id=identity.run_id)
    assert query.nodes

    await stream.aclose()
    settled = await tracer.get(identity.thread, head_run_id=identity.run_id)
    assert settled.status.execution == "cancelled"


async def test_in_memory_graph_index_rebuild_matches_online_reduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    definition = _definition(monkeypatch, _Graph(_parts()), tracer=tracer)
    identity = RunIdentity(
        namespace="test", thread_id="thread-graph-index", run_id="run-graph-index"
    )
    runtime = definition

    async for _part in runtime.open_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input=InputAgentState(messages=[HumanMessage(id="user-1", content="do work")]),
    ):
        pass

    snapshot = await store.snapshot(identity.thread)
    where = TraceGraphFilter()
    before = await store.query_trace_graph(
        snapshot.key,
        run_ids=(identity.run_id,),
        where=where,
        limit=100,
    )
    rebuilt_count = await store.rebuild_trace_graph(snapshot.key)
    after = await store.query_trace_graph(
        snapshot.key,
        run_ids=(identity.run_id,),
        where=where,
        limit=100,
    )

    assert rebuilt_count == len(before.nodes)
    assert after == before
    assert {node.kind for node in after.nodes}.issuperset(
        {
            TraceGraphNodeKind.HUMAN_MESSAGE,
            TraceGraphNodeKind.ASSISTANT_MESSAGE,
            TraceGraphNodeKind.TOOL,
        }
    )
    graph = await tracer.query(identity.thread, limit=100)
    assert graph.nodes[0].kind is TraceGraphNodeKind.HUMAN_MESSAGE
    assert [node.kind for node in graph.nodes].count(TraceGraphNodeKind.TOOL) == 1


async def test_runtime_trace_projects_messages_tree_state_todos_and_safe_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = Tracer(projections=(FactCountProjection(),))
    definition = _definition(monkeypatch, _Graph(_parts()), tracer=tracer)
    runtime = definition

    delivered = [
        part
        async for part in runtime.open_run(
            thread_id=RunIdentity(
                namespace="test", thread_id="thread-1", run_id="run-1"
            ).thread_id,
            run_id=RunIdentity(
                namespace="test", thread_id="thread-1", run_id="run-1"
            ).run_id,
            input=InputAgentState(
                messages=[HumanMessage(id="user-1", content="do work")]
            ),
            config=cast(RunnableConfig, {"configurable": {"thread_id": "thread-1"}}),
        )
    ]
    thread = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-1"),
        projections=("fact_counts",),
    )

    assert delivered == list(_parts())
    assert [(message.role, message.content) for message in thread.messages] == [
        ("user", "do work"),
        ("assistant", "hello world"),
        ("tool", "Updated todo list"),
    ]
    assert thread.messages[2].content_omitted is False
    assert [message.trace_seq for message in thread.messages] == sorted(
        message.trace_seq for message in thread.messages
    )
    assert thread.state.root["todos"] == [
        {"content": "Inspect", "status": "in_progress"},
        {"content": "Verify", "status": "pending"},
    ]
    assert any(
        node.kind == "tool" and node.name == "write_todos"
        for node in thread.graph.nodes
    )
    indexed = (
        await tracer.query(
            ThreadIdentity(namespace="test", thread_id="thread-1"),
            where=TraceGraphFilter(),
            limit=200,
        )
    ).snapshot
    assert indexed.next_cursor is None
    assert thread.graph == TraceGraph.model_validate(
        indexed.model_dump(exclude={"next_cursor"})
    )
    assistant = next(
        message for message in thread.messages if message.role == "assistant"
    )
    tool = next(node for node in thread.graph.nodes if node.kind == "tool")
    assert assistant.trace_seq < tool.started_seq
    assert thread.status.execution == "succeeded"
    assert thread.completeness.payload_omitted is False
    result = thread.projections["fact_counts"]
    assert isinstance(result, FactCountResult)
    assert result.counts["tool"] == 4
    page = await thread.events(limit=100)
    turn = next(event.fact for event in page.items if isinstance(event.fact, TurnFact))
    tool_facts = [
        event.fact for event in page.items if isinstance(event.fact, ToolFact)
    ]
    assistant_facts = [
        event.fact
        for event in page.items
        if isinstance(event.fact, MessageFact)
        and event.fact.source_message_id == "assistant-1"
    ]
    state_facts = [
        event.fact for event in page.items if isinstance(event.fact, StateRevisionFact)
    ]
    run_facts = [event.fact for event in page.items if isinstance(event.fact, RunFact)]
    assert turn.user_message_id == "user-1"
    assert [fact.phase for fact in tool_facts] == [
        "started",
        "arguments",
        "completed",
        "result",
    ]
    assert [fact.phase for fact in assistant_facts] == ["started", "reconciled"]
    assert assistant_facts[-1].content is not None
    assert assistant_facts[-1].content.value == "hello world"
    assert all(fact.graph_namespace == () for fact in tool_facts)
    assert any(
        isinstance(fact.changes.value, dict) and "todos" in fact.changes.value
        for fact in state_facts
    )
    assert [fact.phase for fact in run_facts] == [
        "started",
        "input",
        "terminal",
        "closed",
    ]
    assert all(event.fact.kind not in {"agui", "messaging"} for event in page.items)


async def test_propagated_subagent_interrupt_uses_the_deepest_trace_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = ("tools:subagent-task",)
    interrupt = Interrupt(
        id="child-review",
        value={
            "action_requests": [
                {
                    "name": "write_file",
                    "args": {"file_path": "/result.txt", "content": "result"},
                }
            ],
            "review_configs": [
                {
                    "action_name": "write_file",
                    "allowed_decisions": ["approve", "reject"],
                }
            ],
        },
    )
    child_message = AIMessage(
        id="child-assistant",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "/result.txt", "content": "result"},
                "id": "child-write",
                "type": "tool_call",
            }
        ],
    )
    root_message = AIMessage(
        id="root-assistant",
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {
                    "description": "Write the reviewed result",
                    "subagent_type": "general-purpose",
                },
                "id": "parent-task",
                "type": "tool_call",
            }
        ],
    )
    parts: tuple[Mapping[str, object], ...] = (
        {
            "type": "values",
            "ns": namespace,
            "data": {
                "messages": [child_message],
            },
            "interrupts": (interrupt,),
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    HumanMessage(id="user", content="Delegate"),
                    root_message,
                ],
            },
            "interrupts": (interrupt,),
        },
    )
    tracer = Tracer()
    definition = _definition(monkeypatch, _Graph(parts), tracer=tracer)
    runtime = definition

    delivered = [
        part
        async for part in runtime.open_run(
            thread_id=RunIdentity(
                namespace="test", thread_id="thread-child-review", run_id="run-review"
            ).thread_id,
            run_id=RunIdentity(
                namespace="test", thread_id="thread-child-review", run_id="run-review"
            ).run_id,
            input=InputAgentState(
                messages=[HumanMessage(id="user", content="Delegate")]
            ),
            config=cast(
                RunnableConfig, {"configurable": {"thread_id": "thread-child-review"}}
            ),
        )
    ]
    thread = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-child-review")
    )

    assert delivered == list(parts)
    assert [
        (interaction.graph_namespace, interaction.source_id, interaction.status)
        for interaction in thread.interactions
    ] == [(namespace, "child-review", "pending")]
    assert thread.status.execution == "waiting"


async def test_locked_subagent_hitl_remains_an_interrupt_with_tracing() -> None:
    model = _SubagentToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Run the reviewed child Tool",
                            "subagent_type": "general-purpose",
                        },
                        "id": "parent-task-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "reviewed_child_tool",
                        "args": {"value": "child"},
                        "id": "child-reviewed-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="child done"),
            AIMessage(content="root done"),
        ]
    )
    tracer = Tracer(store=InMemoryTraceStore())
    tinkerfin = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
    )
    definition = tinkerfin.build(
        model=model,
        tools=[reviewed_child_tool],
        interrupt_on={"reviewed_child_tool": {"allowed_decisions": ["approve"]}},
    )
    identity = RunIdentity(
        namespace="test", thread_id="thread-real-child-review", run_id="run-review"
    )
    stream = definition.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input={"messages": [HumanMessage(content="Delegate", id="user")]},
    )
    events = [event async for event in stream]
    terminal = events[-1]
    thread = await tracer.get(identity.thread)

    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None
    assert terminal.outcome.type == "interrupt"
    assert len(terminal.outcome.interrupts) == 1
    assert stream.error is None
    assert len(thread.interactions) == 1
    assert thread.interactions[0].graph_namespace
    assert thread.interactions[0].status == "pending"
    assert thread.status.execution == "waiting"
    graph = await tracer.query(identity.thread, limit=200)
    assert graph.nodes
    assert all(node.failure is None for node in graph.nodes)
    assert not any(
        node.status in {TraceGraphNodeStatus.RUNNING, TraceGraphNodeStatus.FAILED}
        for node in graph.nodes
    )


async def test_canonical_graph_links_messages_models_and_one_aggregated_tool() -> None:
    @tool
    async def graph_tool(value: str) -> str:
        """Return one Graph test value.

        Args:
            value: Value to return.

        Returns:
            The supplied value.
        """

        return value

    model = _SubagentToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "graph_tool",
                        "args": {"value": "kept"},
                        "id": "graph-tool-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    tracer = Tracer(store=InMemoryTraceStore())
    tinkerfin = TinkerFin().with_namespace("test").with_observer(tracer)
    definition = tinkerfin.build(model=model, tools=[graph_tool])
    identity = RunIdentity(
        namespace="test", thread_id="thread-canonical-graph", run_id="run-graph"
    )
    stream = definition.open_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input={"messages": [HumanMessage(content="Use the Tool", id="human-graph")]},
    )
    async for _part in stream:
        pass

    semantic = await tracer.query(identity.thread, limit=200)
    assert semantic.nodes[0].kind is TraceGraphNodeKind.HUMAN_MESSAGE
    assert [node.kind for node in semantic.nodes].count(TraceGraphNodeKind.TOOL) == 1
    assert [node.kind for node in semantic.nodes].count(
        TraceGraphNodeKind.ASSISTANT_MESSAGE
    ) == 2
    assert all(
        node.parent_subagent_id is None
        or node.parent_subagent_id in {candidate.id for candidate in semantic.nodes}
        for node in semantic.nodes
    )
    tool_node = next(
        node for node in semantic.nodes if node.kind is TraceGraphNodeKind.TOOL
    )
    assert tool_node.request == {"value": "kept"}
    assert tool_node.result is not None
    assert not tool_node.link_issues
    contexts = [
        node for node in semantic.nodes if node.kind is TraceGraphNodeKind.CONTEXT
    ]
    assert contexts
    assert all(
        node.completed_at is not None and node.completed_at >= node.started_at
        for node in contexts
    )
    assert not any(node.name == "ToolMessage" for node in semantic.nodes)


async def test_real_subagent_cancellation_settles_every_child_graph_node() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    @tool
    async def wait_for_child_cancellation() -> str:
        """Wait until the owning Runtime cancels the child Tool.

        Returns:
            An unreachable value.
        """

        entered.set()
        await release.wait()
        return "unreachable"

    model = _SubagentToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Wait inside the child Agent",
                            "subagent_type": "general-purpose",
                        },
                        "id": "cancel-parent-task",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "wait_for_child_cancellation",
                        "args": {},
                        "id": "cancel-child-tool",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    tracer = Tracer(store=InMemoryTraceStore())
    tinkerfin = TinkerFin().with_namespace("test").with_observer(tracer)
    definition = tinkerfin.build(model=model, tools=[wait_for_child_cancellation])
    identity = RunIdentity(
        namespace="test", thread_id="thread-child-cancel", run_id="run-cancel"
    )
    stream = definition.open_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input={"messages": [HumanMessage(content="Delegate", id="user")]},
    )

    async def consume() -> None:
        async for _part in stream:
            pass

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    graph = await tracer.query(
        identity.thread,
        where=TraceGraphFilter(),
        limit=200,
    )
    child_nodes = tuple(
        node
        for node in graph.nodes
        if node.kind
        in {
            TraceGraphNodeKind.SUBAGENT,
            TraceGraphNodeKind.TOOL,
        }
    )
    assert child_nodes
    failures = tuple(node for node in graph.nodes if node.failure is not None)
    assert not failures, [
        (node.kind, node.name, node.status, node.failure) for node in failures
    ]
    assert not any(
        node.status in {TraceGraphNodeStatus.RUNNING, TraceGraphNodeStatus.WAITING}
        for node in child_nodes
    )
    assert any(
        node.kind is TraceGraphNodeKind.SUBAGENT
        and node.status is TraceGraphNodeStatus.CANCELLED
        for node in child_nodes
    )


async def test_branches_require_an_explicit_head_and_window_loads_older_turns() -> None:
    tracer = Tracer()
    await _record_run(tracer, run_id="root")
    await _record_run(tracer, run_id="branch-a", parent_run_id="root")
    await _record_run(tracer, run_id="branch-b", parent_run_id="root")

    with pytest.raises(AmbiguousTraceHead):
        await tracer.get(ThreadIdentity(namespace="test", thread_id="thread-lineage"))
    branch = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage"),
        head_run_id="branch-a",
        limit=1,
    )
    assert branch.head_run_id == "branch-a"
    assert branch.has_older is True
    assert len(branch.graph.turns) == 1
    await branch.load_older(limit=1)
    assert branch.has_older is False
    assert len(branch.graph.turns) == 2


async def test_incremental_core_checkpoint_reads_only_the_visible_turn_window() -> None:
    store = _ReadCountingStore()
    tracer = Tracer(store=store)
    for index in range(8):
        await _record_run(tracer, run_id=f"bounded-{index}")

    store.read_calls.clear()
    thread = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage"), limit=2
    )
    query_calls = tuple(store.read_calls)
    snapshot = await store.snapshot(
        ThreadIdentity(namespace="test", thread_id="thread-lineage")
    )
    full_events = await store.read_events(
        snapshot.key,
        after_seq=0,
        as_of_seq=snapshot.as_of_seq,
        limit=1000,
    )
    baseline = project_core(
        full_events,
        head_run_id=None,
        turn_limit=2,
        active_run_ids=snapshot.active_run_ids,
    )

    assert query_calls == ()
    assert thread.messages == baseline.messages
    assert len(thread.graph.turns) == 2
    assert thread.state == baseline.state
    assert thread.status == baseline.status
    assert thread.completeness == baseline.completeness


async def test_implicit_resume_matches_full_fold_for_every_incremental_batch() -> None:
    store = _ReadCountingStore()
    tracer = Tracer(store=store)
    await _record_run(tracer, run_id="implicit-resume-parent")
    await _record_run(
        tracer,
        run_id="implicit-resume-child",
        input_kind="resume",
    )
    snapshot = await store.snapshot(
        ThreadIdentity(namespace="test", thread_id="thread-lineage")
    )
    events = await store.read_events(
        snapshot.key,
        after_seq=0,
        as_of_seq=snapshot.as_of_seq,
        limit=1000,
    )
    baseline = project_core(
        events,
        head_run_id=None,
        turn_limit=100,
        active_run_ids=snapshot.active_run_ids,
    )

    states = []
    for chunk_size in range(1, len(events) + 1):
        state = empty_core_projection_state()
        for offset in range(0, len(events), chunk_size):
            state = advance_core_projection_state(
                state,
                events[offset : offset + chunk_size],
            )
        states.append(state)
    for split in range(1, len(events)):
        state = advance_core_projection_state(
            empty_core_projection_state(),
            events[:split],
        )
        states.append(advance_core_projection_state(state, events[split:]))

    expected_state = states[0].model_dump(mode="json", by_alias=True)
    for state in states:
        assert state.model_dump(mode="json", by_alias=True) == expected_state
        projected = project_core_checkpoint(
            state,
            head_run_id=None,
            turn_limit=100,
            active_run_ids=snapshot.active_run_ids,
        )
        assert projected.selected_run_ids == baseline.selected_run_ids
        assert projected.selected_head == baseline.selected_head
        assert projected.available_heads == baseline.available_heads
        assert projected.messages == baseline.messages
        assert projected.reasoning == baseline.reasoning
        assert projected.state == baseline.state
        assert projected.interactions == baseline.interactions
        assert projected.summary == baseline.summary
        assert projected.has_older == baseline.has_older

    store.read_calls.clear()
    cached = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage")
    )

    assert store.read_calls == []
    assert cached.completeness == baseline.completeness
    assert cached.completeness.missing_prefix is False


async def test_history_cursor_expands_one_fixed_prefix_after_new_commits() -> None:
    tracer = Tracer()
    for index in range(5):
        await _record_run(tracer, run_id=f"history-{index}")
    first = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage"), limit=2
    )
    cursor = first.history_cursor
    assert cursor is not None

    await _record_run(tracer, run_id="history-newer")
    older = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage"),
        history_cursor=cursor,
        limit=2,
    )

    assert older.as_of_seq == first.as_of_seq
    assert len(older.graph.turns) == 4
    assert all(node.run_id != "history-newer" for node in older.graph.nodes)


async def test_event_pages_are_fixed_as_of_and_follow_returns_semantic_deltas() -> None:
    tracer = Tracer()
    await _record_run(tracer, run_id="first")
    thread = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage")
    )
    first_page = await thread.events(limit=2)
    assert first_page.next_cursor is not None

    follow = thread.follow()
    waiting = asyncio.ensure_future(anext(follow))
    await _record_run(tracer, run_id="second")
    update = await asyncio.wait_for(waiting, timeout=2)

    second_page = await thread.events(cursor=first_page.next_cursor, limit=100)
    assert all(
        item.trace_seq <= first_page.page_as_of_seq for item in second_page.items
    )
    assert update.as_of_seq > thread.as_of_seq
    assert any(fact.kind == "turn.started" for fact in update.facts)
    assert update.status.head_run_id == "second"
    await follow.aclose()


class _ProjectionState(BaseModel):
    count: int = 0


class _ProjectionResult(BaseModel):
    count: int


class _CountingProjection:
    name = "incremental-count"
    state_type = _ProjectionState
    result_type = _ProjectionResult

    def __init__(self) -> None:
        self.apply_calls = 0

    def initial_state(self) -> _ProjectionState:
        return _ProjectionState()

    def apply(
        self,
        state: _ProjectionState,
        fact: TraceSemanticFact,
    ) -> _ProjectionState:
        del fact
        self.apply_calls += 1
        return _ProjectionState(count=state.count + 1)

    def finish(self, state: _ProjectionState) -> _ProjectionResult:
        return _ProjectionResult(count=state.count)


class _FailingProjection:
    name = "failing"
    state_type = _ProjectionState
    result_type = _ProjectionResult

    def initial_state(self) -> _ProjectionState:
        return _ProjectionState()

    def apply(
        self,
        state: _ProjectionState,
        fact: object,
    ) -> _ProjectionState:
        del state, fact
        raise RuntimeError("business projection failed")

    def finish(self, state: _ProjectionState) -> _ProjectionResult:
        return _ProjectionResult(count=state.count)


async def test_custom_projection_checkpoint_reuses_state_and_derives_child_runs() -> (
    None
):
    projection = _CountingProjection()
    tracer = Tracer(projections=(projection,))
    await _record_run(tracer, run_id="projection-root")

    first = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage"),
        projections=(projection.name,),
    )
    first_calls = projection.apply_calls
    assert first_calls > 0
    assert first.projections[projection.name] == _ProjectionResult(count=first_calls)

    projection.apply_calls = 0
    await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage"),
        projections=(projection.name,),
    )
    assert projection.apply_calls == 0

    await _record_run(tracer, run_id="projection-child")
    projection.apply_calls = 0
    child = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage"),
        projections=(projection.name,),
    )
    snapshot = await tracer.store.snapshot(
        ThreadIdentity(namespace="test", thread_id="thread-lineage")
    )
    assert 0 < projection.apply_calls < snapshot.as_of_seq
    result = child.projections[projection.name]
    assert isinstance(result, _ProjectionResult)
    assert result.count == snapshot.as_of_seq


async def test_optional_projection_failure_does_not_change_the_ledger() -> None:
    tracer = Tracer(projections=(_FailingProjection(),))
    await _record_run(tracer, run_id="only")

    with pytest.raises(TraceProjectionFailed):
        await tracer.get(
            ThreadIdentity(namespace="test", thread_id="thread-lineage"),
            projections=("failing",),
        )
    healthy = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-lineage")
    )
    assert healthy.status.execution == "succeeded"
    assert (await healthy.events(limit=100)).items


async def test_requested_projection_names_must_be_unique() -> None:
    tracer = Tracer(projections=(FactCountProjection(),))
    await _record_run(tracer, run_id="only")

    with pytest.raises(ValueError, match="names must be unique"):
        await tracer.get(
            ThreadIdentity(namespace="test", thread_id="thread-lineage"),
            projections=("fact_counts", "fact_counts"),
        )


async def test_tracer_rejects_invalid_extension_boundaries_before_io() -> None:
    invalid: Any = object()

    with pytest.raises(TypeError, match="store must implement"):
        Tracer(store=invalid)
    with pytest.raises(TypeError, match="limits must be"):
        Tracer(limits=invalid)
    with pytest.raises(TypeError, match="capture_policy must be"):
        Tracer(capture_policy=invalid)
    with pytest.raises(TypeError, match="context must be"):
        await Tracer().open_run(invalid)


async def test_live_runtime_objects_strip_private_state_reasoning_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_message = AIMessage(
        id="assistant-private",
        content="visible answer",
        additional_kwargs={"reasoning_content": "provider-private-state"},
    )
    parts: tuple[Mapping[str, object], ...] = (
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-private",
                    content="visible answer",
                    additional_kwargs={"reasoning_content": "provider-private-message"},
                ),
                {"langgraph_node": "model"},
            ),
        },
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-private",
                "name": "model",
                "error": None,
                "result": {"messages": [completed_message]},
                "interrupts": [],
            },
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    HumanMessage(id="user-private", content="question"),
                    completed_message,
                ],
                "_tinkerfin_lineage": {"secret": "private-lineage"},
                "_tinkerfin_resume": {"secret": "private-resume"},
                "reasoning_content": "business-state-value",
                "password": "credential-state-value",
            },
            "interrupts": (),
        },
    )
    tracer = Tracer()
    definition = _definition(monkeypatch, _Graph(parts), tracer=tracer)
    runtime = definition

    assert [
        part
        async for part in runtime.open_run(
            thread_id=RunIdentity(
                namespace="test", thread_id="thread-private", run_id="run-private"
            ).thread_id,
            run_id=RunIdentity(
                namespace="test", thread_id="thread-private", run_id="run-private"
            ).run_id,
            input=InputAgentState(
                messages=[HumanMessage(id="user-private", content="question")]
            ),
            config=cast(
                RunnableConfig, {"configurable": {"thread_id": "thread-private"}}
            ),
        )
    ] == list(parts)
    thread = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-private")
    )
    page = await thread.events(limit=100)
    encoded = page.model_dump_json(by_alias=True)

    assert "provider-private-message" not in encoded
    assert "provider-private-state" not in encoded
    assert "private-lineage" not in encoded
    assert "private-resume" not in encoded
    assert "credential-state-value" not in encoded
    assert thread.state.root["reasoning_content"] == "business-state-value"
    assert thread.state.root["password"] == {"$type": "redacted"}


async def test_runtime_driver_and_tracer_require_both_reasoning_opt_ins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_message = AIMessage(
        id="assistant-reasoning",
        content="visible answer",
        additional_kwargs={"reasoning_content": "first second"},
        response_metadata={"model_provider": "deepseek"},
    )
    parts: tuple[Mapping[str, object], ...] = (
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-reasoning",
                    content="visible answer",
                    additional_kwargs={"reasoning_content": "first"},
                ),
                {"langgraph_node": "model", "ls_provider": "deepseek"},
            ),
        },
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-reasoning",
                    content="",
                    additional_kwargs={"reasoning_content": " second"},
                ),
                {"langgraph_node": "model", "ls_provider": "deepseek"},
            ),
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [completed_message],
                "reasoning_content": "business value",
            },
            "interrupts": (),
        },
    )
    tracer = Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())
    definition = _definition(
        monkeypatch,
        _Graph(parts),
        tracer=tracer,
        runtime_profile=DeepAgentsV2RuntimeProfile(
            reasoning_extractors=(_ProviderReasoningExtractor(),)
        ),
    )
    runtime = definition

    assert [
        part
        async for part in runtime.open_run(
            thread_id=RunIdentity(
                namespace="test", thread_id="thread-reasoning", run_id="run-reasoning"
            ).thread_id,
            run_id=RunIdentity(
                namespace="test", thread_id="thread-reasoning", run_id="run-reasoning"
            ).run_id,
            input=InputAgentState(messages=[]),
            config=cast(
                RunnableConfig, {"configurable": {"thread_id": "thread-reasoning"}}
            ),
        )
    ] == list(parts)
    thread = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-reasoning")
    )
    reasoning_facts = [
        item.fact
        for item in (await thread.events(limit=100)).items
        if isinstance(item.fact, ReasoningFact)
    ]

    assert [fact.phase for fact in reasoning_facts] == [
        "content",
        "content",
        "completed",
    ]
    assert thread.reasoning[0].content == "first second"
    assert thread.reasoning[0].status == "completed"
    assert thread.state.root["reasoning_content"] == "business value"


async def test_host_reasoning_opt_in_rejects_another_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts: tuple[Mapping[str, object], ...] = (
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-openai",
                    content="visible answer",
                    additional_kwargs={"reasoning_content": "private"},
                ),
                {"langgraph_node": "model", "ls_provider": "openai"},
            ),
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    AIMessage(
                        id="assistant-openai",
                        content="visible answer",
                        additional_kwargs={"reasoning_content": "private"},
                        response_metadata={"model_provider": "openai"},
                    )
                ]
            },
            "interrupts": (),
        },
    )
    tracer = Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())
    definition = _definition(
        monkeypatch,
        _Graph(parts),
        tracer=tracer,
        runtime_profile=DeepAgentsV2RuntimeProfile(
            reasoning_extractors=(_ProviderReasoningExtractor(),)
        ),
    )
    runtime = definition

    async for _part in runtime.open_run(
        thread_id=RunIdentity(
            namespace="test", thread_id="thread-openai", run_id="run-openai"
        ).thread_id,
        run_id=RunIdentity(
            namespace="test", thread_id="thread-openai", run_id="run-openai"
        ).run_id,
        input=InputAgentState(messages=[]),
        config=cast(RunnableConfig, {"configurable": {"thread_id": "thread-openai"}}),
    ):
        pass

    thread = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-openai")
    )
    assert not any(
        isinstance(item.fact, ReasoningFact)
        for item in (await thread.events(limit=100)).items
    )


async def test_trace_quota_failure_terminates_the_agent_run_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = TraceLimits(
        max_event_bytes=8 * 1024,
        max_thread_events=16,
        max_thread_bytes=512 * 1024,
        max_tracer_threads=2,
        max_tracer_bytes=1024 * 1024,
        terminal_reserve_events_per_run=2,
        terminal_reserve_bytes_per_run=16 * 1024,
    )
    tracer = Tracer(limits=limits)
    parts: tuple[Mapping[str, object], ...] = tuple(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(id=f"message-{index}", content="content"),
                {"langgraph_node": "model"},
            ),
        }
        for index in range(20)
    )
    definition = _definition(monkeypatch, _Graph(parts), tracer=tracer)
    runtime = definition

    with pytest.raises(RunObservationError):
        _ = [
            part
            async for part in runtime.open_run(
                thread_id=RunIdentity(
                    namespace="test", thread_id="thread-quota", run_id="run-quota"
                ).thread_id,
                run_id=RunIdentity(
                    namespace="test", thread_id="thread-quota", run_id="run-quota"
                ).run_id,
                input=InputAgentState(
                    messages=[HumanMessage(id="user-quota", content="question")]
                ),
            )
        ]

    thread = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread-quota")
    )
    assert thread.status.execution == "unknown"
    assert thread.completeness.missing_tail is True
