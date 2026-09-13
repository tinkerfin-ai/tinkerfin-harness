"""Interrupted child requests survive a sibling failure or consumer cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import TinkerFin
from tinkerfin_tracing import Tracer


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


def _call(name: str) -> AIMessage:
    return AIMessage(
        id=f"proposal-{name}",
        content="",
        tool_calls=[{"name": name, "id": name, "args": {}}],
    )


@pytest.mark.parametrize("cancel", [False, True])
async def test_checkpoint_pending_child_is_not_cancelled_by_its_sibling(
    cancel: bool,
) -> None:
    waiting = asyncio.Event()
    suspended = asyncio.Event()
    attempts = 0
    executed: list[str] = []

    @tool
    async def unavailable() -> str:
        """Retry a destination without resolving the other child's approval."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await waiting.wait()
            if cancel:
                await suspended.wait()
            raise ValueError("Sibling destination failed")
        return "Recovered"

    @tool
    async def reviewed() -> str:
        """Perform the separately reviewed action."""
        executed.append("reviewed")
        return "Reviewed"

    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("parallel")
        .with_observer(tracer)
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        id="parallel",
                        content="",
                        tool_calls=[
                            {
                                "id": f"task-{name}",
                                "name": "task",
                                "args": {
                                    "subagent_type": name,
                                    "description": "Do your task",
                                },
                            }
                            for name in ("failure", "review")
                        ],
                    ),
                    AIMessage(id="answer", content="Done"),
                ]
            ),
            subagents=[
                {
                    "name": "failure",
                    "description": "Destination operation",
                    "system_prompt": "Perform the destination operation.",
                    "model": _Model(
                        responses=[
                            _call("unavailable"),
                            AIMessage(id="recovered", content="Recovered"),
                        ]
                    ),
                    "tools": [unavailable],
                },
                {
                    "name": "review",
                    "description": "Reviewed operation",
                    "system_prompt": "Perform the reviewed operation.",
                    "model": _Model(
                        responses=[
                            _call("reviewed"),
                            AIMessage(id="reviewed-answer", content="Approved"),
                        ]
                    ),
                    "tools": [reviewed],
                    "interrupt_on": {"reviewed": True},
                },
            ],
        )
    )

    async def observe(part):
        if part.get("interrupts"):
            waiting.set()

    first = runtime.open_agui_run(
        thread_id="thread",
        run_id="first",
        messages=[{"id": "user", "role": "user", "content": "Do both"}],
        on_native_part=observe,
    )

    async def consume() -> None:
        async for _event in first:
            pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=5)
        if cancel:
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
        else:
            await consumer
            assert isinstance(first.error, ValueError)
    finally:
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    before = (await runtime.agui.history(tracer).get("thread")).snapshot
    assert before.summary.status.execution == ("cancelled" if cancel else "failed")
    continued = runtime.open_run(thread_id="thread", run_id="continued", input=None)
    _ = [part async for part in continued]
    assert continued.error is None
    after = (await runtime.agui.history(tracer).get("thread")).snapshot
    assert after.summary.status.execution == "waiting"
    assert attempts == 2 and executed == []
    assert len(before.interactions) == len(after.interactions) == 1
    assert before.interactions[0].source_id == after.interactions[0].source_id
    assert before.interactions[0].status == after.interactions[0].status == "pending"


@pytest.mark.parametrize("followup", ["failure", "waiting"])
async def test_repaired_parent_closes_only_its_old_checkpoint_request(
    followup: str,
) -> None:
    from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatResult

    class RootModel(_Model):
        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            if followup == "failure" and self.i == 1:
                raise ValueError("New provider attempt failed")
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    executed: list[str] = []

    @tool
    async def reviewed() -> str:
        """Perform the approved operation."""
        executed.append("reviewed")
        return "Completed"

    root = RootModel(
        responses=[
            AIMessage(
                id=f"parent-{name}",
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "id": f"delegate-{name}",
                        "args": {
                            "subagent_type": name,
                            "description": "Review this action",
                        },
                    }
                ],
            )
            for name in ("old", "new")
        ]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("scope-replacement")
        .with_observer(tracer)
        .build(
            model=root,
            subagents=[
                {
                    "name": name,
                    "description": f"Review {name} action",
                    "system_prompt": "Request approval before acting.",
                    "model": _Model(responses=[_call("reviewed")]),
                    "tools": [reviewed],
                    "interrupt_on": {"reviewed": True},
                }
                for name in ("old", "new")
            ],
        )
    )
    first = runtime.open_agui_run(
        thread_id="thread",
        run_id="first",
        messages=[{"id": "first-user", "role": "user", "content": "Review old action"}],
    )
    _ = [event async for event in first]
    assert first.error is None
    before = (await runtime.agui.history(tracer).get("thread")).snapshot
    assert len(before.interactions) == 1 and before.interactions[0].status == "pending"
    second = runtime.open_agui_run(
        thread_id="thread",
        run_id="second",
        messages=[
            {"id": "next-user", "role": "user", "content": "Review another action"}
        ],
    )
    _ = [event async for event in second]
    after = (await runtime.agui.history(tracer).get("thread")).snapshot
    old = next(
        item for item in after.interactions if item.id == before.interactions[0].id
    )
    assert old.status == "cancelled"
    assert executed == []
    if followup == "failure":
        assert isinstance(second.error, ValueError)
        assert after.summary.status.execution == "failed"
        assert after.summary.pending_interactions == ()
    else:
        assert second.error is None
        assert after.summary.status.execution == "waiting"
        pending = [item for item in after.interactions if item.status == "pending"]
        assert len(pending) == 1 and pending[0].id != old.id
        continued = runtime.open_run(thread_id="thread", run_id="none", input=None)
        _ = [part async for part in continued]
        assert continued.error is None
        final = (await runtime.agui.history(tracer).get("thread")).snapshot
        assert [item.id for item in final.interactions if item.status == "pending"] == [
            pending[0].id
        ]
        assert (
            next(item for item in final.interactions if item.id == old.id).status
            == "cancelled"
        )
        assert executed == []
