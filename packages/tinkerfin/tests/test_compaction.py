"""Manual compression shares saved context without creating chat messages."""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import StateBackend
from deepagents.backends.protocol import WriteResult
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    HumanMessageChunk,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, TypeAdapter
from test_runtime_store import _Model

from tinkerfin import AgentRuntime, TinkerFin, TinkerFinLifecycleError
from tinkerfin.runtime_profile import DeepAgentsV3RuntimeProfile
from tinkerfin_tracing import Tracer


class RecordingModel(_Model):
    inputs: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        self.inputs.append(messages)
        response = self.responses[self.i].model_copy(
            update={
                "usage_metadata": {
                    "input_tokens": 5900,
                    "output_tokens": 100,
                    "total_tokens": 6000,
                },
                "response_metadata": {
                    "model_provider": self._get_ls_params().get("ls_provider")
                },
            }
        )
        self.i = (self.i + 1) % len(self.responses)
        return ChatResult(generations=[ChatGeneration(message=response)])


def model_with(*responses: str) -> RecordingModel:
    return RecordingModel(responses=[AIMessage(content=value) for value in responses])


def eligible_policy(
    model: RecordingModel, backend: StateBackend | None = None
) -> SummarizationMiddleware:
    """Keep lifecycle fixtures eligible for manual but below automatic thresholds."""
    return SummarizationMiddleware(
        model,
        backend=backend or StateBackend(),
        trigger=("tokens", 10000),
        keep=("messages", 1),
    )


def history(prefix: str = "") -> list[AnyMessage | dict[str, Any]]:
    return [
        HumanMessage(
            id=f"{prefix}first", content="Remember project requirements. " * 100
        ),
        AIMessage(id=f"{prefix}reply", content="Earlier investigation results. " * 100),
        HumanMessage(
            id=f"{prefix}second",
            content="Preserve report /attachments/notes.txt. " * 40,
        ),
    ]


def message_ids(messages: Sequence[BaseMessage]) -> list[str | None]:
    return [message.id for message in messages]


def saved_messages(state: Mapping[str, object]) -> list[AnyMessage]:
    return TypeAdapter(list[AnyMessage]).validate_python(state["messages"])


@pytest.mark.parametrize("plan_enabled", [False, True])
async def test_manual_compression_below_threshold_preserves_history_and_next_turn(
    plan_enabled: bool,
) -> None:
    model = model_with(
        "Latest answer", "Project requirements and notes.txt", "Continued"
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .with_plan(enabled=plan_enabled)
        .build(model=model, middleware=[eligible_policy(model)])
    )
    before = await runtime.ainvoke(
        thread_id="t", run_id="chat", input={"messages": history()}
    )
    result = await runtime.compact(thread_id="t", run_id="compact")
    assert result.status == "compacted"
    assert result.compacted_messages == 3
    assert result.summary == "Project requirements and notes.txt"
    assert len(model.inputs) == 2
    assert "/attachments/notes.txt" in str(model.inputs[1])
    repeat = await runtime.compact(thread_id="t", run_id="repeat")
    assert repeat.status == "nothing_to_compact"
    assert len(model.inputs) == 2
    after = await runtime.ainvoke(
        thread_id="t",
        run_id="next",
        input={"messages": [HumanMessage(content="Continue")]},
    )
    assert message_ids(saved_messages(after)[:4]) == message_ids(saved_messages(before))
    assert len(saved_messages(after)) == 6
    assert "Project requirements and notes.txt" in str(model.inputs[-1])
    assert "Earlier investigation results." not in str(model.inputs[-1])
    assert "Earlier investigation results." in str(after["files"])


async def test_empty_conversation_is_a_noop_without_calling_model() -> None:
    model = model_with("must not be called")
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .with_observer(tracer)
        .build(model=model, middleware=[eligible_policy(model)])
    )
    result = await runtime.compact(thread_id="empty", run_id="compact")
    assert result.status == "nothing_to_compact"
    assert not model.inputs
    nodes = (await runtime.agui.history(tracer).get("empty")).snapshot.graph.nodes
    context = next(node for node in nodes if node.kind == "context")
    assert context.content is None
    assert not context.content_omitted
    assert not any(node.kind == "model" for node in nodes)


