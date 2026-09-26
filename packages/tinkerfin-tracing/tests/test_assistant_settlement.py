"""Assistant delivery retains partial content and closes from exact Run evidence."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import JsonValue, ValidationError
from sqlalchemy import event as sql_event
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin_contracts import (
    GraphOrigin,
    GraphTaskReference,
    ModelCallObservation,
    NativeMessageObservation,
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
    RunTerminalOutcome,
    SubagentRequestReference,
    ToolExecutionObservation,
    subagent_request_id,
)
from tinkerfin_tracing import (
    CapturedValue,
    InMemoryTraceStore,
    InteractionFact,
    MessageFact,
    ModelCallFact,
    RunFact,
    SqlAlchemyTraceStore,
    ToolFact,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    TraceLimits,
    TraceQuotaExceeded,
    Tracer,
    TraceSemanticFact,
    TraceStore,
    TraceStoreProtocolError,
    TurnFact,
)
from tinkerfin_tracing._ids import scope_id
from tinkerfin_tracing.redaction import RedactionContext

NOW = datetime(2026, 9, 6, tzinfo=UTC)


class _ObservationSource(TypedDict):
    identity: RunIdentity
    observed_at: datetime
    monotonic_ns: int


def _source(identity: RunIdentity, sequence: int) -> _ObservationSource:
    return {
        "identity": identity,
        "observed_at": NOW + timedelta(milliseconds=sequence),
        "monotonic_ns": sequence,
    }


@pytest.fixture(
    params=(
        "memory",
        "sqlite",
        pytest.param("mysql", marks=pytest.mark.docker_integration),
        pytest.param("postgresql", marks=pytest.mark.docker_integration),
    )
)
async def assistant_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[tuple[TraceStore, AsyncEngine | None]]:
    if request.param == "memory":
        yield InMemoryTraceStore(), None
        return
    async with AsyncExitStack() as databases:
        if request.param == "postgresql":
            engine = await databases.enter_async_context(
                request.getfixturevalue("trace_postgresql_database")()
            )
            assert isinstance(engine, AsyncEngine)
            yield SqlAlchemyTraceStore(engine), engine
            return
        url = (
            await databases.enter_async_context(
                request.getfixturevalue("trace_mysql_database")()
            )
            if request.param == "mysql"
            else f"sqlite+aiosqlite:///{tmp_path / 'assistant.db'}"
        )
        assert isinstance(url, str)
        engine = create_async_engine(url, hide_parameters=True)
        try:
            yield SqlAlchemyTraceStore(engine), engine
        finally:
            await engine.dispose()


async def _start(tracer: Tracer, identity: RunIdentity) -> RunObservationSession:
    source = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"id": "user", "role": "user", "content": "local"}]},
        config={},
        call_tracking_enabled=True,
    )
    session = await tracer.open_run(source)
    await session.observe(RunStartedObservation(**_source(identity, 1)))
    await session.observe(RunInputObservation(**_source(identity, 2), source=source))
    return session


async def _message(
    session: RunObservationSession,
    identity: RunIdentity,
    sequence: int,
    content: str,
    *,
    message_id: str = "assistant",
    namespace: tuple[str, ...] = (),
    graph_origin: GraphOrigin = GraphOrigin(),
    complete: bool = False,
) -> None:
    await session.observe(
        NativeMessageObservation(
            **_source(identity, sequence),
            graph_namespace=namespace,
            graph_origin=graph_origin,
            message=NativeMessageRecord(
                message_type="assistant" if complete else "assistant_chunk",
                id=message_id,
                content=content,
            ),
        )
    )


async def _finish(
    session: RunObservationSession,
    identity: RunIdentity,
    outcome: RunTerminalOutcome,
) -> None:
    await session.observe(
        RunTerminalObservation(**_source(identity, 90), outcome=outcome)
    )
    await session.observe(
        RunClosedObservation(**_source(identity, 91), outcome=outcome)
    )
    await session.aclose()


@pytest.mark.parametrize(
    "outcome", ("succeeded", "failed", "cancelled", "interrupted", "abandoned")
)
@pytest.mark.parametrize("complete", (False, True))
async def test_native_tail_after_model_callback_settles_at_run_drain(
    assistant_store: tuple[TraceStore, AsyncEngine | None],
    outcome: RunTerminalOutcome,
    complete: bool,
) -> None:
    store, _engine = assistant_store
    tracer = Tracer(store=store)
    identity = RunIdentity(namespace="test", thread_id="delivery", run_id="run")
    session = await _start(tracer, identity)
    await session.observe(
        ModelCallObservation(
            **_source(identity, 3),
            phase="started",
            call_id="model",
            messages=(NativeMessageRecord(message_type="human", content="local"),),
        )
    )
    await session.observe(
        ModelCallObservation(
            **_source(identity, 4),
            phase="first_output",
            call_id="model",
            output_message_ids=("assistant",),
        )
    )
    await _message(session, identity, 5, "first ")
    await session.observe(
        ModelCallObservation(
            **_source(identity, 6),
            phase="completed" if complete else "cancelled",
            call_id="model",
            output_message_ids=("assistant",) if complete else (),
        )
    )
    # Cross-channel delivery can lag the callback. The accepted tail belongs to the
    # same message and must not disappear merely because the model already ended.
    await _message(session, identity, 7, "tail")
    if complete:
        await _message(session, identity, 8, "first tail", complete=True)
    before = await tracer.query(identity.thread)
    before_message = next(
        n for n in before.nodes if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    assert before_message.content == ("first tail" if complete else None)
    await _finish(session, identity, outcome)
    graph = await tracer.query(identity.thread)
    message = next(
        n for n in graph.nodes if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    expected = (
        TraceGraphNodeStatus.SUCCEEDED
        if complete
        else {
            "cancelled": TraceGraphNodeStatus.CANCELLED,
            "interrupted": TraceGraphNodeStatus.WAITING,
        }.get(outcome, TraceGraphNodeStatus.ABANDONED)
    )
    assert message.status is expected
    assert message.content == "first tail"
    assert message.completed_at == (
        None
        if expected is TraceGraphNodeStatus.WAITING
        else NOW + timedelta(milliseconds=8 if complete else 90)
    )
    assert message.model_call_id == scope_id("model-call", (), "model")
    history = await tracer.get(identity.thread)
    assistant = next(m for m in history.messages if m.role == "assistant")
    assert assistant.content == "first tail"
    assert assistant.status == "completed"
    original = graph.snapshot.model_dump(mode="json")
    await tracer.rebuild_graph(identity.thread)
    rebuilt = await tracer.query(identity.thread)
    assert rebuilt.snapshot.model_dump(mode="json") == original


async def test_parallel_namespaces_completed_and_removed_messages_remain_independent(
    assistant_store: tuple[TraceStore, AsyncEngine | None],
) -> None:
    store, _engine = assistant_store
    tracer = Tracer(store=store)
    identity = RunIdentity(namespace="test", thread_id="parallel", run_id="run")
    session = await _start(tracer, identity)
    await _message(session, identity, 3, "root", message_id="same")
    await _message(
        session, identity, 4, "child", message_id="same", namespace=("planning:child",)
    )
    await _message(
        session, identity, 5, "finished", message_id="finished", complete=True
    )
    await _message(session, identity, 6, "remove", message_id="removed")
    await session.observe(
        NativeMessageObservation(
            **_source(identity, 7),
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="remove", id="removed", content=""
            ),
        )
    )
    await _finish(session, identity, "cancelled")
    graph = await tracer.query(identity.thread)
    assistants = {
        n.id: n for n in graph.nodes if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    }
    assert set(assistants) == {
        scope_id("message", (), "same"),
        scope_id("message", ("planning:child",), "same"),
        scope_id("message", (), "finished"),
    }
    assert assistants[scope_id("message", (), "same")].content == "root"
    assert (
        assistants[scope_id("message", ("planning:child",), "same")].content == "child"
    )
    assert (
        assistants[scope_id("message", (), "finished")].status
        is TraceGraphNodeStatus.SUCCEEDED
    )


@pytest.mark.parametrize("oversize_first", (False, True))
async def test_omitted_prefix_cannot_be_replaced_by_a_later_small_suffix(
    oversize_first: bool,
) -> None:
    limits = TraceLimits(max_event_bytes=4096)
    tracer = Tracer(limits=limits)
    identity = RunIdentity(namespace="test", thread_id="omitted", run_id="run")
    session = await _start(tracer, identity)
    chunks = (
        ("x" * 10000, "suffix")
        if oversize_first
        else ("x" * 1500, "y" * 1500, "suffix")
    )
    for sequence, chunk in enumerate(chunks, start=3):
        await _message(session, identity, sequence, chunk)
    await _finish(session, identity, "cancelled")
    graph = await tracer.query(identity.thread)
    message = next(
        n for n in graph.nodes if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    assert message.status is TraceGraphNodeStatus.CANCELLED
    assert message.content is None
    assert message.content_omitted


def _fact_source(identity: RunIdentity, sequence: int) -> dict[str, object]:
    return {
        "identity": identity,
        "occurred_at": NOW + timedelta(milliseconds=sequence),
        "monotonic_ns": sequence,
        "source_observation_id": f"source-{sequence}",
    }


async def test_committed_terminal_closes_only_its_active_assistant_and_rebuild_preserves_ledger(
    assistant_store: tuple[TraceStore, AsyncEngine | None],
) -> None:
    store, engine = assistant_store
    tracer = Tracer(store=store)
    identity = RunIdentity(namespace="test", thread_id="raw", run_id="run")
    writer = await store.open_writer(identity)
    shared = _fact_source(identity, 1)
    initial: tuple[TraceSemanticFact, ...] = (
        RunFact.model_validate(
            {**shared, "phase": "started", "input_kind": "ordinary"}
        ),
        TurnFact.model_validate(
            {**shared, "turn_id": "turn", "user_message_id": "user"}
        ),
        ModelCallFact.model_validate(
            {
                **_fact_source(identity, 2),
                "phase": "started",
                "call_id": "model",
                "context_started_at": NOW,
                "request": CapturedValue(
                    disposition="inline", safe_size_bytes=15, value={"messages": []}
                ),
                "system_message_positions": (),
                "output_message_ids": (),
            }
        ),
        ModelCallFact.model_validate(
            {
                **_fact_source(identity, 3),
                "phase": "first_output",
                "call_id": "model",
                "system_message_positions": (),
                "output_message_ids": ("different-output", "active", "last-output"),
            }
        ),
        MessageFact.model_validate(
            {
                **_fact_source(identity, 4),
                "phase": "started",
                "role": "assistant",
                "message_id": scope_id("message", (), "active"),
                "source_message_id": "active",
            }
        ),
        MessageFact.model_validate(
            {
                **_fact_source(identity, 5),
                "phase": "reconciled",
                "role": "assistant",
                "message_id": scope_id("message", (), "completed"),
                "source_message_id": "completed",
                "content": CapturedValue(
                    disposition="inline", safe_size_bytes=6, value="done"
                ),
            }
        ),
        InteractionFact.model_validate(
            {
                **_fact_source(identity, 6),
                "phase": "opened",
                "interaction_id": "approval",
                "source_interaction_id": "approval",
                "interaction_kind": "tool_approval",
                "status": "pending",
            }
        ),
        ToolFact.model_validate(
            {
                **_fact_source(identity, 7),
                "phase": "started",
                "tool_call_id": "tool",
                "source_tool_call_id": "tool",
                "tool_name": "write_file",
            }
        ),
    )
    await writer.append(initial)
    await tracer.query(identity.thread)
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    if engine is not None:
        sql_event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        await writer.append(
            (
                RunFact.model_validate(
                    {
                        **_fact_source(identity, 8),
                        "phase": "terminal",
                        "outcome": "cancelled",
                    }
                ),
            ),
            mandatory=True,
        )
    finally:
        if engine is not None:
            sql_event.remove(
                engine.sync_engine, "before_cursor_execute", record_statement
            )
    await writer.append(
        (
            RunFact.model_validate(
                {**_fact_source(identity, 9), "phase": "closed", "outcome": "cancelled"}
            ),
        ),
        mandatory=True,
    )
    await writer.aclose()
    snapshot = await store.snapshot(identity.thread)
    before = await store.read_events(
        snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
    )
    graph = await tracer.query(identity.thread)
    nodes = {n.id: n for n in graph.nodes}
    active = nodes[scope_id("message", (), "active")]
    assert active.status is TraceGraphNodeStatus.CANCELLED
    assert active.completed_at == NOW + timedelta(milliseconds=8)
    assert active.updated_seq == 9
    assert active.content is None
    assert active.model_call_id == "model"
    assert active.source_id == "active"
    assert (
        nodes[scope_id("message", (), "different-output")].source_id
        == "different-output"
    )
    history = await tracer.get(identity.thread)
    assert (
        next(node for node in history.graph.nodes if node.id == active.id).source_id
        == "active"
    )
    assert (
        nodes[scope_id("message", (), "completed")].status
        is TraceGraphNodeStatus.SUCCEEDED
    )
    assert nodes["approval"].status is TraceGraphNodeStatus.WAITING
    assert nodes[scope_id("tool", (), "tool")].status is TraceGraphNodeStatus.WAITING
    await tracer.rebuild_graph(identity.thread)
    after = await store.read_events(
        snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
    )
    assert after == before
    assert (await tracer.query(identity.thread)).snapshot.model_dump(
        mode="json"
    ) == graph.snapshot.model_dump(mode="json")
    if engine is not None:
        terminal_updates = [
            s for s in statements if s.startswith("UPDATE tinkerfin_trace_graph_nodes")
        ]
        assert len(terminal_updates) == 1
        assert "run_hash" in terminal_updates[0] and "status" in terminal_updates[0]
        assert not any(
            "SELECT" in s.upper() and "tinkerfin_trace_graph_nodes" in s
            for s in statements
        )
        # A genuine Run terminal must not legitimize a forged closure of an already
        # complete message. Its original result locator disproves that mutation.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_graph_nodes SET status = 'cancelled', "
                    "completed_at = :stamp, updated_seq = 9 "
                    "WHERE generation = :generation AND node_id = :node_id"
                ),
                {
                    "stamp": (NOW + timedelta(milliseconds=8)).replace(tzinfo=None),
                    "generation": snapshot.key.generation,
                    "node_id": json.dumps(
                        scope_id("message", (), "completed"), ensure_ascii=False
                    ),
                },
            )
        with pytest.raises(TraceStoreProtocolError):
            await tracer.query(identity.thread)
        await tracer.rebuild_graph(identity.thread)
        assert (await tracer.query(identity.thread)).snapshot == graph.snapshot


class _WholeMessageRedactor:
    def redact(self, value: JsonValue, *, context: RedactionContext) -> JsonValue:
        if context.content_kind == "message" and isinstance(value, str):
            return value.replace("private-value", "[REDACTED]")
        return value


async def test_assembled_partial_content_is_redacted_and_empty_output_is_not_invented() -> (
    None
):
    tracer = Tracer(redactor=_WholeMessageRedactor())
    identity = RunIdentity(namespace="test", thread_id="redaction", run_id="run")
    session = await _start(tracer, identity)
    await _message(session, identity, 3, "private-")
    await _message(session, identity, 4, "value")
    await _message(session, identity, 5, "", message_id="empty")
    await _finish(session, identity, "cancelled")
    nodes = {n.id: n for n in (await tracer.query(identity.thread)).nodes}
    assert nodes[scope_id("message", (), "assistant")].content == "[REDACTED]"
    assert nodes[scope_id("message", (), "empty")].content is None
    assert (
        nodes[scope_id("message", (), "empty")].status is TraceGraphNodeStatus.CANCELLED
    )


async def test_complete_snapshot_can_replace_an_omitted_partial_prefix() -> None:
    tracer = Tracer(limits=TraceLimits(max_event_bytes=4096))
    identity = RunIdentity(namespace="test", thread_id="snapshot", run_id="run")
    session = await _start(tracer, identity)
    await _message(session, identity, 3, "x" * 10000)
    await _message(session, identity, 4, "real snapshot", complete=True)
    await _finish(session, identity, "cancelled")
    message = next(
        n
        for n in (await tracer.query(identity.thread)).nodes
        if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    assert message.content == "real snapshot"
    assert not message.content_omitted
    assert message.status is TraceGraphNodeStatus.SUCCEEDED


@pytest.mark.parametrize("complete_state", (False, True))
async def test_model_success_and_native_response_completeness_have_separate_evidence(
    assistant_store: tuple[TraceStore, AsyncEngine | None],
    complete_state: bool,
) -> None:
    store, _engine = assistant_store
    tracer = Tracer(store=store)
    identity = RunIdentity(
        namespace="test", thread_id="completion-evidence", run_id="run"
    )
    session = await _start(tracer, identity)
    await session.observe(
        ModelCallObservation(
            **_source(identity, 3),
            phase="started",
            call_id="model",
            messages=(NativeMessageRecord(message_type="human", content="local"),),
        )
    )
    await session.observe(
        ModelCallObservation(
            **_source(identity, 4),
            phase="first_output",
            call_id="model",
            output_message_ids=("assistant",),
        )
    )
    await _message(session, identity, 5, "partial")
    await session.observe(
        ModelCallObservation(
            **_source(identity, 6),
            phase="completed",
            call_id="model",
            output_message_ids=("assistant",),
        )
    )
    if complete_state:
        await session.observe(
            NativeStateObservation(
                **_source(identity, 7),
                graph_namespace=(),
                state={},
                messages=(
                    NativeMessageRecord(
                        message_type="assistant",
                        id="assistant",
                        content="complete response",
                    ),
                ),
            )
        )
    await _finish(session, identity, "succeeded")
    graph = await tracer.query(identity.thread)
    model = next(n for n in graph.nodes if n.kind is TraceGraphNodeKind.MODEL)
    message = next(
        n for n in graph.nodes if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    assert model.status is TraceGraphNodeStatus.SUCCEEDED
    assert message.status is TraceGraphNodeStatus.SUCCEEDED
    assert message.content == ("complete response" if complete_state else None)
    assert message.content_omitted is not complete_state
    history = await tracer.get(identity.thread)
    assistant = next(m for m in history.messages if m.role == "assistant")
    assert assistant.status == "completed"
    assert assistant.content_omitted is not complete_state
    if not complete_state:
        snapshot = await store.snapshot(identity.thread)
        events = await store.read_events(
            snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
        )
        completed = next(
            e.fact
            for e in events
            if isinstance(e.fact, MessageFact)
            and e.fact.phase == "completed"
            and e.fact.role == "assistant"
        )
        assert (
            completed.content is not None
            and completed.content.reason == "incomplete_message"
        )


async def test_interrupted_message_can_resume_with_its_same_id_without_duplicate_content(
    assistant_store: tuple[TraceStore, AsyncEngine | None],
) -> None:
    store, _engine = assistant_store
    tracer = Tracer(store=store)
    first = RunIdentity(namespace="test", thread_id="resume-message", run_id="first")
    session = await _start(tracer, first)
    await _message(session, first, 3, "first ")
    await _finish(session, first, "interrupted")
    waiting = next(
        n
        for n in (await tracer.query(first.thread)).nodes
        if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    assert waiting.status is TraceGraphNodeStatus.WAITING
    second = RunIdentity(namespace="test", thread_id=first.thread_id, run_id="second")
    source = RunSourceContext(
        identity=second,
        runtime_profile="deepagents-v2",
        input_kind="resume",
        parent_run_id=first.run_id,
        input=None,
        config={},
    )
    resumed = await tracer.open_run(source)
    await resumed.observe(RunStartedObservation(**_source(second, 101)))
    await resumed.observe(RunInputObservation(**_source(second, 102), source=source))
    await _message(resumed, second, 103, "second")
    current = await tracer.get(first.thread)
    assert (
        next(m for m in current.messages if m.role == "assistant").status == "streaming"
    )
    await _message(resumed, second, 104, "first second", complete=True)
    await resumed.observe(
        RunTerminalObservation(**_source(second, 190), outcome="succeeded")
    )
    await resumed.observe(
        RunClosedObservation(**_source(second, 191), outcome="succeeded")
    )
    await resumed.aclose()
    result = await tracer.get(first.thread)
    messages = [m for m in result.messages if m.role == "assistant"]
    assert len(messages) == 1 and messages[0].content == "first second"
    assert messages[0].status == "completed"
    node = next(
        n for n in result.graph.nodes if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    assert node.id == waiting.id and node.status is TraceGraphNodeStatus.SUCCEEDED
    await tracer.rebuild_graph(first.thread)
    assert (await tracer.get(first.thread)).graph == result.graph


async def test_assistant_settlement_respects_ordinary_event_quota() -> None:
    tracer = Tracer(limits=TraceLimits(max_thread_events=16))
    identity = RunIdentity(namespace="test", thread_id="quota", run_id="run")
    session = await _start(tracer, identity)
    for sequence in range(3, 9):
        await _message(
            session, identity, sequence, "part", message_id=f"message-{sequence}"
        )
    # Starts fit the ordinary quota, but six additional retained deliveries do not.
    await tracer.query(identity.thread)
    with pytest.raises(TraceQuotaExceeded):
        await session.observe(
            RunTerminalObservation(**_source(identity, 90), outcome="cancelled")
        )
    await session.aclose()
    thread = await tracer.get(identity.thread)
    assert thread.status.execution == "unknown"
    assert thread.completeness.missing_tail


@pytest.mark.parametrize("parent_result", (False, True))
async def test_child_assistant_uses_its_own_native_scope_at_cancellation(
    assistant_store: tuple[TraceStore, AsyncEngine | None],
    parent_result: bool,
) -> None:
    store, _engine = assistant_store
    tracer = Tracer(store=store)
    identity = RunIdentity(namespace="test", thread_id="child", run_id="run")
    child = ("tools:parent-task",)
    request = SubagentRequestReference(
        id=subagent_request_id(child),
        parent_graph_namespace=(),
        parent_tool_call_id="delegate",
        graph_task_id="parent-task",
        agent_name="worker",
        description="local child",
    )
    origin = GraphOrigin(
        parent_task=GraphTaskReference(
            graph_namespace=(), task_id="parent-task", node_name="tools"
        ),
        subagent_request=request,
    )
    session = await _start(tracer, identity)
    await session.observe(
        ModelCallObservation(
            **_source(identity, 3),
            phase="started",
            call_id="parent",
            messages=(NativeMessageRecord(message_type="human", content="delegate"),),
        )
    )
    await session.observe(
        ModelCallObservation(
            **_source(identity, 4),
            phase="completed",
            call_id="parent",
            tool_call_ids=("delegate",),
        )
    )
    await session.observe(
        NativeTaskObservation(
            **_source(identity, 5),
            graph_namespace=(),
            subagent_requests=(request,),
            phase="start",
            task_id="parent-task",
            name="tools",
            input=[
                {
                    "id": "delegate",
                    "name": "task",
                    "args": {"description": "local child", "subagent_type": "worker"},
                }
            ],
        )
    )
    await session.observe(
        ToolExecutionObservation(
            **_source(identity, 6),
            graph_namespace=(),
            graph_task_id="parent-task",
            delegation=request,
            phase="started",
            execution_id="parent-execution",
            tool_call_id="delegate",
            tool_call_namespace=(),
            tool_name="task",
            input={"description": "local child", "subagent_type": "worker"},
        )
    )
    await session.observe(
        ModelCallObservation(
            **_source(identity, 7),
            graph_namespace=child,
            graph_origin=origin,
            phase="started",
            call_id="child-model",
            messages=(NativeMessageRecord(message_type="human", content="child"),),
        )
    )
    await session.observe(
        ModelCallObservation(
            **_source(identity, 8),
            graph_namespace=child,
            graph_origin=origin,
            phase="first_output",
            call_id="child-model",
            output_message_ids=("assistant",),
        )
    )
    await _message(
        session, identity, 9, "child partial", namespace=child, graph_origin=origin
    )
    if parent_result:
        await session.observe(
            NativeTaskObservation(
                **_source(identity, 10),
                graph_namespace=(),
                phase="result",
                task_id="parent-task",
                name="tools",
                result={},
            )
        )
    await _finish(session, identity, "cancelled")
    graph = await tracer.query(identity.thread)
    message = next(
        n for n in graph.nodes if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    owner = next(n for n in graph.nodes if n.kind is TraceGraphNodeKind.SUBAGENT)
    assert message.graph_namespace == child and message.parent_subagent_id == owner.id
    assert message.status is TraceGraphNodeStatus.CANCELLED
    assert message.content == "child partial"
    await tracer.rebuild_graph(identity.thread)
    assert (await tracer.query(identity.thread)).snapshot == graph.snapshot


@pytest.mark.parametrize("phase", ("cancelled", "interrupted", "abandoned"))
def test_partial_phases_do_not_claim_state_snapshots_or_non_assistant_roles(
    phase: str,
) -> None:
    identity = RunIdentity(namespace="test", thread_id="fact", run_id="run")
    values = {
        **_fact_source(identity, 1),
        "phase": phase,
        "message_id": "message",
        "role": "assistant",
    }
    fact = MessageFact.model_validate(values)
    assert MessageFact.model_validate_json(fact.model_dump_json()) == fact
    for invalid in ({"role": "user"}, {"from_state_snapshot": True}):
        with pytest.raises(ValidationError, match="Assistant delivery"):
            MessageFact.model_validate({**values, **invalid})


async def test_terminal_cannot_close_or_supply_a_locator_for_another_run(
    assistant_store: tuple[TraceStore, AsyncEngine | None],
) -> None:
    store, engine = assistant_store
    tracer = Tracer(store=store)
    first = RunIdentity(namespace="test", thread_id="run-isolation", run_id="first")
    second = RunIdentity(namespace="test", thread_id=first.thread_id, run_id="second")
    writers = [await store.open_writer(first), await store.open_writer(second)]
    try:
        for identity, writer in zip((first, second), writers, strict=True):
            source = _fact_source(identity, 1)
            await writer.append(
                (
                    RunFact.model_validate(
                        {
                            **source,
                            "phase": "started",
                            "input_kind": "ordinary",
                            "parent_run_id": None
                            if identity == first
                            else first.run_id,
                        }
                    ),
                    TurnFact.model_validate(
                        {
                            **source,
                            "turn_id": identity.run_id,
                            "user_message_id": f"user-{identity.run_id}",
                            "parent_run_id": None
                            if identity == first
                            else first.run_id,
                        }
                    ),
                    MessageFact.model_validate(
                        {
                            **source,
                            "phase": "started",
                            "role": "assistant",
                            "message_id": scope_id("message", (), identity.run_id),
                            "source_message_id": identity.run_id,
                        }
                    ),
                )
            )
        terminal = await writers[1].append(
            (
                RunFact.model_validate(
                    {
                        **_fact_source(second, 2),
                        "phase": "terminal",
                        "outcome": "cancelled",
                    }
                ),
            ),
            mandatory=True,
        )
        graph = await tracer.query(first.thread, head_run_id=second.run_id)
        nodes = {n.id: n for n in graph.nodes}
        assert (
            nodes[scope_id("message", (), first.run_id)].status
            is TraceGraphNodeStatus.RUNNING
        )
        assert (
            nodes[scope_id("message", (), second.run_id)].status
            is TraceGraphNodeStatus.CANCELLED
        )
        if engine is not None:
            snapshot = await store.snapshot(first.thread)
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE tinkerfin_trace_graph_nodes SET status = 'cancelled', "
                        "completed_at = :stamp, updated_seq = :sequence "
                        "WHERE generation = :generation AND node_id = :node_id"
                    ),
                    {
                        "stamp": (NOW + timedelta(milliseconds=2)).replace(tzinfo=None),
                        "sequence": terminal[-1].trace_seq,
                        "generation": snapshot.key.generation,
                        "node_id": json.dumps(
                            scope_id("message", (), first.run_id), ensure_ascii=False
                        ),
                    },
                )
            with pytest.raises(TraceStoreProtocolError):
                await tracer.query(first.thread, head_run_id=second.run_id)
        await tracer.rebuild_graph(first.thread)
        assert (
            await tracer.query(first.thread, head_run_id=second.run_id)
        ).snapshot == graph.snapshot
    finally:
        for writer in writers:
            await writer.aclose()


@pytest.mark.parametrize("unchecked", ("copy", "construct"))
@pytest.mark.parametrize("phase", ("started", "terminal", "closed"))
@pytest.mark.parametrize(
    "invalid_scope",
    ({"graph_namespace": ("child",)}, {"parent_subagent_id": "subagent:child"}),
)
async def test_unchecked_run_scope_is_rejected_atomically_before_any_batch_fact(
    assistant_store: tuple[TraceStore, AsyncEngine | None],
    unchecked: str,
    phase: str,
    invalid_scope: dict[str, object],
) -> None:
    store, _engine = assistant_store
    tracer = Tracer(store=store)
    identity = RunIdentity(
        namespace="test", thread_id="invalid-run-scope", run_id="run"
    )
    writer = await store.open_writer(identity)
    try:
        await writer.append(
            (
                RunFact.model_validate(
                    {
                        **_fact_source(identity, 1),
                        "phase": "started",
                        "input_kind": "ordinary",
                    }
                ),
                TurnFact.model_validate(
                    {
                        **_fact_source(identity, 2),
                        "turn_id": "turn",
                        "user_message_id": "user",
                    }
                ),
                MessageFact.model_validate(
                    {
                        **_fact_source(identity, 3),
                        "phase": "started",
                        "role": "assistant",
                        "message_id": scope_id("message", (), "assistant"),
                        "source_message_id": "assistant",
                    }
                ),
            )
        )
        snapshot = await store.snapshot(identity.thread)
        original_events = await store.read_events(
            snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
        )
        original_graph = (await tracer.query(identity.thread)).snapshot
        root_terminal = RunFact.model_validate(
            {**_fact_source(identity, 4), "phase": "terminal", "outcome": "cancelled"}
        )
        root_closed = RunFact.model_validate(
            {**_fact_source(identity, 5), "phase": "closed", "outcome": "cancelled"}
        )
        valid = RunFact.model_validate(
            {
                **_fact_source(identity, 4),
                "phase": phase,
                **(
                    {"input_kind": "ordinary"}
                    if phase == "started"
                    else {"outcome": "cancelled"}
                ),
            }
        )
        malformed = (
            valid.model_copy(update=invalid_scope)
            if unchecked == "copy"
            else RunFact.model_construct(
                **{
                    **{field: getattr(valid, field) for field in RunFact.model_fields},
                    **invalid_scope,
                }
            )
        )
        # A valid leading fact must not commit when a later unchecked fact is invalid.
        batch = (
            (root_terminal, malformed)
            if phase == "closed"
            else (malformed, root_closed)
            if phase == "terminal"
            else (
                MessageFact.model_validate(
                    {
                        **_fact_source(identity, 4),
                        "phase": "started",
                        "role": "assistant",
                        "message_id": scope_id("message", (), "other"),
                        "source_message_id": "other",
                    }
                ),
                malformed,
            )
        )
        with pytest.raises(TraceStoreProtocolError, match="root scope"):
            await writer.append(batch, mandatory=phase != "started")
        after = await store.snapshot(identity.thread)
        assert after.key == snapshot.key and after.as_of_seq == snapshot.as_of_seq
        assert (
            await store.read_events(
                after.key, after_seq=0, as_of_seq=after.as_of_seq, limit=100
            )
            == original_events
        )
        assert (await tracer.query(identity.thread)).snapshot == original_graph
        # Terminal/closed admission flags and reserves also remain usable after rejection.
        await writer.append((root_terminal, root_closed), mandatory=True)
        graph = await tracer.query(identity.thread)
        assert (
            next(
                n for n in graph.nodes if n.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
            ).status
            is TraceGraphNodeStatus.CANCELLED
        )
    finally:
        await writer.aclose()
