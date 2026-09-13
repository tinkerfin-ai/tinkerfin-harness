"""Failure recovery preserves live control messages, AG-UI output and Trace history."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import pytest
from ag_ui.core import (
    BaseEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import JsonValue, PrivateAttr

from tinkerfin import TinkerFin
from tinkerfin.agui import AgUiHistory, AgUiTraceHistory
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_messaging.native import NativeStreamPartCodec
from tinkerfin_native_stream import (
    NativeMessageStreamPart,
    NativeStreamPart,
    to_json_value,
    validate_native_stream_part,
)
from tinkerfin_tracing import (
    CapturePolicy,
    InMemoryTraceStore,
    TraceEvent,
    Tracer,
    TraceStore,
)


@pytest.fixture(params=("memory", "sqlite"))
async def recovery_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[TraceStore]:
    if request.param == "memory":
        yield InMemoryTraceStore()
        return
    from sqlalchemy.ext.asyncio import create_async_engine

    from tinkerfin_tracing import SqlAlchemyTraceStore

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'control-messages.db'}"
    )
    try:
        yield SqlAlchemyTraceStore(engine)
    finally:
        await engine.dispose()


class _CapturingModel(FakeMessagesListChatModel):
    _inputs: list[tuple[BaseMessage, ...]] = PrivateAttr(default_factory=list)

    @property
    def inputs(self) -> tuple[tuple[BaseMessage, ...], ...]:
        return tuple(self._inputs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._inputs.append(
            tuple(message.model_copy(deep=True) for message in messages)
        )
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


def _call(name: str, call_id: str, **args: str) -> AIMessage:
    return AIMessage(
        id=f"proposal-{call_id}",
        content="",
        tool_calls=[{"name": name, "id": call_id, "args": args}],
    )


def _terminal(events: list[BaseEvent], failed: bool) -> None:
    assert sum(isinstance(event, RunStartedEvent) for event in events) == 1
    assert (
        sum(isinstance(event, (RunErrorEvent, RunFinishedEvent)) for event in events)
        == 1
    )
    assert isinstance(events[-1], RunErrorEvent if failed else RunFinishedEvent)


async def record_failure_recovery(
    nested: bool, next_input: Literal["new", "none"], *, store: TraceStore | None = None
) -> dict[str, JsonValue]:
    attempts = 0

    @tool
    async def deliver_report() -> str:
        """Deliver the report after its destination becomes available."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("Destination unavailable")
        return "Report delivered"

    worker = _CapturingModel(
        responses=[
            _call("deliver_report", "delivery-call"),
            AIMessage(id="worker-final", content="Worker complete"),
        ]
    )
    root = (
        _CapturingModel(
            responses=[
                _call(
                    "task",
                    "delegate",
                    subagent_type="reporter",
                    description="Deliver the report",
                ),
                AIMessage(id="root-final", content="Report complete"),
            ]
        )
        if nested
        else worker
    )
    store = InMemoryTraceStore() if store is None else store
    policy = CapturePolicy.public_history(include_error_messages=True)
    tracer = Tracer(store=store, capture_policy=policy)
    builder = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("recovery")
        .with_observer(tracer)
    )
    runtime = (
        builder.build(
            model=root,
            subagents=[
                {
                    "name": "reporter",
                    "description": "Deliver reports",
                    "system_prompt": "Deliver the requested report.",
                    "model": worker,
                    "tools": [deliver_report],
                }
            ],
        )
        if nested
        else builder.build(model=root, tools=[deliver_report])
    )
    initial_parts: list[Mapping[str, object]] = []
    next_parts: list[Mapping[str, object]] = []

    async def initial_part(part: Mapping[str, object]) -> None:
        initial_parts.append(part)

    async def next_part(part: Mapping[str, object]) -> None:
        next_parts.append(part)

    first = runtime.open_agui_run(
        thread_id="thread",
        run_id="failed",
        messages=[{"id": "user-one", "role": "user", "content": "Deliver the report"}],
        on_native_part=initial_part,
    )
    first_events = [event async for event in first]
    assert isinstance(first.error, ValueError)
    _terminal(first_events, failed=True)
    assert attempts == 1
    reader = runtime.agui.history(tracer)
    before = await reader.get("thread")
    assert before.snapshot.summary.status.execution == "failed"
    old_facts = await before.trace.events(limit=1000)
    next_events: list[BaseEvent] = []
    replay_parts: list[NativeStreamPart] = []
    if next_input == "new":
        second = runtime.open_agui_run(
            thread_id="thread",
            run_id="continued",
            messages=[
                {
                    "id": "user-two",
                    "role": "user",
                    "content": "Continue with another destination",
                }
            ],
            on_native_part=next_part,
        )
        next_events = [event async for event in second]
        assert second.error is None
        _terminal(next_events, failed=False)
        first_starts = [
            event.tool_call_id
            for event in first_events
            if isinstance(event, ToolCallStartEvent)
        ]
        first_ends = [
            event.tool_call_id
            for event in first_events
            if isinstance(event, ToolCallEndEvent)
        ]
        assert len(first_starts) == len(set(first_starts))
        assert sorted(first_starts) == sorted(first_ends)
        assert not any(isinstance(event, ToolCallStartEvent) for event in next_events)
        final_results = [
            event for event in next_events if isinstance(event, ToolCallResultEvent)
        ]
        assert len(final_results) == 1 and final_results[0].tool_call_id in first_starts
        assert attempts == 1
        cancelled = [
            message for message in root.inputs[-1] if isinstance(message, ToolMessage)
        ]
        assert len(cancelled) == 1
        assert cancelled[0].tool_call_id == ("delegate" if nested else "delivery-call")
        assert "was cancelled - another message came in" in str(cancelled[0].content)
        assert any(
            isinstance(message, HumanMessage) and message.id == "user-two"
            for message in root.inputs[-1]
        )
        control = [
            parsed.data.message
            for part in next_parts
            if isinstance(
                parsed := validate_native_stream_part(part), NativeMessageStreamPart
            )
            and isinstance(parsed.data.message, RemoveMessage)
        ]
        assert control and {message.id for message in control} == {"__remove_all__"}
        assert all(
            not isinstance(message, RemoveMessage) for message in root.inputs[-1]
        )
        text = "".join(
            event.delta
            for event in next_events
            if isinstance(event, TextMessageContentEvent)
        )
        assert "another message came in" not in text
        assert "__remove_all__" not in text
        assert text == ("Report complete" if nested else "Worker complete")
    else:
        continued = runtime.open_run(
            thread_id="thread", run_id="continued", input=None, on_native_part=next_part
        )
        codec = NativeStreamPartCodec()
        async for part in continued:
            canonical = continued.messaging_codec_input(part)
            decoded = codec.decode(codec.encode(canonical))
            assert decoded == canonical
            replay_parts.append(decoded)
        assert continued.error is None
        assert attempts == 2
        assert not any(
            isinstance(
                parsed := validate_native_stream_part(part), NativeMessageStreamPart
            )
            and isinstance(parsed.data.message, RemoveMessage)
            for part in next_parts
        )
        results = [
            message for message in worker.inputs[-1] if isinstance(message, ToolMessage)
        ]
        assert len(results) == 1 and results[0].content == "Report delivered"
        assert results[0].tool_call_id == "delivery-call"

    current = await reader.get("thread")
    snapshot = current.snapshot
    assert snapshot.summary.status.execution == "succeeded"
    assert len(snapshot.graph.turns) == (2 if next_input == "new" else 1)
    assert before.snapshot.summary.status.execution == "failed"
    assert await before.trace.events(limit=1000) == old_facts
    assert all(message.source_id != "__remove_all__" for message in snapshot.messages)
    assert AgUiTraceHistory.model_validate_json(snapshot.model_dump_json()) == snapshot
    restarted = await AgUiHistory(
        Tracer(store=store, capture_policy=policy), namespace="recovery"
    ).get("thread")
    assert restarted.snapshot.messages == snapshot.messages
    assert restarted.snapshot.graph == snapshot.graph
    events = await current.trace.events(limit=1000)
    assert all(
        TraceEvent.model_validate_json(event.model_dump_json()) == event
        for event in events.items
    )
    from tinkerfin_tracing import RunFact

    terminals = [
        event.fact
        for event in events.items
        if isinstance(event.fact, RunFact) and event.fact.phase == "terminal"
    ]
    assert [(fact.identity.run_id, fact.outcome) for fact in terminals] == [
        ("failed", "failed"),
        ("continued", "succeeded"),
    ]
    agui_codec = AgUiCodec()
    for event in [*first_events, *next_events]:
        assert agui_codec.encode(
            agui_codec.decode(agui_codec.encode(event))
        ) == agui_codec.encode(event)
    return {
        "nested": nested,
        "nextInput": next_input,
        "attempts": attempts,
        "initialParts": to_json_value(initial_parts),
        "nextParts": to_json_value(next_parts),
        "initialEvents": [
            to_json_value(event.model_dump(mode="json", by_alias=True))
            for event in first_events
        ],
        "nextEvents": [
            to_json_value(event.model_dump(mode="json", by_alias=True))
            for event in next_events
        ],
        "replay": [
            to_json_value(part.model_dump(mode="json", by_alias=True))
            for part in replay_parts
        ],
        "history": to_json_value(snapshot.model_dump(mode="json", by_alias=True)),
    }


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("next_input", ["new", "none"])
async def test_failed_run_accepts_new_input_without_changing_none_retry(
    nested: bool,
    next_input: Literal["new", "none"],
    recovery_store: TraceStore,
) -> None:
    await record_failure_recovery(nested, next_input, store=recovery_store)