@pytest.mark.parametrize("transport", ["native", "agui"])
@pytest.mark.parametrize(
    "message_form", ["native", "native_chunk", "user", "human", "human_type"]
)
@pytest.mark.parametrize("user_id", [None, "user-message"])
async def test_user_input_has_one_real_graph_message(
    transport: str, message_form: str, user_id: str | None
) -> None:
    from copy import deepcopy

    tracer = Tracer()
    model = model_with("Answer")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .with_observer(tracer)
        .build(model=model, middleware=[eligible_policy(model)])
    )
    message: AnyMessage | dict[str, Any]
    if message_form == "native":
        message = HumanMessage(content="Question", id=user_id)
    elif message_form == "native_chunk":
        message = HumanMessageChunk(content="Question", id=user_id)
    elif message_form == "human_type":
        message = {"type": "human", "content": "Question", "id": user_id}
    else:
        message = {"role": message_form, "content": "Question", "id": user_id}
    original = deepcopy(message)
    if transport == "native":
        await runtime.ainvoke(
            thread_id="t", run_id="chat", input={"messages": [message]}
        )
    else:
        stream = runtime.open_agui_run(
            thread_id="t", run_id="chat", input={"messages": [message]}
        )
        try:
            async for _event in stream:
                pass
        finally:
            await stream.aclose()
    snapshot = (await runtime.agui.history(tracer).get("t")).snapshot
    humans = [node for node in snapshot.graph.nodes if node.kind == "human_message"]
    assert len(humans) == 1
    assert humans[0].content == "Question"
    assert humans[0].source_id == next(
        item.id for item in model.inputs[0] if isinstance(item, HumanMessage)
    )
    if user_id is not None:
        assert humans[0].source_id == user_id
    assert message == original


@pytest.mark.parametrize("transport", ["native", "agui"])
async def test_user_input_remains_in_graph_when_preparation_fails(
    transport: str, definition_factory: Callable[..., AgentRuntime[None]]
) -> None:
    tracer = Tracer()
    runtime = definition_factory(
        RuntimeError("model setup failed"),
        tinkerfin=TinkerFin().with_namespace("alpha").with_observer(tracer),
    )
    message = HumanMessage(content="Question")
    if transport == "native":
        with pytest.raises(RuntimeError, match="model setup failed"):
            await runtime.ainvoke(
                thread_id="t", run_id="chat", input={"messages": [message]}
            )
    else:
        stream = runtime.open_agui_run(
            thread_id="t", run_id="chat", input={"messages": [message]}
        )
        try:
            events = [event.type async for event in stream]
        finally:
            await stream.aclose()
        assert events == ["RUN_STARTED", "RUN_ERROR"]
    snapshot = (await runtime.agui.history(tracer).get("t")).snapshot
    humans = [node for node in snapshot.graph.nodes if node.kind == "human_message"]
    assert len(humans) == 1
    assert humans[0].content == "Question"
    assert humans[0].source_id
    assert message.id is None


async def test_manual_compaction_uses_the_effective_summary_middleware() -> None:
    backend = StateBackend()
    first = model_with("First summary")
    last = model_with("Last summary")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .build(
            model=model_with("Answer"),
            backend=backend,
            middleware=[
                SummarizationMiddleware(
                    model,
                    backend=backend,
                    trigger=("messages", 6),
                    keep=("messages", 1),
                    summary_prompt=f"{label} instructions: {{messages}}",
                )
                for label, model in (("First", first), ("Last", last))
            ],
        )
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    result = await runtime.compact(thread_id="t", run_id="compact")
    assert result.summary == "Last summary"
    assert not first.inputs
    assert len(last.inputs) == 1
    assert "Last instructions:" in last.inputs[0][0].text


