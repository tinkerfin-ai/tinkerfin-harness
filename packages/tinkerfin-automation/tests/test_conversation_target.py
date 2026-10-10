"""Scheduled conversations share durable output, history, and cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ag_ui.core import RunFinishedEvent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import TinkerFin
from tinkerfin.agui import AgUiHistory
from tinkerfin_automation import (
    Automation,
    ExecutionLimits,
    ExecutionRequest,
    ExecutionStatus,
    ExecutionSucceeded,
    TinkerFinTarget,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore
from tinkerfin_gateway import Gateway, RunAcceptance, StartRun
from tinkerfin_messaging import Messaging
from tinkerfin_notifications import Notifications
from tinkerfin_tracing import Tracer


class Model(FakeMessagesListChatModel):
    """Return deterministic responses through the real asynchronous Runtime."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        return self


class Registration:
    """Record the public host admission contract."""

    def __init__(self) -> None:
        self.accepted: list[RunAcceptance] = []
        self.released = 0

    async def confirm(self, acceptance: RunAcceptance) -> None:
        self.accepted.append(acceptance)

    async def release(self) -> None:
        self.released += 1


async def test_execution_publishes_history_and_continues_in_the_same_conversation() -> (
    None
):
    tracer = Tracer()
    model = Model(
        responses=[AIMessage(content="scheduled"), AIMessage(content="followup")]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alice")
        .with_observer(tracer)
        .build(model)
    )
    registration = Registration()
    async with Messaging() as messaging, Notifications() as notices:
        gateway = Gateway(messaging=messaging, notifications=notices)
        app = Automation(namespace="scheduler")
        app.target(
            "report",
            TinkerFinTarget(runtime, gateway=gateway, registration=registration),
        )
        async with app.worker() as worker:
            owner = app.for_owner("alice")
            run = await owner.run(
                "report",
                input={"messages": [{"role": "user", "content": "report"}]},
                request_id="report",
            )
            await worker.wait_until_idle()
            await run.refresh()
            assert run.status == ExecutionStatus.SUCCEEDED, run.snapshot.failure_message
            assert (
                await owner.run(
                    "report",
                    input={"messages": [{"role": "user", "content": "report"}]},
                    request_id="report",
                )
            ).id == run.id
            assert len(registration.accepted) == 1
            assert registration.accepted[0].identity == run.identity
            assert registration.released == 0
            async with gateway.run(run.identity).subscribe() as stream:
                events = [item.data async for item in stream]
            terminals = [
                event for event in events if isinstance(event, RunFinishedEvent)
            ]
            assert len(terminals) == 1
            assert terminals[0].run_id == run.identity.run_id
            followup = await gateway.start(
                runtime,
                StartRun(
                    thread_id=run.identity.thread_id,
                    run_id="followup",
                    parent_run_id=run.identity.run_id,
                    messages=({"id": "next", "role": "user", "content": "continue"},),
                ),
            )
            async with followup.subscribe() as stream:
                _ = [item async for item in stream]
            history = await AgUiHistory(tracer, namespace="alice").get(
                run.identity.thread_id
            )
            assert [message.content for message in history.snapshot.messages] == [
                "report",
                "scheduled",
                "continue",
                "followup",
            ]
            original = await AgUiHistory(tracer, namespace="alice").get_run(
                run.identity.thread_id, run_id=run.identity.run_id
            )
            assert [message.content for message in original.messages] == [
                "report",
                "scheduled",
            ]
            assert all(message.agui is not None for message in original.messages)
            continued = await AgUiHistory(tracer, namespace="alice").get_run(
                run.identity.thread_id, run_id="followup"
            )
            assert [message.content for message in continued.messages] == [
                "continue",
                "followup",
            ]
            assert (await owner.get_run(run.id)).status == ExecutionStatus.SUCCEEDED


