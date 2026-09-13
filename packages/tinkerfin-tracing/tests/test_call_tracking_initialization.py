"""Call history remains known across failures that precede Agent execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from deepagents.backends import StateBackend
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin import TinkerFin
from tinkerfin_contracts import (
    NativeTaskObservation,
    PreparedWorkspace,
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
)
from tinkerfin_tracing import (
    CallTrackingFact,
    InMemoryTraceStore,
    RunFact,
    SqlAlchemyTraceStore,
    TraceGraphFilter,
    TraceGraphNodeKind,
    Tracer,
    TraceStore,
    TraceThreadNotFound,
)


class _PreparationWorkspace:
    def __init__(self, prepare: Callable[[], Awaitable[None]]) -> None:
        self._prepare = prepare

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[None, StateBackend]]:
        await self._prepare()
        yield PreparedWorkspace(None, StateBackend())


class _LocalModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> _LocalModel:
        del tools, kwargs
        return self


@pytest.fixture(
    params=(
        "memory",
        "sqlite",
        pytest.param("mysql", marks=pytest.mark.docker_integration),
    )
)
async def trace_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[TraceStore]:
    if request.param == "memory":
        yield InMemoryTraceStore()
        return
    async with AsyncExitStack() as databases:
        url = (
            await databases.enter_async_context(
                request.getfixturevalue("trace_mysql_database")()
            )
            if request.param == "mysql"
            else f"sqlite+aiosqlite:///{tmp_path / 'call-history.db'}"
        )
        assert isinstance(url, str)
        engine = create_async_engine(url)
        try:
            yield SqlAlchemyTraceStore(engine)
        finally:
            await engine.dispose()


def _input(identity: RunIdentity) -> InputAgentState:
    return InputAgentState(
        messages=[HumanMessage(id=f"user-{identity.run_id}", content="local test")]
    )


async def _run_healthy(runtime: TinkerFin, identity: RunIdentity) -> None:
    definition = runtime.build(
        model=_LocalModel(
            responses=[
                AIMessage(id=f"assistant-{identity.run_id}", content="local reply")
            ]
        ),
        tools=[],
        system_prompt="Local test",
    )
    stream = definition.open_run(
        thread_id=identity.thread_id, run_id=identity.run_id, input=_input(identity)
    )
    try:
        async for _ in stream:
            pass
    finally:
        await stream.aclose()


@pytest.mark.parametrize("update", [False, True])
async def test_native_continuation_initialization_failure_keeps_input_and_turn(
    trace_store: TraceStore, update: bool
) -> None:
    tracer = Tracer(store=trace_store)
    runtime = TinkerFin().with_namespace("test").with_observer(tracer)
    first = RunIdentity(
        namespace="test", thread_id="continuation-setup", run_id="first"
    )
    await _run_healthy(runtime, first)
    failure = RuntimeError("continuation workspace unavailable")

    async def prepare_graph() -> None:
        raise failure

    broken = runtime.build(
        model=_LocalModel(responses=[AIMessage(content="unused")]),
        backend=_PreparationWorkspace(prepare_graph),
    )
    graph_input: Command[object] | None = (
        Command(update={"note": "updated"}) if update else None
    )
    stream = broken.open_run(
        thread_id=first.thread_id, run_id="failed", input=graph_input
    )
    with pytest.raises(RuntimeError) as captured:
        async for _part in stream:
            raise AssertionError("Failed preparation emitted a graph part")
    assert captured.value is failure
    view = await tracer.get(first.thread)
    assert len(view.graph.turns) == 1
    assert view.status.execution == "failed"
    facts = [event.fact for event in (await view.events(limit=100)).items]
    source = next(
        fact
        for fact in facts
        if isinstance(fact, RunFact)
        and fact.identity.run_id == "failed"
        and fact.phase == "resumed"
    )
    assert source.input_kind == "continuation"
    assert source.input is not None
    if update:
        assert isinstance(source.input.value, dict)
        command_value = source.input.value["value"]
        assert isinstance(command_value, dict)
        assert command_value["update"] == {"note": "updated"}
    else:
        assert source.input.value is None
    terminal = next(
        fact
        for fact in facts
        if isinstance(fact, RunFact)
        and fact.identity.run_id == "failed"
        and fact.phase == "terminal"
    )
    assert terminal.code == "runtime_initialization_error"
    assert terminal.error_type == "builtins.RuntimeError"
    await _run_healthy(runtime, first.model_copy(update={"run_id": "next"}))
    assert len((await tracer.get(first.thread)).graph.turns) == 2


@pytest.mark.parametrize("transport", ("native", "agui"))
async def test_initialization_failure_keeps_existing_and_future_call_history(
    trace_store: TraceStore, transport: Literal["native", "agui"]
) -> None:
    tracer = Tracer(store=trace_store)
    runtime = TinkerFin().with_namespace("test").with_observer(tracer)
    first = RunIdentity(
        namespace="test", thread_id="call-history", run_id="healthy-first"
    )
    failed = RunIdentity(
        namespace="test", thread_id=first.thread_id, run_id="initialization-failed"
    )
    last = RunIdentity(
        namespace="test", thread_id=first.thread_id, run_id="healthy-last"
    )
    failure = RuntimeError("test-owned initialization failure")

    async def prepare_graph() -> None:
        raise failure

    await _run_healthy(runtime, first)
    before = await tracer.query(first.thread)
    original_models = {
        node.id for node in before.nodes if node.kind is TraceGraphNodeKind.MODEL
    }
    assert len(original_models) == 1
    assert not before.completeness.call_tracking_missing
    failed_runtime = (
        TinkerFin()
        .with_namespace("test")
        .with_observer(tracer)
        .build(
            model=_LocalModel(responses=[AIMessage(content="unused")]),
            backend=_PreparationWorkspace(prepare_graph),
        )
    )
    if transport == "native":
        native = failed_runtime.open_run(
            thread_id=failed.thread_id, run_id=failed.run_id, input=_input(failed)
        )
        try:
            with pytest.raises(RuntimeError) as captured:
                async for _ in native:
                    raise AssertionError("initialization failure emitted native data")
            assert captured.value is failure
            assert native.error is failure
        finally:
            await native.aclose()
    else:
        agui = failed_runtime.open_agui_run(
            thread_id=failed.thread_id, run_id=failed.run_id, input=_input(failed)
        )
        try:
            lifecycle = [item.type.value async for item in agui]
            assert lifecycle == ["RUN_STARTED", "RUN_ERROR"]
            assert agui.error is failure
        finally:
            await agui.aclose()

    snapshot = await trace_store.snapshot(first.thread)
    events = await trace_store.read_events(
        snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=1000
    )
    failed_facts = [event.fact for event in events if event.fact.identity == failed]
    assert not any(isinstance(fact, CallTrackingFact) for fact in failed_facts)
    assert not any(
        fact.kind in {"model.call", "tool.execution", "tool"} for fact in failed_facts
    )
    phases = [fact.phase for fact in failed_facts if isinstance(fact, RunFact)]
    assert phases == ["started", "input", "terminal", "closed"]
    terminal = next(
        fact
        for fact in failed_facts
        if isinstance(fact, RunFact) and fact.phase == "terminal"
    )
    assert terminal.code == "runtime_initialization_error"
    assert terminal.outcome == "failed"
    assert terminal.error_type == "builtins.RuntimeError"

    after_failure = await tracer.query(first.thread)
    assert not after_failure.completeness.call_tracking_missing
    assert original_models <= {node.id for node in after_failure.nodes}
    assert any(node.run_id == failed.run_id for node in after_failure.nodes)
    response = await tracer.query(
        first.thread,
        where=TraceGraphFilter(model_call_id=next(iter(original_models))),
    )
    assert response.nodes
    assert not response.completeness.call_tracking_missing
    history = await tracer.get(first.thread, limit=1)
    assert history.status.execution == "failed"
    assert not history.graph.completeness.call_tracking_missing
    assert history.history_cursor is not None

    await _run_healthy(runtime, last)
    current = await tracer.query(first.thread)
    assert not current.completeness.call_tracking_missing
    assert (
        len([node for node in current.nodes if node.kind is TraceGraphNodeKind.MODEL])
        == 2
    )
    assert (await tracer.get(first.thread)).status.execution == "succeeded"
    older = await tracer.get(
        first.thread, history_cursor=history.history_cursor, limit=100
    )
    assert older.as_of_seq == history.as_of_seq
    assert not older.graph.completeness.call_tracking_missing
    assert not any(node.run_id == last.run_id for node in older.graph.nodes)


@pytest.mark.parametrize("has_native_task", (False, True))
async def test_untracked_execution_failure_remains_unknown_after_a_healthy_run(
    trace_store: TraceStore, has_native_task: bool
) -> None:
    tracer = Tracer(store=trace_store)
    identity = RunIdentity(
        namespace="test", thread_id="untracked", run_id="untracked-failure"
    )
    source = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={},
        config={},
        call_tracking_enabled=False,
    )
    session = await tracer.open_run(source)
    now = datetime.now(UTC)
    try:
        await session.observe(
            RunStartedObservation(identity=identity, observed_at=now, monotonic_ns=1)
        )
        await session.observe(
            RunInputObservation(
                identity=identity, source=source, observed_at=now, monotonic_ns=2
            )
        )
        if has_native_task:
            await session.observe(
                NativeTaskObservation(
                    identity=identity,
                    graph_namespace=(),
                    phase="start",
                    task_id="untracked-model-task",
                    name="model",
                    input={},
                    observed_at=now,
                    monotonic_ns=3,
                )
            )
        await session.observe(
            RunTerminalObservation(
                identity=identity,
                outcome="failed",
                code="runtime_error",
                error_type="builtins.RuntimeError",
                observed_at=now,
                monotonic_ns=4,
            )
        )
        await session.observe(
            RunClosedObservation(
                identity=identity, outcome="failed", observed_at=now, monotonic_ns=5
            )
        )
    finally:
        await session.aclose()
    assert (await tracer.query(identity.thread)).completeness.call_tracking_missing
    assert (await tracer.get(identity.thread)).graph.completeness.call_tracking_missing
    await _run_healthy(
        TinkerFin().with_namespace("test").with_observer(tracer),
        RunIdentity(
            namespace="test", thread_id=identity.thread_id, run_id="subsequent-healthy"
        ),
    )
    assert (await tracer.query(identity.thread)).completeness.call_tracking_missing
    assert (await tracer.get(identity.thread)).graph.completeness.call_tracking_missing


@pytest.mark.parametrize("transport", ("native", "agui"))
async def test_cancelled_initialization_preserves_cancellation_and_next_run(
    trace_store: TraceStore, transport: Literal["native", "agui"]
) -> None:
    tracer = Tracer(store=trace_store)
    runtime = TinkerFin().with_namespace("test").with_observer(tracer)
    identity = RunIdentity(
        namespace="test", thread_id="cancelled-initialization", run_id="cancelled"
    )
    entered = asyncio.Event()

    async def prepare_graph() -> None:
        entered.set()
        await asyncio.Future[None]()
        raise AssertionError("cancelled initialization continued")

    cancelled_runtime = (
        TinkerFin()
        .with_namespace("test")
        .with_observer(tracer)
        .build(
            model=_LocalModel(responses=[AIMessage(content="unused")]),
            backend=_PreparationWorkspace(prepare_graph),
        )
    )
    stream = (
        cancelled_runtime.open_run(
            thread_id=identity.thread_id, run_id=identity.run_id, input=_input(identity)
        )
        if transport == "native"
        else cancelled_runtime.open_agui_run(
            thread_id=identity.thread_id, run_id=identity.run_id, input=_input(identity)
        )
    )
    pending = asyncio.create_task(stream.messaging_owner_preflight())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        for attempt in range(6):
            if pending.done():
                break
            pending.cancel(f"initialization cancellation {attempt + 1}")
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError) as captured:
            await pending
        assert captured.value.args == ("initialization cancellation 1",)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
    with pytest.raises(TraceThreadNotFound):
        await trace_store.snapshot(identity.thread)
    await _run_healthy(
        runtime,
        RunIdentity(
            namespace="test", thread_id=identity.thread_id, run_id="after-cancellation"
        ),
    )
    assert not (await tracer.query(identity.thread)).completeness.call_tracking_missing