@pytest.mark.parametrize("summary", ["", "Longer summary " * 3000])
async def test_unsuitable_summary_keeps_original_effective_context(
    summary: str,
) -> None:
    model = model_with("Latest answer", summary, "Continued")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    if summary:
        result = await runtime.compact(thread_id="t", run_id="compact")
        assert result.status == "not_reduced"
    else:
        with pytest.raises(TinkerFinLifecycleError, match="empty summary"):
            await runtime.compact(thread_id="t", run_id="compact")
    after = await runtime.ainvoke(
        thread_id="t",
        run_id="next",
        input={"messages": [HumanMessage(content="Continue")]},
    )
    assert len(saved_messages(after)) == 6
    assert "Earlier investigation results." in str(model.inputs[-1])


async def test_pending_work_is_rejected_before_summary_generation() -> None:
    model = model_with("must not be called")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(
        thread_id="t",
        run_id="paused",
        input={"messages": history()},
        interrupt_before=["model"],
    )
    with pytest.raises(TinkerFinLifecycleError, match="pending work"):
        await runtime.compact(thread_id="t", run_id="compact")
    assert not model.inputs


async def test_automatic_compression_continues_from_manual_summary() -> None:
    model = model_with(
        "First answer", "Manual summary", "Automatic summary", "Final answer"
    )
    backend = StateBackend()
    middleware = SummarizationMiddleware(
        model, backend=backend, trigger=("messages", 6), keep=("messages", 1)
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .build(model=model, backend=backend, middleware=[middleware])
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    assert (
        await runtime.compact(thread_id="t", run_id="compact")
    ).status == "compacted"
    after = await runtime.ainvoke(
        thread_id="t",
        run_id="next",
        input={"messages": [*history("new-"), HumanMessage(content="Continue")]},
    )
    assert len(model.inputs) == 4
    assert "Manual summary" in str(model.inputs[2])
    assert "Automatic summary" in str(model.inputs[3])
    assert len(saved_messages(after)) == 9


async def test_archive_failure_does_not_replace_context() -> None:
    class FailedArchive(StateBackend):
        async def awrite(self, file_path: str, content: str) -> WriteResult:
            del file_path, content
            return WriteResult(error="archive unavailable")

    model = model_with("Answer", "Short summary", "Continued")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .build(
            model=model,
            backend=FailedArchive(),
            middleware=[eligible_policy(model, FailedArchive())],
        )
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    with pytest.raises(TinkerFinLifecycleError, match="archive"):
        await runtime.compact(thread_id="t", run_id="compact")
    await runtime.ainvoke(
        thread_id="t",
        run_id="next",
        input={"messages": [HumanMessage(content="Continue")]},
    )
    assert "Earlier investigation results." in str(model.inputs[-1])


async def test_compaction_agui_has_one_terminal_and_no_assistant_messages() -> None:
    model = model_with("Answer", "Short summary")
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .with_observer(tracer)
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    before = (await runtime.agui.history(tracer).get("t")).snapshot
    stream = runtime.agui.open_compaction(thread_id="t", run_id="compact")
    try:
        events = [
            event.model_dump(mode="json", by_alias=True) async for event in stream
        ]
    finally:
        await stream.aclose()
    types = [event["type"] for event in events]
    assert types.count("RUN_STARTED") == types.count("RUN_FINISHED") == 1
    assert not any(kind.startswith(("TEXT_MESSAGE_", "TOOL_CALL_")) for kind in types)
    assert "Short summary" in str(events)
    assert "_summarization_event" not in str(events)
    after = (await runtime.agui.history(tracer).get("t")).snapshot
    assert after.messages == before.messages
    compact_nodes = [node for node in after.graph.nodes if node.run_id == "compact"]
    assert not any(
        node.kind in {"human_message", "assistant_message"} for node in compact_nodes
    )
    model_nodes = [node for node in compact_nodes if node.kind == "model"]
    assert len(model_nodes) == 1
    contexts = [node for node in compact_nodes if node.kind == "context"]
    assert len(contexts) == 1
    assert contexts[0].content is None
    assert contexts[0].result is None
    actions = [node for node in compact_nodes if node.name == "context_compaction"]
    assert len(actions) == 1
    assert actions[0].parent_node_id == contexts[0].id
    assert model_nodes[0].parent_node_id == actions[0].id
    assert "Earlier investigation results." in str(actions[0].request)
    assert "Earlier investigation results." in str(model_nodes[0].request)
    operation = next(
        node for node in after.graph.nodes if node.name == "context_compaction"
    )
    assert operation.status == "succeeded"
    assert operation.result == {
        "run_id": "compact",
        "status": "compacted",
        "summary": "Short summary",
        "generated_summary": "Short summary",
        "compacted_messages": 3,
    }


async def test_compaction_initialization_failure_remains_in_history() -> None:
    tracer = Tracer()
    runtime = (
        TinkerFin()
        .with_namespace("alpha")
        .with_observer(tracer)
        .build(model=model_with("Unused"))
    )
    stream = runtime.agui.open_compaction(thread_id="t", run_id="compact")
    try:
        events = [event.type async for event in stream]
    finally:
        await stream.aclose()
    assert events == ["RUN_STARTED", "RUN_ERROR"]
    snapshot = (await runtime.agui.history(tracer).get("t")).snapshot
    operations = [
        node for node in snapshot.graph.nodes if node.name == "context_compaction"
    ]
    assert len(operations) == 1
    assert operations[0].status == "failed"
    assert not snapshot.messages


async def test_compaction_uses_selected_runtime_profile() -> None:
    model = model_with("Answer", "Short summary")
    runtime = (
        TinkerFin(
            checkpointer=InMemorySaver(), runtime_profile=DeepAgentsV3RuntimeProfile()
        )
        .with_namespace("alpha")
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    result = await runtime.compact(thread_id="t", run_id="compact")
    assert result.status == "compacted"
    stream = runtime.agui.open_compaction(thread_id="t", run_id="repeat")
    try:
        types = [event.type.value async for event in stream]
    finally:
        await stream.aclose()
    assert types.count("RUN_FINISHED") == 1
    assert not any(kind.startswith("TEXT_MESSAGE_") for kind in types)
    assert len(model.inputs) == 2


async def test_cancelled_generation_closes_model_and_can_be_requested_again() -> None:
    import asyncio

    class PausedModel(RecordingModel):
        entered: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)
        released: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)
        closed: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if len(self.inputs) == 1 and not self.released.is_set():
                self.entered.set()
                try:
                    await self.released.wait()
                finally:
                    self.closed.set()
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    model = PausedModel(
        responses=[AIMessage(content="Answer"), AIMessage(content="Short summary")]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .with_observer(tracer)
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    task = asyncio.create_task(runtime.compact(thread_id="t", run_id="cancelled"))
    try:
        await model.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        model.released.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert model.closed.is_set()
    cancelled = (await runtime.agui.history(tracer).get("t")).snapshot
    operation = next(
        node for node in cancelled.graph.nodes if node.name == "context_compaction"
    )
    assert operation.status == "cancelled"
    assert operation.result == {"status": "generating"}
    result = await runtime.compact(thread_id="t", run_id="retry")
    assert result.status == "compacted"
    assert "Earlier investigation results." in str(model.inputs[-1])


@pytest.mark.parametrize("committed_before_error", [False, True])
async def test_checkpoint_failure_never_publishes_success(
    committed_before_error: bool,
) -> None:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import (
        ChannelVersions,
        Checkpoint,
        CheckpointMetadata,
    )

    class FailingSaver(InMemorySaver):
        async def aput(
            self,
            config: RunnableConfig,
            checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: ChannelVersions,
        ) -> RunnableConfig:
            if checkpoint["channel_values"].get("context_compaction") is not None:
                if committed_before_error:
                    await super().aput(config, checkpoint, metadata, new_versions)
                raise OSError("checkpoint acknowledgment failed")
            return await super().aput(config, checkpoint, metadata, new_versions)

    model = model_with("Answer", "Short summary")
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=FailingSaver())
        .with_namespace("alpha")
        .with_observer(tracer)
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    stream = runtime.agui.open_compaction(thread_id="t", run_id="compact")
    events = [event.model_dump(mode="json", by_alias=True) async for event in stream]
    await stream.aclose()
    assert [event["type"] for event in events].count("RUN_ERROR") == 1
    assert not any(event["type"] == "RUN_FINISHED" for event in events)
    assert "Short summary" not in str(events)
    snapshot = (await runtime.agui.history(tracer).get("t")).snapshot
    operation = next(
        node for node in snapshot.graph.nodes if node.name == "context_compaction"
    )
    assert operation.status == "failed"
    assert isinstance(operation.result, dict)
    assert operation.result["status"] == "saving"


