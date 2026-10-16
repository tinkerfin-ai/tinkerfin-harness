"""Run complete commands through real Runtime and Memory Messaging boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from ag_ui.core import RunFinishedEvent, RunStartedEvent
from ag_ui.core.types import ResumeEntry
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, JsonValue

from tinkerfin import AgUiResumeReceipt, AgUiResumeRequest, TinkerFin
from tinkerfin_gateway import (
    CompactRun,
    Gateway,
    ResumeRun,
    RunAcceptance,
    StartRun,
)
from tinkerfin_messaging import Messaging, RunRequestConflict
from tinkerfin_notifications import Notifications


class Model(FakeMessagesListChatModel):
    inputs: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.inputs.append(messages)
        return await super()._agenerate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        return self


def command(*, run_id: str = "run", text: str = "Hello") -> StartRun:
    return StartRun(
        thread_id="thread",
        run_id=run_id,
        messages=({"id": "user", "role": "user", "content": text},),
    )


async def test_user_and_context_command_replays_once_and_binds_provenance() -> None:
    model = Model(responses=[AIMessage(content="answer")])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver()).with_namespace("account").build(model)
    )
    context: dict[str, JsonValue] = {
        "id": "context",
        "role": "user",
        "content": "Authorized document",
        "source": {"kind": "context", "name": "retrieval"},
    }
    request = StartRun(
        thread_id="thread",
        run_id="context-run",
        messages=(
            {"id": "question", "role": "user", "content": "Question"},
            context,
        ),
    )
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        first = await gateway.start(runtime, request)
        async with first.subscribe() as stream:
            before = [item.data async for item in stream]
        retry = await gateway.start(runtime, request)
        async with retry.subscribe() as stream:
            replay = [item.data async for item in stream]
        assert replay == before
        assert len(model.inputs) == 1
        assert [
            message.id for message in model.inputs[0] if message.type == "human"
        ] == ["question", "context"]
        conflicting = request.model_copy(
            update={
                "messages": (
                    request.messages[0],
                    {
                        **context,
                        "source": {"kind": "context", "name": "another-source"},
                    },
                )
            }
        )
        with pytest.raises(RunRequestConflict):
            await gateway.start(runtime, conflicting)


class Registration:
    def __init__(self) -> None:
        self.acceptances: list[RunAcceptance] = []
        self.released = 0
        self.failure: Exception | None = None

    async def confirm(self, acceptance: RunAcceptance) -> None:
        self.acceptances.append(acceptance)
        if self.failure is not None:
            raise self.failure

    async def release(self) -> None:
        self.released += 1


@pytest.mark.parametrize(
    "changed",
    [
        command(text="different"),
        command().model_copy(update={"parameters": {"choice": "different"}}),
        command().model_copy(update={"parent_run_id": "different"}),
        CompactRun(thread_id="thread", run_id="run"),
        ResumeRun(
            thread_id="thread",
            run_id="run",
            resume=AgUiResumeRequest(
                entries=(ResumeEntry(interrupt_id="review", status="cancelled"),)
            ),
        ),
    ],
)
async def test_full_command_binding_rejects_conflicts(
    changed: StartRun | CompactRun | ResumeRun,
) -> None:
    runtime = (
        TinkerFin()
        .with_namespace("account")
        .build(Model(responses=[AIMessage(content="answer")]))
    )
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        run = await gateway.start(runtime, command())
        async with run.subscribe() as replies:
            _ = [item async for item in replies]
        rejected = Registration()
        with pytest.raises(RunRequestConflict):
            await gateway.stream(runtime, changed, registration=rejected)
        assert rejected.released == 1
        assert not rejected.acceptances


async def test_detaching_output_does_not_cancel_and_explicit_cancel_settles() -> None:
    executing, stopped = asyncio.Event(), asyncio.Event()

    @tool
    async def held_tool() -> str:
        """Wait until the operation is explicitly cancelled."""
        executing.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "unreachable"

    runtime = (
        TinkerFin()
        .with_namespace("account")
        .build(
            Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "held_tool", "args": {}, "id": "tool"}],
                    )
                ]
            ),
            tools=[held_tool],
        )
    )
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        run = await gateway.start(runtime, command())
        await executing.wait()
        async with run.subscribe() as replies:
            iterator = aiter(replies)
            assert isinstance((await anext(iterator)).data, RunStartedEvent)
        assert not stopped.is_set()
        assert await run.cancel()
        assert stopped.is_set()
        assert await run.delivery_status() == "cancelled"


async def test_resume_saved_receipt_precedes_tool_and_attachment_does_not_settle_again() -> (
    None
):
    calls: list[str] = []

    @tool
    async def reviewed_tool() -> str:
        """Perform the operation after approval."""
        calls.append("tool")
        return "done"

    class Settlement:
        async def saved(self, receipt: AgUiResumeReceipt) -> None:
            assert receipt.identity.run_id == "resume"
            calls.append("saved")

        async def not_saved(self) -> None:
            calls.append("not_saved")

    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "reviewed_tool", "args": {}, "id": "review"}],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("account")
        .build(
            model,
            tools=[reviewed_tool],
            interrupt_on={"reviewed_tool": True},
        )
    )
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        parent = await gateway.start(runtime, command())
        async with parent.subscribe() as replies:
            events = [item.data async for item in replies]
        terminal = events[-1]
        assert isinstance(terminal, RunFinishedEvent)
        assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
        resume = ResumeRun(
            thread_id="thread",
            run_id="resume",
            parent_run_id="run",
            resume=AgUiResumeRequest(
                entries=(
                    ResumeEntry(
                        interrupt_id=terminal.outcome.interrupts[0].id,
                        status="resolved",
                        payload={"type": "approve"},
                    ),
                )
            ),
        )
        run = await gateway.resume(runtime, resume, settlement=Settlement())
        async with run.subscribe() as replies:
            resumed = [item.data async for item in replies]
        assert isinstance(resumed[-1], RunFinishedEvent)
        assert calls == ["saved", "tool"]
        await gateway.resume(runtime, resume, settlement=Settlement())
        assert calls == ["saved", "tool"]