@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_middleware_control_messages_are_live_and_values_replace_history(
    protocol: str,
) -> None:
    from langchain.agents.middleware import AgentMiddleware, AgentState
    from langchain_core.messages import SystemMessage
    from langgraph.runtime import Runtime

    class RewriteMessages(AgentMiddleware):
        async def abefore_agent(
            self, state: AgentState, runtime: Runtime[None]
        ) -> dict[str, object]:
            del state, runtime
            return {
                "messages": [
                    RemoveMessage(id="__remove_all__"),
                    SystemMessage(id="system", content="CONTROL-INSTRUCTIONS"),
                    HumanMessage(id="corrected-user", content="CORRECTED-REQUEST"),
                ]
            }

    model = _CapturingModel(
        responses=[AIMessage(id="answer", content="Visible answer")]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("controls")
        .with_observer(tracer)
        .build(model=model, middleware=[RewriteMessages()])
    )
    parts: list[Mapping[str, object]] = []

    async def record(part: Mapping[str, object]) -> None:
        parts.append(part)

    if protocol == "agui":
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            messages=[
                {"id": "original-user", "role": "user", "content": "REPLACED-REQUEST"}
            ],
            on_native_part=record,
        )
        events = [event async for event in stream]
        assert stream.error is None
        _terminal(events, failed=False)
        assert (
            "".join(
                event.delta
                for event in events
                if isinstance(event, TextMessageContentEvent)
            )
            == "Visible answer"
        )
    else:
        native = runtime.open_run(
            thread_id="thread",
            run_id="run",
            input={
                "messages": [
                    HumanMessage(id="original-user", content="REPLACED-REQUEST")
                ]
            },
            on_native_part=record,
        )
        codec = NativeStreamPartCodec()
        async for part in native:
            replay = native.messaging_codec_input(part)
            assert codec.decode(codec.encode(replay)) == replay
        assert native.error is None
    control = [
        parsed.data.message
        for part in parts
        if isinstance(
            parsed := validate_native_stream_part(part), NativeMessageStreamPart
        )
    ]
    assert {message.type for message in control} >= {"human", "system", "remove", "ai"}
    assert any(
        isinstance(message, RemoveMessage) and message.id == "__remove_all__"
        for message in control
    )
    assert len(model.inputs) == 1
    assert {message.id for message in model.inputs[0]} >= {"system", "corrected-user"}
    assert all(
        message.id not in {"original-user", "__remove_all__"}
        for message in model.inputs[0]
    )
    history = (await runtime.agui.history(tracer).get("thread")).snapshot
    assert history.summary.status.execution == "succeeded"
    assert {message.source_id for message in history.messages} == {
        "system",
        "corrected-user",
        "answer",
    }