async def test_compaction_preserves_tool_pairs_todos_and_namespace() -> None:
    from langchain.agents.middleware import TodoListMiddleware
    from langchain_core.messages import ToolMessage

    saver = InMemorySaver()
    todos = [{"content": "Prepare report", "status": "in_progress"}]
    model = RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "write_todos", "id": "todo", "args": {"todos": todos}}
                ],
            ),
            AIMessage(content="Answer"),
            AIMessage(content="Short summary"),
            AIMessage(content="Continued"),
        ]
    )
    alpha = (
        TinkerFin(checkpointer=saver)
        .with_namespace("alpha")
        .build(model=model, middleware=[eligible_policy(model), TodoListMiddleware()])
    )
    beta_model = model_with("Other answer", "Other continued")
    beta = TinkerFin(checkpointer=saver).with_namespace("beta").build(model=beta_model)
    messages = [
        *history(),
        AIMessage(
            content="",
            id="proposal",
            tool_calls=[
                {
                    "name": "read_file",
                    "id": "read",
                    "args": {"file_path": "/attachments/notes.txt"},
                }
            ],
        ),
        ToolMessage(id="result", tool_call_id="read", content="Report content. " * 100),
    ]
    before = await alpha.ainvoke(
        thread_id="t", run_id="chat", input={"messages": messages}
    )
    await beta.ainvoke(
        thread_id="t", run_id="chat", input={"messages": history("beta-")}
    )
    assert (
        await alpha.compact(thread_id="t", run_id="compact")
    ).compacted_messages == 7
    after = await alpha.ainvoke(
        thread_id="t",
        run_id="next",
        input={"messages": [HumanMessage(content="Continue")]},
    )
    assert message_ids(saved_messages(after)[:8]) == message_ids(saved_messages(before))
    assert after["todos"] == todos
    assert "proposal" in str(after["files"]) or "Report content." in str(after["files"])
    await beta.ainvoke(
        thread_id="t",
        run_id="next",
        input={"messages": [HumanMessage(content="Continue")]},
    )
    assert "Short summary" not in str(beta_model.inputs[-1])
    assert "Earlier investigation results." in str(beta_model.inputs[-1])


