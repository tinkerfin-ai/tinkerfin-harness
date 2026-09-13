"""Public behavior of the deferred Deep Agents runtime façade."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypedDict, cast

import pytest
from ag_ui.core import BaseEvent, RunStartedEvent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.base import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from tinkerfin import (
    AgentRuntime,
    AgUiResumeRequest,
    AgUiRunStream,
    NativeRunStream,
    RunIdentity,
    TinkerFin,
    TinkerFinLifecycleError,
)
from tinkerfin.deep_agent import DeepAgentGraph, create_graph
from tinkerfin.native_driver import DeepAgentsV2StreamDriver


async def test_direct_stream_close_waits_for_borrowed_resource_cleanup() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def source(
        input: object, config: RunnableConfig | None = None, **options: object
    ) -> AsyncIterator[Mapping[str, object]]:
        del input, config, options
        try:
            yield {"type": "values", "ns": (), "data": {"messages": []}}
        finally:
            entered.set()
            await release.wait()
            finished.set()

    graph = DeepAgentGraph(astream=source, driver=DeepAgentsV2StreamDriver())
    stream = graph.astream({"messages": []})
    assert (await anext(stream))["type"] == "values"
    closing = asyncio.create_task(stream.aclose())
    cleanup_entered = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait(
            {closing, cleanup_entered}, return_when=asyncio.FIRST_COMPLETED
        )
        assert not closing.done()
        release.set()
        await closing
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(closing, cleanup_entered, return_exceptions=True)


@asynccontextmanager
async def _coordinate(_identity: RunIdentity) -> AsyncIterator[None]:
    yield


class _RuntimeContext(TypedDict):
    tenant: str


class _FakeModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


class _RecordingGraph:
    def __init__(
        self,
        *,
        parts: tuple[object, ...] = (),
        source_error: Exception | None = None,
    ) -> None:
        self.parts = parts
        self.source_error = source_error
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.opened = 0
        self.pulled = 0
        self.closed = 0

    def astream(
        self,
        *args: object,
        **options: object,
    ) -> AsyncIterator[object]:
        self.calls.append((args, options))

        async def source() -> AsyncIterator[object]:
            self.opened += 1
            try:
                for part in self.parts:
                    self.pulled += 1
                    yield part
                if self.source_error is not None:
                    raise self.source_error
            finally:
                self.closed += 1

        return source()


setattr(
    _RecordingGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


def _install_builder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parts: tuple[object, ...] = (),
    source_error: Exception | None = None,
) -> tuple[
    list[tuple[tuple[object, ...], dict[str, object]]],
    list[_RecordingGraph],
]:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    graphs: list[_RecordingGraph] = []

    def build(*args: object, **kwargs: object) -> _RecordingGraph:
        calls.append((args, kwargs))
        graph = _RecordingGraph(parts=parts, source_error=source_error)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph",
        build,
    )
    return calls, graphs


def _definition(
    tinkerfin: TinkerFin,
) -> AgentRuntime[_RuntimeContext]:
    return tinkerfin.build(
        model="provider:model",
        tools=[],
        system_prompt="system",
        context_schema=_RuntimeContext,
    )


def _graph_input() -> InputAgentState:
    return InputAgentState(messages=[])


def _graph_config() -> RunnableConfig:
    return {"configurable": {"thread_id": "thread-1"}}


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


def _resume_identity(*, run_id: str = "run-resume") -> RunIdentity:
    return _identity(run_id=run_id)


def _state_part(value: int = 1) -> Mapping[str, object]:
    return {
        "type": "values",
        "ns": (),
        "data": {"value": value},
        "interrupts": (),
    }


def _real_definition() -> AgentRuntime[None]:
    return (
        TinkerFin()
        .with_namespace("test")
        .build(model=_FakeModel(responses=[AIMessage(content="ok")]), tools=[])
    )


@pytest.mark.asyncio
async def test_native_facade_streams_a_real_deep_agent_graph() -> None:
    runtime = _real_definition()
    parts = [
        cast(Mapping[str, object], part)
        async for part in runtime.open_run(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=InputAgentState(messages=[HumanMessage(content="hello")]),
        )
    ]
    state_part = next(
        part
        for part in reversed(parts)
        if part["type"] == "values" and part["ns"] == ()
    )
    state = cast(Mapping[str, object], state_part["data"])
    messages = cast(Sequence[BaseMessage], state["messages"])

    assert {part["type"] for part in parts} == {"messages", "tasks", "values"}
    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].content == "ok"


@pytest.mark.asyncio
async def test_definition_creates_one_reusable_direct_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, graphs = _install_builder(
        monkeypatch,
        parts=(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessage(content="answer", id="message-1"),
                    {"langgraph_node": "model"},
                ),
            },
            _state_part(7),
        ),
    )
    graph = await create_graph(_definition(TinkerFin().with_namespace("test")))

    assert isinstance(graph, DeepAgentGraph)
    assert len(calls) == 1
    values = [
        part
        async for part in graph.astream(
            _graph_input(),
            _graph_config(),
            stream_mode=("values",),
        )
    ]
    result = await graph.ainvoke(_graph_input(), _graph_config())

    assert [part["type"] for part in values] == ["values"]
    assert result == {"value": 7}
    assert len(calls) == 1
    assert len(graphs) == 1
    assert len(graphs[0].calls) == 2
    for _args, options in graphs[0].calls:
        assert options["version"] == "v2"
        assert options["subgraphs"] is True
        assert options["stream_mode"] == ("messages", "tasks", "values")


@pytest.mark.asyncio
async def test_direct_graph_reuses_runnable_config_and_async_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, graphs = _install_builder(monkeypatch, parts=(_state_part(9),))
    graph = await create_graph(_definition(TinkerFin().with_namespace("test")))
    configured = graph.with_config({"configurable": {"thread_id": "configured-thread"}})

    configured_result = await configured.ainvoke(_graph_input())
    batch_results = await graph.abatch(
        [_graph_input(), _graph_input()],
        config=[
            {"configurable": {"thread_id": "batch-thread-1"}},
            {"configurable": {"thread_id": "batch-thread-2"}},
        ],
    )

    assert configured_result == {"value": 9}
    assert batch_results == [{"value": 9}, {"value": 9}]
    assert len(calls) == 1
    assert len(graphs) == 1
    thread_ids: set[object] = set()
    for args, _options in graphs[0].calls:
        call_config = cast(Mapping[str, object], args[1])
        configurable = cast(Mapping[str, object], call_config["configurable"])
        thread_ids.add(configurable["thread_id"])
    assert thread_ids == {
        "configured-thread",
        "batch-thread-1",
        "batch-thread-2",
    }


@pytest.mark.asyncio
async def test_tinkerfin_uses_its_default_checkpointer() -> None:
    checkpointer = MemorySaver()
    runtime = (
        TinkerFin(checkpointer=checkpointer)
        .with_namespace("test")
        .build(
            model=_FakeModel(responses=[AIMessage(content="saved")]),
        )
    )
    await runtime.ainvoke(thread_id="thread-1", run_id="run-1", input=_graph_input())
    assert [row async for row in checkpointer.alist(None)]


def test_build_cannot_override_the_constructor_checkpointer() -> None:
    from test_agent_construction import invalid_call

    builder = TinkerFin(checkpointer=MemorySaver()).with_namespace("test")
    with pytest.raises(TypeError, match="checkpointer"):
        invalid_call(
            builder.build,
            model=_FakeModel(responses=[AIMessage(content="saved")]),
            checkpointer=MemorySaver(),
        )


def test_direct_graph_rejects_synchronous_execution() -> None:
    graph = cast(Any, object.__new__(DeepAgentGraph))

    with pytest.raises(NotImplementedError, match="async execution only"):
        graph.invoke(_graph_input(), _graph_config())
    with pytest.raises(NotImplementedError, match="async execution only"):
        graph.stream(_graph_input(), _graph_config())
    with pytest.raises(NotImplementedError, match="async execution only"):
        graph.batch([_graph_input()], [_graph_config()], return_exceptions=True)
    with pytest.raises(NotImplementedError, match="async execution only"):
        graph.batch_as_completed(
            [_graph_input()],
            [_graph_config()],
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_direct_graph_ainvoke_runs_a_real_deep_agent() -> None:
    graph = await create_graph(_real_definition())
    result = await graph.ainvoke(
        InputAgentState(messages=[HumanMessage(content="hello")]),
        _graph_config(),
    )
    messages = cast(Sequence[BaseMessage], result["messages"])

    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].content == "ok"


@pytest.mark.asyncio
async def test_open_run_manages_a_prebuilt_definition() -> None:
    tinkerfin = TinkerFin().with_namespace("test")
    agent = tinkerfin.build(
        model=_FakeModel(responses=[AIMessage(content="managed")]), tools=[]
    )

    stream = agent.open_run(
        thread_id=_identity().thread_id,
        run_id=_identity().run_id,
        input=InputAgentState(messages=[HumanMessage(content="hello")]),
        config=_graph_config(),
    )
    parts = [part async for part in stream]
    state = cast(
        Mapping[str, object],
        next(
            part["data"]
            for part in reversed(parts)
            if part["type"] == "values" and part["ns"] == ()
        ),
    )
    messages = cast(Sequence[BaseMessage], state["messages"])

    assert messages[-1].content == "managed"
    assert stream.messaging_identity == _identity()


@pytest.mark.asyncio
async def test_managed_ainvoke_closes_a_source_without_root_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final-result caller must retain the managed stream's cleanup semantics."""

    _, graphs = _install_builder(monkeypatch)
    tinkerfin = TinkerFin().with_namespace("test")
    agent = _definition(tinkerfin)

    with pytest.raises(TinkerFinLifecycleError, match="without a root values"):
        await agent.ainvoke(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
        )

    assert graphs[0].closed == 1