async def record_pending_replacement(
    nested: bool,
    *,
    tracer: Tracer,
    continue_none: bool = True,
) -> dict[str, JsonValue]:
    from ag_ui.core import RunFinishedInterruptOutcome, ToolCallResultEvent

    executed: list[str] = []

    @tool
    async def deliver_report() -> str:
        """Deliver the report only after explicit review."""
        executed.append("delivered")
        return "Delivered"

    worker = _CapturingModel(
        responses=[
            _call("deliver_report", "reviewed-call"),
            AIMessage(id="worker-final", content="New request accepted"),
        ]
    )
    root = (
        _CapturingModel(
            responses=[
                _call(
                    "task",
                    "delegate",
                    subagent_type="reporter",
                    description="Deliver after approval",
                ),
                AIMessage(id="root-final", content="New request accepted"),
            ]
        )
        if nested
        else worker
    )
    builder = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("pending")
        .with_observer(tracer)
    )
    runtime = (
        builder.build(
            model=root,
            subagents=[
                {
                    "name": "reporter",
                    "description": "Deliver reports",
                    "system_prompt": "Deliver only approved reports.",
                    "model": worker,
                    "tools": [deliver_report],
                }
            ],
            interrupt_on={"deliver_report": True},
        )
        if nested
        else builder.build(
            model=root, tools=[deliver_report], interrupt_on={"deliver_report": True}
        )
    )
    initial_parts: list[Mapping[str, object]] = []
    none_parts: list[Mapping[str, object]] = []

    async def observe_initial(part: Mapping[str, object]) -> None:
        initial_parts.append(part)

    async def observe_none(part: Mapping[str, object]) -> None:
        none_parts.append(part)

    initial = runtime.open_agui_run(
        thread_id="thread",
        run_id="pending",
        on_native_part=observe_initial,
        messages=[
            {"id": "user-one", "role": "user", "content": "Deliver after approval"}
        ],
    )
    initial_events = [event async for event in initial]
    assert initial.error is None
    _terminal(initial_events, failed=False)
    terminal = initial_events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    calls_before = (len(root.inputs), len(worker.inputs))
    if continue_none:
        continued = runtime.open_run(
            thread_id="thread", run_id="none", input=None, on_native_part=observe_none
        )
        _ = [part async for part in continued]
        assert continued.error is None
        assert (len(root.inputs), len(worker.inputs)) == calls_before
        assert executed == []
    waiting = await runtime.agui.history(tracer).get("thread")
    assert waiting.snapshot.summary.status.execution == "waiting"
    reader = runtime.agui.history(tracer)
    query_before = await reader.query("thread", limit=1000)
    history_follow = waiting.follow()
    graph_follow = query_before.follow()
    parts: list[Mapping[str, object]] = []

    async def record(part: Mapping[str, object]) -> None:
        parts.append(part)

    new = runtime.open_agui_run(
        thread_id="thread",
        run_id="new",
        messages=[
            {"id": "user-two", "role": "user", "content": "Use a different plan"}
        ],
        on_native_part=record,
    )
    events = [event async for event in new]
    assert new.error is None
    _terminal(events, failed=False)
    assert executed == []
    results = [event for event in events if isinstance(event, ToolCallResultEvent)]
    assert (
        len(results) == 1
        and "was cancelled - another message came in" in results[0].content
    )
    assert any(
        isinstance(parsed := validate_native_stream_part(part), NativeMessageStreamPart)
        and isinstance(parsed.data.message, RemoveMessage)
        for part in parts
    )
    history = await runtime.agui.history(tracer).get("thread")
    assert history.snapshot.summary.status.execution == "succeeded"
    assert history.snapshot.summary.pending_interactions == ()
    target = history.snapshot.as_of_seq
    async with history_follow:
        async for update in history_follow:
            if update.as_of_seq == target:
                assert update.summary.status.execution == "succeeded"
                assert update.summary.pending_interactions == ()
                break
    async with graph_follow:
        async for delta in graph_follow:
            if delta.as_of_seq == target:
                assert delta.ordered_node_ids == history.snapshot.graph.ordered_node_ids
                break
    page_ids: set[str] = set()
    cursor = None
    while True:
        query = await reader.query("thread", limit=2, cursor=cursor)
        page_ids.update(node.id for node in query.snapshot.nodes)
        assert all(
            node.turn_id in {turn.id for turn in history.snapshot.graph.turns}
            for node in query.snapshot.nodes
        )
        cursor = query.snapshot.next_cursor
        if cursor is None:
            break
    assert page_ids == {node.id for node in history.snapshot.graph.nodes}
    from tinkerfin_tracing import TraceGraphFilter
    from tinkerfin_tracing.store import TraceGraphStore

    graph_store = tracer.store
    assert isinstance(graph_store, TraceGraphStore)
    lineage = ("pending", "none", "new") if continue_none else ("pending", "new")
    key = history.trace.key
    for started_runs in (None, (), ("pending",), ("new",)):
        for search in (None, "New request accepted"):
            selected = await graph_store.query_trace_graph(
                key,
                run_ids=lineage,
                started_run_ids=started_runs,
                where=TraceGraphFilter(search=search),
                limit=1,
            )
            direct = {record.node_id: record for record in selected.nodes}
            if started_runs is not None:
                assert all(
                    direct[node_id].started_event.fact.identity.run_id in started_runs
                    for node_id in selected.matched_node_ids
                )
            if started_runs == ():
                assert selected.nodes == () and not selected.has_more
    for invalid in (("foreign",), ("new", "new")):
        with pytest.raises(ValueError, match="unique subset"):
            await graph_store.query_trace_graph(
                key,
                run_ids=lineage,
                started_run_ids=invalid,
                where=TraceGraphFilter(),
                limit=1,
            )
    return {
        "nested": nested,
        "continuedWithNone": continue_none,
        "initialParts": to_json_value(initial_parts),
        "noneParts": to_json_value(none_parts),
        "initialEvents": [
            to_json_value(event.model_dump(mode="json", by_alias=True))
            for event in initial_events
        ],
        "nextEvents": [
            to_json_value(event.model_dump(mode="json", by_alias=True))
            for event in events
        ],
        "nextParts": to_json_value(parts),
        "history": to_json_value(
            history.snapshot.model_dump(mode="json", by_alias=True)
        ),
    }


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("continue_none", [False, True])
async def test_pending_review_none_does_not_execute_and_new_input_keeps_native_pairing(
    nested: bool,
    continue_none: bool,
    recovery_store: TraceStore,
) -> None:
    tracer = Tracer(store=recovery_store)
    await record_pending_replacement(nested, tracer=tracer, continue_none=continue_none)
    reader = AgUiHistory(tracer, namespace="pending")
    recent = await reader.get("thread", limit=1)
    assert all(node.kind != "subagent" for node in recent.snapshot.graph.nodes)
    assert len(recent.snapshot.graph.turns) == 1
    assert recent.snapshot.history_cursor is not None
    full = await reader.get("thread")
    from tinkerfin_tracing.store import TraceGraphRebuildStore

    assert isinstance(recovery_store, TraceGraphRebuildStore)
    await recovery_store.rebuild_trace_graph(recent.trace.key)
    assert (await reader.get("thread", limit=1)).snapshot.graph == recent.snapshot.graph
    # Advance the same Ledger so the old view must rebuild its fixed prefix.
    from datetime import UTC, datetime

    from tinkerfin_contracts import RunIdentity
    from tinkerfin_tracing import RunFact, TurnFact

    now = datetime.now(UTC)
    identity = RunIdentity(namespace="pending", thread_id="thread", run_id="later")
    writer = await recovery_store.open_writer(identity)
    try:
        await writer.append(
            (
                RunFact(
                    identity=identity,
                    source_observation_id="later-start",
                    occurred_at=now,
                    monotonic_ns=1,
                    phase="started",
                    input_kind="ordinary",
                    parent_run_id="new",
                ),
                TurnFact(
                    identity=identity,
                    source_observation_id="later-input",
                    occurred_at=now,
                    monotonic_ns=2,
                    turn_id="later-turn",
                    parent_run_id="new",
                ),
            )
        )
        await writer.append(
            (
                RunFact(
                    identity=identity,
                    source_observation_id="later-terminal",
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="terminal",
                    outcome="succeeded",
                ),
            ),
            mandatory=True,
        )
    finally:
        await writer.aclose()
    original_seq = recent.snapshot.as_of_seq
    await recent.load_older(limit=1)
    assert recent.snapshot.as_of_seq == original_seq
    assert recent.snapshot.graph == full.snapshot.graph