async def test_pending_tool_approval_is_not_changed_by_compaction() -> None:
    from langchain_core.tools import tool

    @tool
    async def save_report() -> str:
        """Save a report after approval."""
        raise AssertionError("must not execute before approval")

    model = RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "save_report", "id": "save", "args": {}}],
            )
        ]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .with_observer(tracer)
        .build(model=model, tools=[save_report], interrupt_on={"save_report": True})
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    before = (await runtime.agui.history(tracer).get("t")).snapshot
    with pytest.raises(TinkerFinLifecycleError, match="pending work"):
        await runtime.compact(thread_id="t", run_id="compact")
    after = (await runtime.agui.history(tracer).get("t")).snapshot
    assert after.messages == before.messages
    assert after.interactions == before.interactions
    assert len(model.inputs) == 1


async def test_pending_plan_input_rejects_compaction() -> None:
    from test_plan_mode import _planner

    model = RecordingModel(responses=[_planner()])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .with_plan()
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(
        thread_id="t", run_id="chat", input={"messages": history()}, mode="plan"
    )
    with pytest.raises(TinkerFinLifecycleError, match="pending work|Plan input"):
        await runtime.compact(thread_id="t", run_id="compact")
    assert len(model.inputs) == 1


async def test_repeated_delivery_replays_one_compaction_without_model_execution() -> (
    None
):
    from tinkerfin_messaging import Messaging

    model = model_with("Answer", "Short summary")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    async with Messaging() as messaging:
        channel = messaging.agui_channel(name="compaction")
        deliveries = []
        for _ in range(2):
            body = await channel.open_sse(
                runtime.agui.open_compaction(thread_id="t", run_id="compact"), after=0
            )
            try:
                deliveries.append(b"".join([chunk async for chunk in body]))
            finally:
                await body.aclose()
        assert deliveries[0] == deliveries[1]
    assert len(model.inputs) == 2