@pytest.mark.asyncio
async def test_open_run_constructs_one_graph_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch, parts=(_state_part(),))
    runtime = _definition(TinkerFin().with_namespace("test"))
    stream = runtime.open_run(
        thread_id="thread-1", run_id="run-1", input=_graph_input()
    )
    assert calls == []
    await stream.messaging_owner_preflight()
    await stream.messaging_owner_preflight()
    await anext(stream)
    await stream.aclose()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_open_run_retains_graph_preparation_failure(definition_factory) -> None:
    runtime = definition_factory(RuntimeError("model setup failed"))
    stream = runtime.open_run(
        thread_id="thread-1", run_id="run-1", input=_graph_input()
    )
    with pytest.raises(RuntimeError, match="model setup failed"):
        await anext(stream)
    assert isinstance(stream.error, RuntimeError)


@pytest.mark.asyncio
async def test_open_agui_run_manages_a_prebuilt_definition() -> None:
    identity = _identity()
    tinkerfin = TinkerFin().with_namespace("test")
    agent = tinkerfin.build(
        model=_FakeModel(responses=[AIMessage(content="managed")]), tools=[]
    )

    stream = agent.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input=InputAgentState(messages=[HumanMessage(content="hello")]),
    )
    events = [event async for event in stream]

    event_types = [event.type.value for event in events]
    assert stream.messaging_identity == identity
    assert event_types.count("RUN_STARTED") == 1
    assert event_types.count("RUN_FINISHED") == 1
    assert "RUN_ERROR" not in event_types
    assert any(event_type == "TEXT_MESSAGE_CONTENT" for event_type in event_types)


