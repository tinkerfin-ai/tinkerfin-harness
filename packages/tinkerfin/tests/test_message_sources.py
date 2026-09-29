"""Host context keeps its model role without becoming another user request."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from ag_ui.core import RunFinishedEvent, UserMessage
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import JsonValue, ValidationError
from sqlalchemy.ext.asyncio import create_async_engine
from test_agui_history import _call, _resume, save_report
from test_plan_mode import _FakeModel

from tinkerfin import TinkerFin
from tinkerfin_agui_adapter import AttachmentMessagesSnapshotEvent
from tinkerfin_agui_adapter.media import user_message_to_langchain
from tinkerfin_contracts import MessageSource
from tinkerfin_tracing import SqlAlchemyTraceStore, Tracer


@pytest.fixture(params=["memory", "sqlite"])
async def source_tracer(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Tracer]:
    """Exercise retained provenance with both public Trace Store implementations."""
    if request.param == "memory":
        yield Tracer()
        return
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sources.db'}")
    try:
        store = SqlAlchemyTraceStore(engine)
        await store.setup()
        yield Tracer(store=store)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "content", ["use this document", [{"type": "text", "text": "use this document"}]]
)
def test_authorized_source_survives_user_input_conversion(content: JsonValue) -> None:
    source = MessageSource(
        kind="context", name="retrieval", metadata={"document": "report"}
    )
    message = UserMessage.model_validate(
        {
            "id": "context",
            "role": "user",
            "content": content,
            "source": source.model_dump(mode="json"),
        }
    )
    native = user_message_to_langchain(message)
    assert native.id == "context"
    assert (
        MessageSource.model_validate(native.additional_kwargs["tinkerfin_source"])
        == source
    )
    assert native.content == content
    assert (
        "tinkerfin_source"
        not in user_message_to_langchain(
            UserMessage(id="user", content="question")
        ).additional_kwargs
    )


@pytest.mark.parametrize(
    "source",
    [{"kind": "unknown"}, {"kind": "context", "metadata": {"unsafe": float("nan")}}],
)
def test_invalid_context_source_is_rejected(source: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        user_message_to_langchain(
            UserMessage.model_validate(
                {"id": "context", "content": "document", "source": source}
            )
        )


async def test_context_persists_across_turns_and_history_queries(
    source_tracer: Tracer,
) -> None:
    model = _FakeModel(
        responses=[
            AIMessage(content="first", id="reply-1"),
            AIMessage(content="second", id="reply-2"),
        ]
    )
    tracer = source_tracer
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("documents")
        .with_observer(tracer)
        .build(model=model, tools=[])
    )
    source = MessageSource(
        kind="context", name="retrieval", metadata={"document": "report"}
    )
    first = runtime.open_agui_run(
        thread_id="thread",
        run_id="first",
        messages=[
            {"id": "request", "role": "user", "content": "  请分析 /report\n"},
            {
                "id": "context",
                "role": "user",
                "content": "Exact report instructions",
                "source": source.model_dump(mode="json"),
            },
        ],
    )
    events = [event async for event in first]
    assert first.error is None
    assert sum(event.type == "RUN_FINISHED" for event in events) == 1
    inputs = [
        message
        for message in model.model_inputs[0]
        if isinstance(message, HumanMessage)
    ]
    assert [(message.id, message.content) for message in inputs] == [
        ("request", "  请分析 /report\n"),
        ("context", "Exact report instructions"),
    ]
    before = (
        await runtime.agui.history(Tracer(store=tracer.store)).get("thread")
    ).snapshot
    saved = next(message for message in before.messages if message.source)
    assert saved.source == source
    assert saved.content == "Exact report instructions"
    assert len(before.graph.turns) == 1
    assert (
        len([node for node in before.graph.nodes if node.kind == "human_message"]) == 1
    )
    second = runtime.open_agui_run(
        thread_id="thread",
        run_id="second",
        messages=[{"id": "next", "role": "user", "content": "继续"}],
    )
    _ = [event async for event in second]
    assert second.error is None
    assert [
        message.id
        for message in model.model_inputs[-1]
        if isinstance(message, HumanMessage)
    ] == ["request", "context", "next"]
    after = (
        await runtime.agui.history(Tracer(store=tracer.store)).get("thread")
    ).snapshot
    assert len([message for message in after.messages if message.source]) == 1
    assert len(after.graph.turns) == 2


async def test_context_keeps_one_identity_after_interrupt_and_resume() -> None:
    model = _FakeModel(
        responses=[_call("save_report", "save"), AIMessage(id="done", content="Saved")]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("documents")
        .with_observer(tracer)
        .build(model=model, tools=[save_report], interrupt_on={"save_report": True})
    )
    source = MessageSource(kind="context", name="retrieval")
    first = runtime.open_agui_run(
        thread_id="thread",
        run_id="first",
        messages=[
            {"id": "request", "role": "user", "content": "Save report"},
            {
                "id": "context",
                "role": "user",
                "content": "Retained instructions",
                "source": source.model_dump(mode="json"),
            },
        ],
    )
    events = [event async for event in first]
    assert first.error is None
    snapshots = [
        event for event in events if isinstance(event, AttachmentMessagesSnapshotEvent)
    ]
    context = next(
        message
        for message in snapshots[-1].messages
        if (message.model_extra or {}).get("source")
    )
    assert MessageSource.model_validate((context.model_extra or {})["source"]) == source
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    second = runtime.open_agui_run(
        thread_id="thread", run_id="resume", resume=_resume(terminal, "approve")
    )
    resumed = [event async for event in second]
    assert second.error is None
    assert sum(event.type == "RUN_FINISHED" for event in resumed) == 1
    snapshot = (await runtime.agui.history(tracer).get("thread")).snapshot
    assert len(snapshot.graph.turns) == 1
    assert [
        (message.source, message.content)
        for message in snapshot.messages
        if message.source
    ] == [(source, "Retained instructions")]
    assert [
        message.id
        for message in model.model_inputs[-1]
        if isinstance(message, HumanMessage)
    ] == ["request", "context"]
