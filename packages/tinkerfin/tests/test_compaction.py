"""Manual compression shares saved context without creating chat messages."""

from collections.abc import Mapping, Sequence
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
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, TypeAdapter
from test_runtime_store import _Model

from tinkerfin import TinkerFin, TinkerFinLifecycleError
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