async def test_execution_interrupt_never_runs_the_reviewed_tool() -> None:
    calls: list[str] = []

    @tool
    async def reviewed() -> str:
        """Perform an explicitly approved action."""
        calls.append("executed")
        return "done"

    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alice")
        .build(
            Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[{"id": "review", "name": "reviewed", "args": {}}],
                    )
                ]
            ),
            tools=[reviewed],
            interrupt_on={"reviewed": True},
        )
    )
    async with Messaging() as messaging, Notifications() as notices:
        gateway = Gateway(messaging=messaging, notifications=notices)
        app = Automation(namespace="scheduler")
        app.target("report", TinkerFinTarget(runtime, gateway=gateway))
        async with app.worker() as worker:
            run = await app.for_owner("alice").run(
                "report",
                input={"messages": [{"id": "q", "role": "user", "content": "act"}]},
            )
            await worker.wait_until_idle()
            await run.refresh()
            assert run.status == ExecutionStatus.INTERRUPTED
            assert run.snapshot.interrupt_ids
            assert calls == []


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_stopping_execution_settles_its_producer_without_closing_gateway(
    store_with_clock: tuple[AutomationStore, ManualClock],
    stop: str,
) -> None:
    store, clock = store_with_clock
    entered, stopped = asyncio.Event(), asyncio.Event()

    @tool
    async def held() -> str:
        """Wait for the owner to cancel the operation."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "unreachable"

    runtime = (
        TinkerFin()
        .with_namespace("alice")
        .build(
            Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[{"id": "held", "name": "held", "args": {}}],
                    )
                ]
            ),
            tools=[held],
        )
    )
    async with Messaging() as messaging, Notifications() as notices:
        gateway = Gateway(messaging=messaging, notifications=notices)
        app = Automation(namespace="scheduler", store=store, clock=clock)
        app.target("report", TinkerFinTarget(runtime, gateway=gateway))
        async with app.worker() as worker:
            run = await app.for_owner("alice").run(
                "report",
                input={"messages": [{"id": "q", "role": "user", "content": "act"}]},
                limits=ExecutionLimits(execution_timeout=timedelta(seconds=5)),
            )
            await entered.wait()
            if stop == "cancel":
                await run.cancel()
            else:
                await clock.advance(timedelta(seconds=5))
            await worker.wait_until_idle()
            assert stopped.is_set()
            assert await gateway.run(run.identity).delivery_status() == "cancelled"
            assert (await run.refresh()).status == ExecutionStatus.NEEDS_ATTENTION


async def test_invalid_conversation_input_releases_host_reservation() -> None:
    runtime = (
        TinkerFin()
        .with_namespace("alice")
        .build(Model(responses=[AIMessage(content="unused")]))
    )
    registration = Registration()
    async with Messaging() as messaging, Notifications() as notices:
        gateway = Gateway(messaging=messaging, notifications=notices)
        app = Automation(namespace="scheduler")
        app.target(
            "report",
            TinkerFinTarget(runtime, gateway=gateway, registration=registration),
        )
        async with app.worker() as worker:
            run = await app.for_owner("alice").run("report", input={"unexpected": True})
            await worker.wait_until_idle()
            assert (await run.refresh()).status == ExecutionStatus.FAILED
            assert registration.accepted == []
            assert registration.released == 1


def test_conversation_options_require_delivery() -> None:
    runtime = (
        TinkerFin()
        .with_namespace("alice")
        .build(Model(responses=[AIMessage(content="unused")]))
    )
    with pytest.raises(ValueError, match="require a Gateway"):
        TinkerFinTarget(runtime, registration=Registration())


@pytest.mark.parametrize("delivery", [False, True])
async def test_target_preserves_runtime_default_plan_review(delivery: bool) -> None:
    plan = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_plan",
                "id": "review-default",
                "args": {
                    "content": {
                        "goal": "Inspect the report",
                        "assumptions": [],
                        "steps": [
                            {
                                "id": "one",
                                "title": "Inspect",
                                "description": "Inspect report",
                                "verification": ["Reviewed"],
                            }
                        ],
                        "acceptance_criteria": ["Reviewed"],
                    }
                },
            }
        ],
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("alice")
        .with_plan(default_mode="plan", planner_model=Model(responses=[plan]))
        .build(Model(responses=[AIMessage(content="must await approval")]))
    )
    assert runtime.default_mode == "plan"
    async with Messaging() as messaging, Notifications() as notices:
        app = Automation(namespace="scheduler")
        gateway = Gateway(messaging=messaging, notifications=notices)
        app.target(
            "report", TinkerFinTarget(runtime, gateway=gateway if delivery else None)
        )
        async with app.worker() as worker:
            run = await app.for_owner("alice").run(
                "report",
                input={"messages": [{"role": "user", "content": "Inspect report"}]},
            )
            await worker.wait_until_idle()
            await run.refresh()
            assert run.status == ExecutionStatus.INTERRUPTED
            assert run.snapshot.interrupt_ids
            await run.cancel()


@pytest.mark.parametrize("fail_close", [False, True])
async def test_terminal_delivery_waits_for_source_close_and_preserves_failure(
    monkeypatch, fail_close: bool
) -> None:
    from tinkerfin_messaging import RunProducerFailed

    entered, release = asyncio.Event(), asyncio.Event()
    runtime = (
        TinkerFin()
        .with_namespace("alice")
        .build(Model(responses=[AIMessage(content="complete")]))
    )
    open_run = runtime.open_agui_run

    def open_gated(*args, **kwargs):
        source = open_run(*args, **kwargs)
        close = source.aclose

        async def close_gated():
            await close()
            entered.set()
            await release.wait()
            if fail_close:
                raise RuntimeError("source cleanup failed")

        source.aclose = close_gated
        return source

    monkeypatch.setattr(runtime, "open_agui_run", open_gated)
    async with Messaging() as messaging, Notifications() as notices:
        gateway = Gateway(messaging=messaging, notifications=notices)
        target = TinkerFinTarget(runtime, gateway=gateway)
        app = Automation(namespace="scheduler")
        app.target("report", target)
        async with app:
            run = await app.for_owner("alice").run(
                "report", input={"messages": [{"role": "user", "content": "report"}]}
            )
            delivery = asyncio.create_task(
                target.run(
                    ExecutionRequest(run.snapshot, datetime(2030, 1, 1, tzinfo=UTC))
                )
            )
            try:
                await entered.wait()
                async with gateway.run(run.identity).subscribe() as stream:
                    async for item in stream:
                        if isinstance(item.data, RunFinishedEvent):
                            break
                    else:
                        pytest.fail("main terminal was not committed")
                assert not delivery.done()
                release.set()
                if fail_close:
                    with pytest.raises(RunProducerFailed):
                        await delivery
                else:
                    assert isinstance(await delivery, ExecutionSucceeded)
            finally:
                release.set()
                if not delivery.done():
                    delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)