async def test_new_execution_keeps_its_own_turn_and_previous_tool_history() -> None:
    executed: list[str] = []

    @tool
    async def save_report(report: str) -> str:
        """Save the report selected by this new request."""
        executed.append(report)
        return f"Saved {report}"

    model = _CapturingModel(
        responses=[
            message
            for index in (1, 2)
            for message in (
                AIMessage(
                    id=f"proposal-{index}",
                    content="",
                    tool_calls=[
                        {
                            "name": "save_report",
                            "id": f"source-{index}",
                            "args": {"report": str(index)},
                        }
                    ],
                ),
                AIMessage(id=f"answer-{index}", content=f"Saved {index}"),
            )
        ]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("reuse")
        .with_observer(tracer)
        .build(model=model, tools=[save_report])
    )
    first = runtime.open_agui_run(
        thread_id="thread",
        run_id="first",
        messages=[{"id": "user-1", "role": "user", "content": "Save report 1"}],
    )
    _ = [event async for event in first]
    assert first.error is None
    before = (await runtime.agui.history(tracer).get("thread")).snapshot
    old_tool = next(node for node in before.graph.nodes if node.kind == "tool")
    second = runtime.open_agui_run(
        thread_id="thread",
        run_id="second",
        messages=[{"id": "user-2", "role": "user", "content": "Save report 2"}],
    )
    events = [event async for event in second]
    assert second.error is None
    _terminal(events, failed=False)
    assert executed == ["1", "2"]
    assert sum(isinstance(event, ToolCallStartEvent) for event in events) == 1
    assert sum(isinstance(event, ToolCallEndEvent) for event in events) == 1
    assert sum(isinstance(event, ToolCallResultEvent) for event in events) == 1
    after = (await runtime.agui.history(tracer).get("thread")).snapshot
    tools = [node for node in after.graph.nodes if node.kind == "tool"]
    assert len(tools) == 2
    assert next(node for node in tools if node.id == old_tool.id) == old_tool
    current = next(node for node in tools if node.source_id == "source-2")
    assert current.id != old_tool.id
    assert current.source_id == "source-2"
    assert current.turn_id == after.graph.turns[-1].id
    assert current.turn_id != old_tool.turn_id
    assert current.started_seq > old_tool.started_seq
    assert current.result == "Saved 2"


