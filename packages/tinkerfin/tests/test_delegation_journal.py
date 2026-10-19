"""Reject corrupted delegation ownership through the public Runtime and saver APIs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import AgentRuntime, TinkerFin
from tinkerfin.errors import DelegationReplayError


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
            raise ValueError("Retry the delegated work")
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


class _CorruptReceiptSaver(InMemorySaver):
    corrupted: bool = False

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        stored: list[tuple[str, Any]] = []
        for channel, value in writes:
            # Corrupt the persisted ownership contract at the native saver's
            # public write boundary; no private journal or query method is used.
            if (
                isinstance(value, dict)
                and value.get("kind") == "outcome"
                and "tool_call_id" in value
            ):
                value = {**value, "tool_call_id": "another-invocation"}
                self.corrupted = True
            stored.append((channel, value))
        await super().aput_writes(config, stored, task_id, task_path)


async def test_foreign_receipt_cannot_start_another_attempt_or_execute_tools() -> None:
    saver = _CorruptReceiptSaver()
    effects: list[str] = []

    @tool
    async def work() -> str:
        """Record an executed effect."""
        effects.append("executed")
        return "done"

    child = _Child(
        responses=[
            AIMessage(
                content="", tool_calls=[{"id": "work", "name": "work", "args": {}}]
            ),
            AIMessage(content="Done"),
        ]
    )
    root = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {"description": "Do work", "subagent_type": "worker"},
                    }
                ],
            ),
            AIMessage(content="Complete"),
        ]
    )

    def build() -> AgentRuntime[None]:
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("receipts")
            .build(
                model=root,
                subagents=[
                    {
                        "name": "worker",
                        "description": "Do work",
                        "system_prompt": "Do work",
                        "model": child,
                        "tools": [work],
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

    initial: InputAgentState = {"messages": [{"role": "user", "content": "Do work"}]}
    for run_id, state in (
        ("initial", initial),
        ("reconstructed", None),
    ):
        with pytest.raises(DelegationReplayError):
            _ = [
                part
                async for part in build().open_run(
                    thread_id="thread", run_id=run_id, input=state
                )
            ]
        assert saver.corrupted
        assert child.attempts == 1
        assert effects == []
