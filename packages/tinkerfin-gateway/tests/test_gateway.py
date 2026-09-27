"""Run complete commands through real Runtime and Memory Messaging boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from ag_ui.core import RunErrorEvent, RunFinishedEvent, RunStartedEvent
from ag_ui.core.types import ResumeEntry
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, ValidationError

from tinkerfin import AgUiResumeReceipt, AgUiResumeRequest, TinkerFin
from tinkerfin_gateway import (
    CommittedRunEvent,
    CompactRun,
    Gateway,
    ResumeRun,
    RunAcceptance,
    RunPresentation,
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


async def test_start_runs_without_output_consumption_and_retries_do_not_execute() -> (
    None
):
    finished = asyncio.Event()
    observations: list[CommittedRunEvent] = []

    async def observe(value: CommittedRunEvent) -> None:
        observations.append(value)
        if isinstance(value.event, RunFinishedEvent | RunErrorEvent):
            finished.set()

    runtime = (
        TinkerFin()
        .with_namespace("account")
        .build(Model(responses=[AIMessage(content="answer")]))
    )
    first, retry = Registration(), Registration()
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        run = await gateway.start(
            runtime, command(), registration=first, on_committed=observe
        )
        await finished.wait()
        async with run.subscribe() as replies:
            events = [item.data async for item in replies]
        assert isinstance(events[0], RunStartedEvent)
        assert isinstance(events[-1], RunFinishedEvent)
        assert await run.delivery_status() == "completed"
        assert [item.kind for item in first.acceptances] == ["new"]
        await gateway.start(
            runtime, command(), registration=retry, on_committed=observe
        )
        assert [item.kind for item in retry.acceptances] == ["existing"]
        assert len(observations) == 2
        assert first.released == retry.released == 0


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


async def test_command_and_presentation_are_frozen_before_registration_wait() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldRegistration(Registration):
        async def confirm(self, acceptance: RunAcceptance) -> None:
            await super().confirm(acceptance)
            entered.set()
            await release.wait()

    model = Model(responses=[AIMessage(content="answer")])
    runtime = TinkerFin().with_namespace("account").build(model)
    original = command()
    style = RunPresentation(start_attributes={"report": {"name": "original"}})
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        pending = asyncio.create_task(
            gateway.stream(
                runtime,
                original,
                registration=HeldRegistration(),
                presentation=style,
            )
        )
        await entered.wait()
        original.messages[0]["content"] = "mutated"
        style.start_attributes["report"] = "mutated"
        release.set()
        async with await pending as stream:
            events = [item.data async for item in stream]
        started = events[0]
        assert isinstance(started, RunStartedEvent)
        assert started.model_dump()["report"] == {"name": "original"}
        assert model.inputs[0][-1].content == "Hello"
        await gateway.start(runtime, command())


async def test_prepared_registration_failure_is_never_released() -> None:
    registration = Registration()
    registration.failure = ValueError("host write failed")
    runtime = (
        TinkerFin()
        .with_namespace("account")
        .build(Model(responses=[AIMessage(content="answer")]))
    )
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        with pytest.raises(ValueError, match="host write failed"):
            await gateway.start(runtime, command(), registration=registration)
        assert len(registration.acceptances) == 1
        assert registration.released == 0


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


@pytest.mark.parametrize("attribute", ["type", "runId", "run_id", "input"])
def test_presentation_cannot_replace_protocol_fields(attribute: str) -> None:
    with pytest.raises(ValidationError):
        RunPresentation(start_attributes={attribute: "overridden"})


async def test_compaction_uses_a_distinct_durable_run_without_new_user_input() -> None:
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("account")
        .build(Model(responses=[AIMessage(content="answer")]))
    )
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        run = await gateway.start(runtime, command())
        async with run.subscribe() as replies:
            _ = [item async for item in replies]
        compacted = await gateway.compact(
            runtime, CompactRun(thread_id="thread", run_id="compact")
        )
        async with compacted.subscribe() as replies:
            events = [item.data async for item in replies]
        assert isinstance(events[0], RunStartedEvent)
        assert events[0].run_id == "compact"
        assert isinstance(events[-1], RunFinishedEvent)


@pytest.mark.parametrize(
    "failure", [ValueError("cleanup failed"), BaseException("stop signal")]
)
async def test_repeated_cancellation_waits_for_registration_release_and_preserves_control(
    failure: BaseException,
) -> None:
    releasing, release = asyncio.Event(), asyncio.Event()
    settled = asyncio.Event()

    class Releasing(Registration):
        async def release(self) -> None:
            releasing.set()
            await release.wait()
            settled.set()
            raise failure

    runtime = (
        TinkerFin()
        .with_namespace("account")
        .build(Model(responses=[AIMessage(content="answer")]))
    )
    invalid = command().model_copy(update={"messages": ()})
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        submitting = asyncio.create_task(
            gateway.start(runtime, invalid, registration=Releasing())
        )
        await releasing.wait()
        submitting.cancel()
        submitting.cancel()
        release.set()
        expected = (
            asyncio.CancelledError if isinstance(failure, Exception) else BaseException
        )
        with pytest.raises(expected) as caught:
            await submitting
        assert settled.is_set()
        if not isinstance(failure, Exception):
            assert caught.value is failure
        assert caught.value.__cause__ is not caught.value
