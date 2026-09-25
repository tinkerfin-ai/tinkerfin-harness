"""Proven Subagent context boundaries do not depend on cross-channel delivery order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from tinkerfin_contracts import (
    ModelCallObservation,
    NativeMessageRecord,
    NativeStateObservation,
    NativeTaskObservation,
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunObservationSession,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    ToolExecutionObservation,
)
from tinkerfin_tracing import (
    CapturePolicy,
    ModelCallFact,
    SubagentFact,
    ToolExecutionFact,
    TraceCorruption,
    TraceGraphNodeKind,
    Tracer,
)

_IDENTITY = RunIdentity(namespace="test", thread_id="callback-order", run_id="run")
_CHILD = ("tools:parent-task",)
_BASE = datetime(2026, 9, 5, tzinfo=UTC)


def _time(value: int) -> datetime:
    return _BASE + timedelta(seconds=value)


async def _prepare(
    tracer: Tracer,
    *,
    descriptor: bool = True,
    parent_execution: bool = True,
    parent_time: int = 6,
    proposed_description: str = "local child",
) -> RunObservationSession:
    source = RunSourceContext(
        identity=_IDENTITY,
        runtime_profile="deepagents-v3",
        input_kind="ordinary",
        input={},
        config={},
        call_tracking_enabled=True,
    )
    session = await tracer.open_run(source)
    await session.observe(
        RunStartedObservation(identity=_IDENTITY, observed_at=_time(1), monotonic_ns=1)
    )
    await session.observe(
        RunInputObservation(
            identity=_IDENTITY, source=source, observed_at=_time(2), monotonic_ns=2
        )
    )
    await session.observe(
        ModelCallObservation(
            identity=_IDENTITY,
            phase="started",
            call_id="parent-model",
            messages=(NativeMessageRecord(message_type="system", content="root"),),
            observed_at=_time(3),
            monotonic_ns=3,
        )
    )
    await session.observe(
        ModelCallObservation(
            identity=_IDENTITY,
            phase="completed",
            call_id="parent-model",
            tool_call_ids=("delegate",),
            observed_at=_time(4),
            monotonic_ns=4,
        )
    )
    if descriptor:
        await session.observe(_parent_task_start(description=proposed_description))
    if parent_execution:
        await session.observe(
            ToolExecutionObservation(
                identity=_IDENTITY,
                graph_namespace=(),
                phase="started",
                execution_id="parent-execution",
                tool_call_id="delegate",
                tool_name="task",
                graph_task_id="parent-task",
                input={"description": "local child", "subagent_type": "worker"},
                observed_at=_time(parent_time),
                monotonic_ns=parent_time,
            )
        )
    return session


def _parent_task_start(*, description: str = "local child") -> NativeTaskObservation:
    return NativeTaskObservation(
        identity=_IDENTITY,
        graph_namespace=(),
        phase="start",
        task_id="parent-task",
        name="tools",
        input=[
            {
                "id": "delegate",
                "name": "task",
                "args": {"description": description, "subagent_type": "worker"},
            }
        ],
        observed_at=_time(5),
        monotonic_ns=5,
    )


def _model_start(
    value: int, namespace: tuple[str, ...] = _CHILD
) -> ModelCallObservation:
    return ModelCallObservation(
        identity=_IDENTITY,
        graph_namespace=namespace,
        phase="started",
        call_id="child-model",
        agent_name="worker",
        messages=(NativeMessageRecord(message_type="system", content="child"),),
        observed_at=_time(value),
        monotonic_ns=value,
    )


def _tool_start(value: int) -> ToolExecutionObservation:
    return ToolExecutionObservation(
        identity=_IDENTITY,
        graph_namespace=_CHILD,
        phase="started",
        execution_id="child-execution",
        tool_call_id="child-tool",
        tool_name="child_tool",
        input={"value": "local"},
        observed_at=_time(value),
        monotonic_ns=value,
    )


def _native_start(value: int) -> NativeTaskObservation:
    return NativeTaskObservation(
        identity=_IDENTITY,
        graph_namespace=_CHILD,
        phase="start",
        task_id="child-native-task",
        name="model",
        input={},
        observed_at=_time(value),
        monotonic_ns=value,
    )


@pytest.mark.parametrize("first", ("model", "tool", "native"))
@pytest.mark.parametrize("parent_native_first", (False, True))
async def test_child_callback_and_native_orders_share_one_proven_start(
    first: Literal["model", "tool", "native"],
    parent_native_first: bool,
) -> None:
    tracer = Tracer()
    session = await _prepare(tracer, descriptor=parent_native_first)
    expected_context_start = _time(6)
    try:
        if first == "native":
            await session.observe(_native_start(7))
            await session.observe(_model_start(8))
        elif first == "model":
            await session.observe(_model_start(7))
            await session.observe(_native_start(8))
        else:
            await session.observe(_tool_start(7))
            await session.observe(_native_start(8))
            await session.observe(
                ToolExecutionObservation(
                    identity=_IDENTITY,
                    graph_namespace=_CHILD,
                    phase="completed",
                    execution_id="child-execution",
                    tool_call_id="child-tool",
                    tool_name="child_tool",
                    output="done",
                    observed_at=_time(9),
                    monotonic_ns=9,
                )
            )
            await session.observe(_model_start(10))
            expected_context_start = _time(9)
        if not parent_native_first:
            await session.observe(_parent_task_start())
        await session.observe(
            NativeStateObservation(
                identity=_IDENTITY,
                graph_namespace=_CHILD,
                state={},
                observed_at=_time(11),
                monotonic_ns=11,
            )
        )
        await session.observe(
            ModelCallObservation(
                identity=_IDENTITY,
                graph_namespace=_CHILD,
                phase="completed",
                call_id="child-model",
                observed_at=_time(12),
                monotonic_ns=12,
            )
        )
        await session.observe(
            ToolExecutionObservation(
                identity=_IDENTITY,
                graph_namespace=(),
                phase="completed",
                execution_id="parent-execution",
                tool_call_id="delegate",
                tool_name="task",
                output="done",
                graph_task_id="parent-task",
                observed_at=_time(13),
                monotonic_ns=13,
            )
        )
        await session.observe(
            NativeTaskObservation(
                identity=_IDENTITY,
                graph_namespace=(),
                phase="result",
                task_id="parent-task",
                name="tools",
                result={},
                observed_at=_time(14),
                monotonic_ns=14,
            )
        )
        await session.observe(
            RunTerminalObservation(
                identity=_IDENTITY,
                outcome="succeeded",
                observed_at=_time(15),
                monotonic_ns=15,
            )
        )
        await session.observe(
            RunClosedObservation(
                identity=_IDENTITY,
                outcome="succeeded",
                observed_at=_time(16),
                monotonic_ns=16,
            )
        )
    finally:
        await session.aclose()
    snapshot = await tracer.store.snapshot(_IDENTITY.thread)
    events = await tracer.store.read_events(
        snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
    )
    starts = [
        event
        for event in events
        if isinstance(event.fact, SubagentFact) and event.fact.phase == "started"
    ]
    assert len(starts) == 1
    opening = starts[0]
    assert isinstance(opening.fact, SubagentFact)
    assert opening.fact.occurred_at == _time(6)
    assert opening.fact.monotonic_ns == 6
    assert opening.fact.graph_namespace == _CHILD
    assert opening.fact.parent_execution_id is not None
    child_model = next(
        event
        for event in events
        if isinstance(event.fact, ModelCallFact)
        and event.fact.phase == "started"
        and event.fact.graph_namespace == _CHILD
    )
    assert isinstance(child_model.fact, ModelCallFact)
    assert child_model.fact.context_started_at == expected_context_start
    assert opening.trace_seq < child_model.trace_seq
    if first == "tool":
        child_tool = next(
            event
            for event in events
            if isinstance(event.fact, ToolExecutionFact)
            and event.fact.phase == "started"
            and event.fact.graph_namespace == _CHILD
        )
        assert opening.trace_seq < child_tool.trace_seq
    graph = await tracer.query(_IDENTITY.thread)
    subagents = [
        node for node in graph.nodes if node.kind is TraceGraphNodeKind.SUBAGENT
    ]
    assert len(subagents) == 1
    assert subagents[0].started_at == _time(6)
    context = next(
        node
        for node in graph.nodes
        if node.kind is TraceGraphNodeKind.CONTEXT and node.graph_namespace == _CHILD
    )
    model = next(
        node
        for node in graph.nodes
        if node.kind is TraceGraphNodeKind.MODEL and node.graph_namespace == _CHILD
    )
    assert context.parent_subagent_id == model.parent_subagent_id == subagents[0].id
    assert context.started_at == expected_context_start
    assert graph.ordered_node_ids.index(context.id) + 1 == graph.ordered_node_ids.index(
        model.id
    )


@pytest.mark.parametrize("callback", ("model", "tool"))
@pytest.mark.parametrize("parent_time", (None, 9))
async def test_proven_child_callback_rejects_missing_or_later_parent_boundary(
    callback: Literal["model", "tool"], parent_time: int | None
) -> None:
    tracer = Tracer()
    session = await _prepare(
        tracer, parent_execution=parent_time is not None, parent_time=parent_time or 6
    )
    try:
        with pytest.raises(TraceCorruption):
            await session.observe(
                _model_start(7) if callback == "model" else _tool_start(7)
            )
    finally:
        await session.aclose()
    snapshot = await tracer.store.snapshot(_IDENTITY.thread)
    events = await tracer.store.read_events(
        snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
    )
    assert not any(isinstance(event.fact, SubagentFact) for event in events)


@pytest.mark.parametrize("namespace", [("ordinary:subgraph",), ("tools:unrelated",)])
async def test_unproven_namespace_keeps_ordinary_scope_without_inventing_subagent(
    namespace: tuple[str, ...],
) -> None:
    tracer = Tracer()
    session = await _prepare(tracer, descriptor=False)
    try:
        await session.observe(_model_start(7, namespace=namespace))
    finally:
        await session.aclose()
    snapshot = await tracer.store.snapshot(_IDENTITY.thread)
    events = await tracer.store.read_events(
        snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
    )
    assert not any(isinstance(event.fact, SubagentFact) for event in events)
    child = next(
        event.fact
        for event in events
        if isinstance(event.fact, ModelCallFact) and event.fact.graph_namespace
    )
    assert isinstance(child, ModelCallFact)
    assert not child.in_subagent_scope
    assert child.context_started_at == _time(4)


@pytest.mark.parametrize("parent_native_first", (False, True))
async def test_subagent_uses_executed_arguments_when_native_proposal_differs(
    parent_native_first: bool,
) -> None:
    tracer = Tracer(capture_policy=CapturePolicy.public_history())
    session = await _prepare(
        tracer,
        descriptor=parent_native_first,
        proposed_description="proposed task",
    )
    try:
        await session.observe(_model_start(7))
        if not parent_native_first:
            await session.observe(_parent_task_start(description="proposed task"))
        await session.observe(
            ModelCallObservation(
                identity=_IDENTITY,
                graph_namespace=_CHILD,
                phase="completed",
                call_id="child-model",
                observed_at=_time(8),
                monotonic_ns=8,
            )
        )
    finally:
        await session.aclose()

    snapshot = await tracer.store.snapshot(_IDENTITY.thread)
    events = await tracer.store.read_events(
        snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
    )
    opening = next(
        event.fact for event in events if isinstance(event.fact, SubagentFact)
    )
    assert isinstance(opening, SubagentFact)
    assert opening.input is not None
    assert opening.input.value == {
        "description": "local child",
        "subagent_type": "worker",
    }
    models = [
        event.fact
        for event in events
        if isinstance(event.fact, ModelCallFact)
        and event.fact.graph_namespace == _CHILD
    ]
    assert [model.in_subagent_scope for model in models] == [True, True]
    assert any(
        node.kind is TraceGraphNodeKind.SUBAGENT
        for node in (await tracer.query(_IDENTITY.thread)).nodes
    )


async def test_tool_execution_cannot_change_its_owning_graph_task() -> None:
    tracer = Tracer()
    session = await _prepare(tracer)
    try:
        with pytest.raises(TraceCorruption, match="Tool execution identity changed"):
            await session.observe(
                ToolExecutionObservation(
                    identity=_IDENTITY,
                    graph_namespace=(),
                    phase="completed",
                    execution_id="parent-execution",
                    tool_call_id="delegate",
                    tool_name="task",
                    graph_task_id="another-task",
                    output="done",
                    observed_at=_time(7),
                    monotonic_ns=7,
                )
            )
    finally:
        await session.aclose()