@pytest.mark.asyncio
async def test_open_agui_run_constructs_one_graph_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch, parts=(_state_part(),))
    runtime = _definition(TinkerFin().with_namespace("test"))
    stream = runtime.open_agui_run(
        thread_id="thread-1", run_id="run-1", input=_graph_input()
    )
    assert calls == []
    await stream.messaging_owner_preflight()
    await stream.messaging_owner_preflight()
    await anext(stream)
    await stream.aclose()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_open_agui_run_emits_one_lifecycle_for_preparation_failure(
    definition_factory,
) -> None:
    runtime = definition_factory(RuntimeError("model setup failed"))
    stream = runtime.open_agui_run(
        thread_id="thread-1", run_id="run-1", input=_graph_input()
    )
    events = [event async for event in stream]
    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert isinstance(stream.error, RuntimeError)


@pytest.mark.asyncio
async def test_open_agui_run_requires_exactly_one_input_or_resume() -> None:
    tinkerfin = TinkerFin().with_namespace("test")
    definition = _definition(tinkerfin)
    resume = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": "interrupt-1#0",
                    "status": "cancelled",
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="exactly one"):
        cast(Callable[..., object], definition.open_agui_run)(
            thread_id=_identity().thread_id, run_id=_identity().run_id
        )
    with pytest.raises(ValueError, match="exactly one"):
        cast(Callable[..., object], definition.open_agui_run)(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            input=_graph_input(),
            resume=resume,
        )