async def test_history_exposes_saving_phase_until_checkpoint_finishes() -> None:
    import asyncio

    from ag_ui.core import RawEvent
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import (
        ChannelVersions,
        Checkpoint,
        CheckpointMetadata,
    )

    class PausedSaver(InMemorySaver):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def aput(
            self,
            config: RunnableConfig,
            checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: ChannelVersions,
        ) -> RunnableConfig:
            if checkpoint["channel_values"].get("context_compaction") is not None:
                self.entered.set()
                await self.release.wait()
            return await super().aput(config, checkpoint, metadata, new_versions)

    saver = PausedSaver()
    model = model_with("Answer", "Short summary")
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=saver)
        .with_namespace("alpha")
        .with_observer(tracer)
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    stream = runtime.agui.open_compaction(thread_id="t", run_id="compact")
    progress = asyncio.Event()

    async def consume():
        events = []
        async for event in stream:
            events.append(event)
            if isinstance(event, RawEvent) and event.source == "langgraph.custom":
                progress.set()
        return events

    consumer = asyncio.create_task(consume())
    try:
        await saver.entered.wait()
        await progress.wait()
        snapshot = (await runtime.agui.history(tracer).get("t")).snapshot
        phase = next(
            node for node in snapshot.graph.nodes if node.name == "context_compaction"
        )
        assert phase.status == "running"
        main = next(
            node for node in snapshot.graph.nodes if node.name == "context_compaction"
        )
        assert isinstance(main.result, dict)
        assert main.result["status"] == "saving"
        saver.release.set()
        events = await consumer
        assert events[-1].type == "RUN_FINISHED"
    finally:
        saver.release.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()


@pytest.mark.parametrize(
    "tokens,provider,expected",
    [
        (4999, "matching", "nothing_to_compact"),
        (5000, "matching", "compacted"),
        (6000, "other", "nothing_to_compact"),
        (None, "matching", "nothing_to_compact"),
    ],
)
async def test_manual_gate_uses_native_reported_usage(
    tokens, provider, expected
) -> None:
    class UsageModel(RecordingModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            result = await super()._agenerate(messages, stop, run_manager, **kwargs)
            message = result.generations[0].message
            assert isinstance(message, AIMessage)
            message.usage_metadata = (
                None
                if tokens is None
                else {
                    "input_tokens": tokens,
                    "output_tokens": 0,
                    "total_tokens": tokens,
                }
            )
            if provider == "other":
                result.generations[0].message.response_metadata["model_provider"] = (
                    "unrelated-provider"
                )
            return result

    model = UsageModel(
        responses=[AIMessage(content="Answer"), AIMessage(content="Summary")]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .build(model=model, middleware=[eligible_policy(model)])
    )
    await runtime.ainvoke(thread_id="t", run_id="chat", input={"messages": history()})
    assert (await runtime.compact(thread_id="t", run_id="compact")).status == expected
    assert len(model.inputs) == (2 if expected == "compacted" else 1)


@pytest.mark.parametrize("enabled", [False, True])
async def test_compaction_tool_builder_is_preserved_and_executes_in_active_run(
    enabled: bool,
) -> None:
    from langchain_core.messages import ToolMessage

    class ToolModel(RecordingModel):
        bound_names: list[str] = Field(default_factory=list, exclude=True)

        def bind_tools(self, tools, **kwargs):
            self.bound_names[:] = [
                item["name"] if isinstance(item, dict) else item.name for item in tools
            ]
            return super().bind_tools(tools, **kwargs)

    model = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "compact_conversation", "id": "compact-call", "args": {}}
                ],
            ),
            AIMessage(content="Short summary"),
            AIMessage(content="Done"),
        ]
        if enabled
        else [AIMessage(content="Done")]
    )
    builder = TinkerFin(checkpointer=InMemorySaver())
    runtime = (
        builder.with_compaction_tool(enabled=enabled)
        .with_namespace("alpha")
        .with_plan(enabled=False)
        .with_observer(Tracer())
        .build(model=model, middleware=[eligible_policy(model)])
    )
    state = await runtime.ainvoke(
        thread_id="t", run_id="chat", input={"messages": history()}
    )
    assert ("compact_conversation" in model.bound_names) is enabled
    results = [item for item in saved_messages(state) if isinstance(item, ToolMessage)]
    if enabled:
        assert len(results) == 1
        assert results[0].tool_call_id == "compact-call"
        assert "Conversation compacted" in results[0].text
        assert "Short summary" in str(model.inputs[-1])
        assert len(model.inputs) == 3
    else:
        assert not results