async def test_graph_backend_cannot_ignore_requested_start_owners(
    recovery_store: TraceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from tinkerfin_tracing import (
        DurableTraceStore,
        TraceGraphFilter,
        TraceStoreProtocolError,
    )
    from tinkerfin_tracing.backend import (
        StoredTraceGraphPage,
        TraceGraphQueryBackend,
        TraceGraphQueryRequest,
    )

    tracer = Tracer(store=recovery_store)
    runtime = (
        TinkerFin()
        .with_namespace("backend-filter")
        .with_observer(tracer)
        .build(
            model=_CapturingModel(
                responses=[AIMessage(id="answer", content="Recorded answer")]
            )
        )
    )
    stream = runtime.open_run(
        thread_id="thread",
        run_id="run",
        input={"messages": [HumanMessage(id="user", content="Hello")]},
    )
    _ = [part async for part in stream]
    history = await tracer.get(runtime.thread_identity("thread"))
    assert isinstance(recovery_store, DurableTraceStore)
    backend = recovery_store.backend
    assert isinstance(backend, TraceGraphQueryBackend)
    original = backend.query_trace_graph

    async def ignore_start_filter(
        request: TraceGraphQueryRequest,
    ) -> StoredTraceGraphPage:
        return await original(replace(request, started_run_ids=None))

    monkeypatch.setattr(backend, "query_trace_graph", ignore_start_filter)
    with pytest.raises(TraceStoreProtocolError, match="outside the requested Runs"):
        await recovery_store.query_trace_graph(
            history.key,
            run_ids=("run",),
            started_run_ids=(),
            where=TraceGraphFilter(),
            limit=1,
        )


@pytest.mark.parametrize("scenario_index", range(4))
def test_recorded_control_messages_restore_as_live_objects_and_keep_replay(
    scenario_index: int,
) -> None:
    import json

    from langchain_core.messages import messages_from_dict

    from tinkerfin_native_stream import NativeStreamContractError

    recorded = json.loads(
        (Path(__file__).parent / "fixtures/native-control-recovery.json").read_text()
    )
    scenario = recorded["scenarios"][scenario_index]
    control_ids: list[str] = []
    for part in scenario["nextParts"]:
        if part["type"] != "messages":
            continue
        pair = part["data"]["items"]
        serialized = pair[0]["value"]
        message = messages_from_dict([serialized])[0]
        live = {
            "type": "messages",
            "ns": tuple(part["ns"]["items"]),
            "data": (message, pair[1]),
        }
        parsed = validate_native_stream_part(live)
        assert (
            isinstance(parsed, NativeMessageStreamPart)
            and parsed.data.message is message
        )
        if isinstance(message, RemoveMessage):
            assert message.id is not None
            control_ids.append(message.id)
        with pytest.raises(NativeStreamContractError):
            validate_native_stream_part({**live, "data": (serialized, pair[1])})
    assert control_ids == (["__remove_all__"] if scenario["nextInput"] == "new" else [])
    native_codec = NativeStreamPartCodec()
    for item in scenario["replay"]:
        replay = NativeStreamPart.model_validate_json(json.dumps(item))
        assert native_codec.decode(native_codec.encode(replay)) == replay
    agui_codec = AgUiCodec()
    first = [
        agui_codec.decode(json.dumps(event).encode())
        for event in scenario["initialEvents"]
    ]
    _terminal(first, failed=True)
    if scenario["nextInput"] == "new":
        next_events = [
            agui_codec.decode(json.dumps(event).encode())
            for event in scenario["nextEvents"]
        ]
        _terminal(next_events, failed=False)
    history = AgUiTraceHistory.model_validate_json(json.dumps(scenario["history"]))
    assert history.summary.status.execution == "succeeded"
    assert all(message.source_id != "__remove_all__" for message in history.messages)


@pytest.mark.parametrize("scenario_index", [0, 1])
def test_recorded_pending_controls_keep_terminal_and_scope_settlement(
    scenario_index: int,
) -> None:
    import json

    from langchain_core.messages import messages_from_dict

    recorded = json.loads(
        (Path(__file__).parent / "fixtures/native-control-recovery.json").read_text()
    )
    scenario = recorded["pendingScenarios"][scenario_index]
    assert scenario["continuedWithNone"] is True
    codec = AgUiCodec()
    initial = [
        codec.decode(json.dumps(event).encode()) for event in scenario["initialEvents"]
    ]
    after = [
        codec.decode(json.dumps(event).encode()) for event in scenario["nextEvents"]
    ]
    _terminal(initial, failed=False)
    _terminal(after, failed=False)
    for stage in ("initialParts", "noneParts", "nextParts"):
        removals: list[str | None] = []
        for part in scenario[stage]:
            if part["type"] != "messages":
                continue
            pair = part["data"]["items"]
            message = messages_from_dict([pair[0]["value"]])[0]
            parsed = validate_native_stream_part(
                {
                    "type": "messages",
                    "ns": tuple(part["ns"]["items"]),
                    "data": (message, pair[1]),
                }
            )
            assert isinstance(parsed, NativeMessageStreamPart)
            assert parsed.data.message is message
            if isinstance(message, RemoveMessage):
                removals.append(message.id)
        assert removals == (["__remove_all__"] if stage == "nextParts" else [])
    history = AgUiTraceHistory.model_validate_json(json.dumps(scenario["history"]))
    assert history.summary.status.execution == "succeeded"
    assert history.summary.pending_interactions == ()
    expected = "cancelled" if scenario["nested"] else "resolved"
    assert all(interaction.status == expected for interaction in history.interactions)