@pytest.mark.asyncio
async def test_agui_facade_streams_a_real_deep_agent_graph() -> None:
    identity = _identity()
    runtime = _real_definition()
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=InputAgentState(messages=[HumanMessage(content="hello")]),
        )
    ]

    assert events[0].type.value == "RUN_STARTED"
    assert events[-1].type.value == "RUN_FINISHED"
    assert [event.type.value for event in events].count("RUN_STARTED") == 1
    assert [event.type.value for event in events].count("RUN_FINISHED") == 1
    assert any(event.type.value == "TEXT_MESSAGE_CONTENT" for event in events)
    assert isinstance(events[0], RunStartedEvent)
    assert events[0].input is None


def test_open_agui_rejects_invalid_identity_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(TinkerFin().with_namespace("test"))

    with pytest.raises(ValueError, match="surrounding whitespace"):
        definition.open_agui_run(
            thread_id=" thread-1",
            run_id="run-1",
            input=_graph_input(),
        )

    assert calls == []


def test_open_agui_rejects_self_parent_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(TinkerFin().with_namespace("test"))
    identity = _resume_identity()

    with pytest.raises(ValueError, match="must differ"):
        definition.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=_graph_input(),
            parent_run_id=identity.run_id,
        )

    assert calls == []


def test_resume_requires_public_decisions_before_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    runtime = _definition(TinkerFin().with_namespace("test"))
    open_events = cast(Callable[..., AgUiRunStream], runtime.open_agui_run)
    with pytest.raises(TypeError, match="AgUiResumeRequest"):
        open_events(
            thread_id="thread-1",
            run_id="run-1",
            resume=Command(resume={"decisions": []}),
        )
    assert calls == []


@pytest.mark.asyncio
async def test_agui_runtime_keeps_identity_before_graph_stream_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_builder(monkeypatch, parts=(_state_part(),))
    identity = _identity(thread_id="public-thread")
    runtime = _definition(TinkerFin().with_namespace("test"))

    stream = runtime.open_agui_run(
        thread_id=identity.thread_id, run_id=identity.run_id, input=_graph_input()
    )
    started = await anext(stream)

    assert isinstance(started, RunStartedEvent)
    assert stream.messaging_identity == identity
    assert started.thread_id == "public-thread"
    assert started.input is None
    await stream.aclose()


@pytest.mark.asyncio
async def test_build_defers_fresh_builds_until_stream_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, graphs = _install_builder(monkeypatch)
    tools: list[Callable[..., object]] = []
    tinkerfin = TinkerFin().with_namespace("test")

    definition = tinkerfin.build(
        model="provider:model", tools=tools, system_prompt="system"
    )
    tools.append(lambda: "borrowed-after-definition")

    assert isinstance(definition, AgentRuntime)
    assert calls == []

    native = definition
    agui_identity = _identity(run_id="agui-run")
    agui = definition

    assert isinstance(native, AgentRuntime)
    assert isinstance(agui, AgentRuntime)
    assert calls == []

    native_stream = native.open_run(
        thread_id=_identity(run_id="native-run").thread_id,
        run_id=_identity(run_id="native-run").run_id,
        input=_graph_input(),
    )
    with pytest.raises(StopAsyncIteration):
        await anext(native_stream)
    await native_stream.aclose()
    assert len(calls) == 1

    events = [
        event
        async for event in agui.open_agui_run(
            thread_id=agui_identity.thread_id,
            run_id=agui_identity.run_id,
            input=_graph_input(),
        )
    ]
    assert [event.type.value for event in events] == [
        "RUN_STARTED",
        "RUN_FINISHED",
    ]
    assert len(calls) == 2
    assert len(graphs) == 2
    assert graphs[0] is not graphs[1]
    from tinkerfin._agent_spec import AgentSpec

    specifications = [call[0][0] for call in calls]
    assert all(
        isinstance(spec, AgentSpec) and spec.tools == () for spec in specifications
    )
    assert specifications[0] is not specifications[1]


