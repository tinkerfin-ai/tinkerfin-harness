"""Recover reviewed delegation through public Runtime calls on isolated Redis."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from ag_ui.core import RunFinishedEvent
from langchain.agents.middleware import ToolRetryMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from tests.support.docker_services import owned_database

from tinkerfin import AgentRuntime, AgUiResumeReceipt, AgUiResumeRequest, TinkerFin

pytestmark = pytest.mark.docker_integration


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> _Model:
        return self


class _Child(_Model):
    attempts: int = 0

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.attempts += 1
        if self.attempts == 1:
            raise ValueError("The provider rejected the first attempt")
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def _approval(terminal: RunFinishedEvent) -> AgUiResumeRequest:
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    return AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": item.id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
                for item in terminal.outcome.interrupts
            ]
        }
    )


async def test_redis_reconstruction_keeps_approval_parent_and_namespace_ownership(
    tmp_path: Path,
) -> None:
    effects: list[str] = []
    receipts: list[AgUiResumeReceipt] = []

    async def saved(receipt: AgUiResumeReceipt) -> None:
        receipts.append(receipt)

    @tool
    async def record(value: str) -> str:
        """Record the approved value."""
        effects.append(value)
        return value

    root = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {
                            "description": "Record both values",
                            "subagent_type": "worker",
                        },
                    }
                ],
            ),
            AIMessage(content="Complete"),
        ]
    )
    child_responses: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[{"id": value, "name": "record", "args": {"value": value}}],
        )
        for value in ("first", "second")
    ]
    child = _Child(responses=[*child_responses, AIMessage(content="Recorded")])

    def build(saver: AsyncRedisSaver, namespace: str = "owner") -> AgentRuntime[None]:
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace(namespace)
            .build(
                model=root,
                subagents=[
                    {
                        "name": "worker",
                        "description": "Record values",
                        "system_prompt": "Record the requested values",
                        "model": child,
                        "tools": [record],
                        "interrupt_on": {"record": True},
                    }
                ],
                middleware=[
                    ToolRetryMiddleware(
                        tools=["task"],
                        max_retries=1,
                        retry_on=(ValueError,),
                        initial_delay=0,
                        jitter=False,
                        on_failure="error",
                    )
                ],
            )
        )

    async with owned_database(tmp_path, "redis") as url:
        async with AsyncRedisSaver.from_conn_string(url) as saver:
            await saver.asetup()
            initial = build(saver).open_agui_run(
                thread_id="thread",
                run_id="proposal",
                messages=[
                    {"id": "input", "role": "user", "content": "Record both values"}
                ],
            )
            first = [event async for event in initial]
            assert initial.error is None
            assert isinstance(first[-1], RunFinishedEvent)
            approval = _approval(first[-1])
            assert child.attempts == 2 and effects == []

        async with AsyncRedisSaver.from_conn_string(url) as saver:
            foreign = build(saver, "another-owner").open_agui_run(
                thread_id="thread", run_id="foreign", resume=approval
            )
            _ = [event async for event in foreign]
            assert foreign.error is not None and effects == []
            resumed = build(saver).open_agui_run(
                thread_id="thread",
                run_id="first-approval",
                resume=approval,
                on_resume_saved=saved,
            )
            second = [event async for event in resumed]
            assert resumed.error is None
            assert receipts[-1].parent_run_id == "proposal"
            assert isinstance(second[-1], RunFinishedEvent)
            approval = _approval(second[-1])
            assert effects == ["first"]

        async with AsyncRedisSaver.from_conn_string(url) as saver:
            resumed = build(saver).open_agui_run(
                thread_id="thread",
                run_id="second-approval",
                resume=approval,
                on_resume_saved=saved,
            )
            final = [event async for event in resumed]
            assert resumed.error is None
            assert receipts[-1].parent_run_id == "first-approval"
            assert isinstance(final[-1], RunFinishedEvent)
            assert final[-1].outcome is not None and final[-1].outcome.type == "success"
            assert effects == ["first", "second"]