@pytest.mark.parametrize("origin", ["tool", "automatic"])
@pytest.mark.parametrize("save_failure", [False, True])
async def test_native_compaction_commit_is_independent_of_later_run_failure(
    origin, save_failure
) -> None:
    from langchain_core.tools import tool

    class LaterFailure(RecordingModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if len(self.inputs) == (2 if origin == "tool" else 1):
                raise RuntimeError("later model failed")
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    class RejectSummary(InMemorySaver):
        async def aput(self, config, checkpoint, metadata, new_versions):
            if save_failure and "_tinkerfin_compaction_id" in new_versions:
                raise OSError("save refused")
            return await super().aput(config, checkpoint, metadata, new_versions)

    @tool
    async def ping() -> str:
        """Return a fixed tool result."""
        return "pong"

    model = LaterFailure(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "compact_conversation" if origin == "tool" else "ping",
                        "id": "call",
                        "args": {},
                    }
                ],
            ),
            AIMessage(content="Short summary"),
        ]
    )
    summarizer = model if origin == "tool" else model_with("Short summary")
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=RejectSummary())
        .with_namespace("alpha")
        .with_observer(tracer)
        .with_compaction_tool(enabled=origin == "tool")
        .build(
            model=model,
            tools=[ping],
            middleware=[
                SummarizationMiddleware(
                    summarizer,
                    backend=StateBackend(),
                    trigger=("tokens", 10000) if origin == "tool" else ("messages", 3),
                    keep=("messages", 1),
                )
            ],
        )
    )
    with pytest.raises(OSError if save_failure else RuntimeError):
        await runtime.ainvoke(
            thread_id="t", run_id="run", input={"messages": history()}
        )
    snapshot = (await runtime.agui.history(tracer).get("t")).snapshot
    actions = [
        node
        for node in snapshot.graph.nodes
        if node.kind == "custom" and node.context_kind == "compaction"
    ]
    applied = [
        node
        for node in actions
        if isinstance(node.result, dict) and node.result.get("status") == "compacted"
    ]
    assert len(applied) == (0 if save_failure else 1)
    assert all(node.compaction_origin == origin for node in actions)
    for action in actions:
        models = [
            node
            for node in snapshot.graph.nodes
            if node.kind == "model" and node.parent_node_id == action.id
        ]
        assert len(models) == 1
        context = next(
            node for node in snapshot.graph.nodes if node.id == action.parent_node_id
        )
        assert context.kind == "context"
        if origin == "tool":
            assert context.parent_node_id == next(
                node.id
                for node in snapshot.graph.nodes
                if node.kind == "tool" and node.name == "compact_conversation"
            )