def test_factory_parameter_binding_fails_without_building_a_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    create = cast(Callable[..., object], TinkerFin().with_namespace("test").build)

    with pytest.raises(TypeError):
        create(unknown_option=True)

    assert calls == []


def test_open_run_requires_identity_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(
        TinkerFin(run_coordinator=_coordinate).with_namespace("test")
    )
    untyped_new = cast(Callable[..., object], definition.open_run)

    with pytest.raises(TypeError, match="thread_id"):
        untyped_new()

    assert calls == []


@pytest.mark.asyncio
async def test_native_run_forwards_settings_lazily_and_has_one_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_part = {"type": "custom", "ns": (), "data": {"value": "native"}}
    _, graphs = _install_builder(monkeypatch, parts=(native_part,))
    observed: list[object] = []

    async def on_part(part: object) -> None:
        observed.append(part)

    identity = _identity()
    runtime = _definition(TinkerFin().with_namespace("test"))
    graph_input = _graph_input()
    config = _graph_config()
    stream = runtime.open_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        on_native_part=on_part,
        input=graph_input,
        config=config,
        context={"tenant": "tenant-1"},
        stream_mode=("custom", "values", "messages", "tasks"),
        print_mode="debug",
        interrupt_before=("model",),
        interrupt_after=("tools",),
        durability="sync",
        control=None,
        debug=True,
    )

    assert isinstance(stream, NativeRunStream)
    assert stream.messaging_identity == identity
    assert graphs == []
    assert observed == []
    assert await anext(stream) is native_part
    assert len(graphs) == 1
    assert observed == [native_part]
    assert config == {"configurable": {"thread_id": "thread-1"}}
    assert len(graphs[0].calls) == 1
    args, options = graphs[0].calls[0]
    assert args[0] == graph_input
    forwarded = cast(RunnableConfig, args[1])
    assert forwarded.get("configurable", {}).get("thread_id") == identity.thread_id
    assert options == {
        "context": {"tenant": "tenant-1"},
        "stream_mode": ("messages", "tasks", "values", "custom"),
        "print_mode": "debug",
        "interrupt_before": ("model",),
        "interrupt_after": ("tools",),
        "durability": "sync",
        "subgraphs": True,
        "debug": True,
        "version": "v2",
    }
    assert aiter(stream) is stream
    with pytest.raises(TinkerFinLifecycleError, match="only be consumed once"):
        aiter(stream)
    await stream.aclose()
    assert graphs[0].closed == 1


@pytest.mark.asyncio
async def test_native_invalid_binding_does_not_claim_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    part = {"type": "custom", "ns": (), "data": {"value": "native"}}
    _, graphs = _install_builder(monkeypatch, parts=(part,))
    runtime = _definition(TinkerFin().with_namespace("test"))
    invalid_astream = cast(Callable[..., object], runtime.open_run)

    with pytest.raises(TypeError):
        invalid_astream()

    stream = runtime.open_run(
        thread_id=_identity().thread_id, run_id=_identity().run_id, input=_graph_input()
    )
    assert await anext(stream) is part
    await stream.aclose()
    assert len(graphs[0].calls) == 1
    forwarded = cast(RunnableConfig, graphs[0].calls[0][0][1])
    assert forwarded.get("configurable", {}).get("thread_id") == _identity().thread_id
    assert graphs[0].calls[0][1]["version"] == "v2"
    assert graphs[0].calls[0][1]["stream_mode"] == (
        "messages",
        "tasks",
        "values",
    )
    assert graphs[0].calls[0][1]["subgraphs"] is True


@pytest.mark.asyncio
async def test_native_invalid_options_and_conflicting_thread_fail_before_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    part = _state_part()
    _, graphs = _install_builder(monkeypatch, parts=(part,))
    runtime = _definition(TinkerFin().with_namespace("test"))
    open_run = cast(Callable[..., NativeRunStream], runtime.open_run)
    for name, value in (
        ("version", "v1"),
        ("subgraphs", False),
        ("output_keys", "messages"),
    ):
        with pytest.raises(TypeError, match=name):
            open_run(
                thread_id="thread-1",
                run_id="run-1",
                input=_graph_input(),
                **{name: value},
            )
    for modes in ("messages", ()):
        with pytest.raises(ValueError, match="stream_mode"):
            open_run(
                thread_id="thread-1",
                run_id="run-1",
                input=_graph_input(),
                stream_mode=modes,
            )
    with pytest.raises(ValueError, match="must equal identity.thread_id"):
        open_run(
            thread_id="thread-1",
            run_id="run-1",
            input=_graph_input(),
            config={"configurable": {"thread_id": "other"}},
        )
    assert graphs == []
    stream = runtime.open_run(
        thread_id="thread-1", run_id="run-1", input=_graph_input()
    )
    assert await anext(stream) == part
    assert len(graphs) == 1
    await stream.aclose()


@pytest.mark.asyncio
async def test_agui_runtime_defaults_reserved_options_and_stays_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    identity = _identity()
    runtime = _definition(TinkerFin().with_namespace("test"))
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input=_graph_input(),
        config=_graph_config(),
    )

    assert isinstance(stream, AgUiRunStream)
    assert graphs == []
    assert (await anext(stream)).type.value == "RUN_STARTED"
    assert len(graphs) == 1
    assert graphs[0].calls == []
    assert (await anext(stream)).type.value == "STATE_SNAPSHOT"
    assert graphs[0].calls[0][1] == {
        "context": None,
        "stream_mode": ("messages", "tasks", "values"),
        "version": "v2",
        "subgraphs": True,
    }
    assert (await anext(stream)).type.value == "RUN_FINISHED"
    await stream.aclose()


@pytest.mark.asyncio
async def test_agui_runtime_normalizes_explicit_modes_and_forwards_other_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    identity = _identity()
    runtime = _definition(TinkerFin().with_namespace("test"))
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input=_graph_input(),
        stream_mode=("custom", "values", "messages", "debug", "tasks"),
        print_mode="updates",
        context={"tenant": "tenant-1"},
    )

    assert (await anext(stream)).type.value == "RUN_STARTED"
    assert (await anext(stream)).type.value == "STATE_SNAPSHOT"
    assert graphs[0].calls[0][1] == {
        "print_mode": "updates",
        "context": {"tenant": "tenant-1"},
        "stream_mode": ("messages", "tasks", "values", "custom", "debug"),
        "version": "v2",
        "subgraphs": True,
    }
    await stream.aclose()


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"stream_mode": "messages"}, "missing required"),
        (
            {"stream_mode": ("messages", "tasks", "values", "messages")},
            "duplicate",
        ),
        (
            {"stream_mode": ("messages", "tasks", "values", "unknown")},
            "unsupported",
        ),
        ({"version": "v1"}, "version"),
        ({"subgraphs": False}, "subgraphs"),
        ({"output_keys": "messages"}, "output_keys"),
    ],
)
def test_invalid_agui_reserved_options_fail_before_every_stream_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, object],
    expected: str,
) -> None:
    coordination: list[RunIdentity] = []
    observed: list[object] = []

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncIterator[None]:
        coordination.append(identity)
        yield

    async def on_part(part: object) -> None:
        observed.append(part)

    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    identity = _identity()
    runtime = _definition(TinkerFin(run_coordinator=coordinate).with_namespace("test"))

    with pytest.raises((TypeError, ValueError), match=expected):
        cast(Callable[..., AgUiRunStream], runtime.open_agui_run)(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=_graph_input(),
            **options,
        )

    assert graphs == []
    assert coordination == []
    assert observed == []

    valid = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        on_native_part=on_part,
        input=_graph_input(),
    )
    assert isinstance(valid, AgUiRunStream)


@pytest.mark.asyncio
async def test_agui_runtime_preserves_observer_order_and_error_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []

    async def on_part(part: object) -> None:
        assert part == _state_part()
        trace.append("part")

    async def on_event(event: BaseEvent) -> None:
        trace.append(f"event:{event.type.value}")

    _, graphs = _install_builder(
        monkeypatch,
        parts=(_state_part(),),
        source_error=RuntimeError("native failed"),
    )
    identity = _identity()
    runtime = _definition(TinkerFin().with_namespace("test"))
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        on_native_part=on_part,
        on_agui_event=on_event,
        input=_graph_input(),
    )
    events = [event async for event in stream]

    assert [event.type.value for event in events] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "RUN_ERROR",
    ]
    assert trace == [
        "event:RUN_STARTED",
        "part",
        "event:STATE_SNAPSHOT",
        "event:RUN_ERROR",
    ]
    assert isinstance(stream.error, RuntimeError)
    assert graphs[0].closed == 1