async def test_automatic_summary_is_not_applied_when_same_model_step_fails() -> None:
    class FailedAnswer(RecordingModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("answer failed")

    tracer = Tracer()
    summary = model_with("Short summary")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alpha")
        .with_observer(tracer)
        .build(
            model=FailedAnswer(responses=[AIMessage(content="unused")]),
            middleware=[
                SummarizationMiddleware(
                    summary,
                    backend=StateBackend(),
                    trigger=("messages", 3),
                    keep=("messages", 1),
                )
            ],
        )
    )
    with pytest.raises(RuntimeError, match="answer failed"):
        await runtime.ainvoke(
            thread_id="t", run_id="run", input={"messages": history()}
        )
    nodes = (await runtime.agui.history(tracer).get("t")).snapshot.graph.nodes
    action = next(
        node
        for node in nodes
        if node.kind == "custom" and node.context_kind == "compaction"
    )
    assert action.status == "failed"
    assert isinstance(action.result, dict)
    assert action.result["status"] == "generated"
    assert action.result["summary"] == "Short summary"


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
async def test_filtered_compaction_preserves_inspectable_unit(
    storage: str, tmp_path: Path
) -> None:
    """Direct matches and cursors remain filtered while summary context stays readable."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from tinkerfin_contracts import ThreadIdentity
    from tinkerfin_tracing import (
        InMemoryTraceStore,
        SqlAlchemyTraceStore,
        TraceGraphFilter,
        TraceGraphNodeKind,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'compaction.db'}")
    try:
        store = (
            InMemoryTraceStore()
            if storage == "memory"
            else SqlAlchemyTraceStore(engine)
        )
        if isinstance(store, SqlAlchemyTraceStore):
            await store.setup()
        tracer = Tracer(store=store)
        model = model_with("Answer", "Short summary")
        runtime = (
            TinkerFin(checkpointer=InMemorySaver())
            .with_namespace("alpha")
            .with_observer(tracer)
            .build(model=model, middleware=[eligible_policy(model)])
        )
        await runtime.ainvoke(
            thread_id="t", run_id="chat", input={"messages": history()}
        )
        identity = ThreadIdentity(namespace="alpha", thread_id="t")
        initial = await tracer.query(
            identity, where=TraceGraphFilter(search="context_compaction"), limit=1
        )
        updates = initial.follow()
        try:
            await runtime.compact(thread_id="t", run_id="compact")
            async with updates:
                delta = await anext(updates)
        finally:
            await updates.aclose()
        full = await tracer.query(identity)
        unit = {node.id: node for node in full.nodes if node.run_id == "compact"}
        assert {node.kind for node in unit.values()} == {
            TraceGraphNodeKind.CONTEXT,
            TraceGraphNodeKind.CUSTOM,
            TraceGraphNodeKind.MODEL,
        }
        assert {node.id: node for node in delta.node_upserts} == unit
        assert set(delta.ordered_node_ids) == set(unit)
        context_search = await tracer.query(
            identity,
            where=TraceGraphFilter(
                kinds=frozenset({TraceGraphNodeKind.CONTEXT}),
                search="Earlier investigation results.",
            ),
        )
        assert not context_search.matched_node_ids
        pages = []
        for where in (
            None,
            TraceGraphFilter(search="context_compaction"),
            TraceGraphFilter(kinds=frozenset({TraceGraphNodeKind.MODEL})),
        ):
            page = await tracer.query(identity, where=where, limit=1)
            assert len(page.matched_node_ids) == 1
            assert {node.id: node for node in page.nodes} == unit
            assert all(
                node.parent_node_id is None or node.parent_node_id in unit
                for node in page.nodes
            )
            pages.append((where, page))
        await tracer.rebuild_graph(identity)
        for where, page in pages:
            rebuilt = await tracer.query(identity, where=where, limit=1)
            assert rebuilt.snapshot == page.snapshot
    finally:
        await engine.dispose()