def test_builder_signature_retains_model_configuration_and_names_its_result() -> None:
    build = inspect.signature(TinkerFin().build)
    assert set(build.parameters) == {
        "model",
        "tools",
        "system_prompt",
        "middleware",
        "subagents",
        "skills",
        "memory",
        "permissions",
        "backend",
        "interrupt_on",
        "response_format",
        "state_schema",
        "context_schema",
        "name",
    }
    assert build.parameters["model"].default is inspect.Parameter.empty
    runtime = _real_definition()
    for operation in (runtime.open_run, runtime.open_agui_run, runtime.ainvoke):
        parameters = inspect.signature(operation).parameters
        assert {"thread_id", "run_id", "context"} <= parameters.keys()
        assert all(
            parameter.kind is not inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        assert not {"agent", "version", "subgraphs", "output_keys"} & parameters.keys()


@pytest.mark.asyncio
async def test_runtime_creates_independent_single_consumer_agui_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    runtime = _definition(TinkerFin().with_namespace("test"))
    first = runtime.open_agui_run(
        thread_id="thread-1", run_id="run-1", input=_graph_input()
    )
    second = runtime.open_agui_run(
        thread_id="thread-1", run_id="run-2", input=_graph_input()
    )
    assert first is not second
    assert aiter(first) is first
    with pytest.raises(TinkerFinLifecycleError, match="only be consumed once"):
        aiter(first)
    await first.aclose()
    await second.aclose()
    assert calls == []


@pytest.mark.asyncio
async def test_agui_user_messages_preserve_authoritative_ids_and_content() -> None:
    seen: list[BaseMessage] = []

    class CaptureMessages(AsyncCallbackHandler):
        async def on_chat_model_start(self, serialized, messages, **kwargs):
            seen.extend(messages[0])

    tinkerfin = TinkerFin().with_namespace("test")
    agent = tinkerfin.build(
        model=_FakeModel(
            responses=[AIMessage(content="received")], callbacks=[CaptureMessages()]
        ),
        tools=[],
    )
    stream = agent.open_agui_run(
        thread_id=_identity().thread_id,
        run_id=_identity().run_id,
        messages=[
            {"id": "user-one", "role": "user", "name": "reader", "content": "hello"},
            {"id": "user-two", "role": "user", "content": "follow up"},
        ],
    )
    events = [event async for event in stream]
    assert events[-1].type.value == "RUN_FINISHED"
    users = [message for message in seen if isinstance(message, HumanMessage)]
    assert [(message.id, message.content) for message in users] == [
        ("user-one", "hello"),
        ("user-two", "follow up"),
    ]
    assert users[0].name == "reader"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"content": "missing id"}],
        [{"id": "m", "role": "assistant", "content": "no"}],
        [{"id": "m", "content": "one"}, {"id": "m", "content": "two"}],
    ],
)
async def test_invalid_agui_messages_fail_before_graph_preparation(
    monkeypatch: pytest.MonkeyPatch, messages
) -> None:
    calls, _ = _install_builder(monkeypatch)
    runtime = _definition(TinkerFin().with_namespace("test"))
    stream = runtime.open_agui_run(
        thread_id="thread-1", run_id="run-1", messages=messages
    )
    events = [event async for event in stream]
    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert calls == []


@pytest.mark.asyncio
async def test_agui_messages_cannot_be_combined_with_native_input_or_resume() -> None:
    tinkerfin = TinkerFin().with_namespace("test")
    agent = _definition(tinkerfin)
    messages = [{"id": "m", "content": "hello"}]
    with pytest.raises(ValueError, match="exactly one"):
        cast(Callable[..., object], agent.open_agui_run)(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            messages=messages,
            input=_graph_input(),
        )
    with pytest.raises(ValueError, match="exactly one"):
        cast(Callable[..., object], agent.open_agui_run)(
            thread_id=_identity().thread_id,
            run_id=_identity().run_id,
            messages=messages,
            resume=AgUiResumeRequest.model_validate(
                {"entries": [{"interruptId": "i", "status": "cancelled"}]}
            ),
        )
