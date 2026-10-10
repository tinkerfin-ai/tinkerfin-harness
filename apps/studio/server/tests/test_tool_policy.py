"""工具失败后的纠错、有限执行和控制信号传播"""

import asyncio

import pytest
from ag_ui.core import RunFinishedInterruptOutcome
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from tinkerfin import AgUiResumeRequest, TinkerFin
from tinkerfin_sandbox import OpenSandboxStateOwnershipError
from tinkerfin_studio.agent.tool_policy import TOOL_CALL_LIMIT, tool_execution_policy


class Model(FakeMessagesListChatModel):
    requests: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def proposal(index: int, *, name: str = "operation", args=None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"call-{index}", "name": name, "args": args or {}}],
    )


async def collect(runtime, *, run_id="run", thread_id="thread", resume=None):
    stream = runtime.open_agui_run(
        thread_id=thread_id,
        run_id=run_id,
        messages=None
        if resume
        else [{"id": f"user-{run_id}", "role": "user", "content": "执行任务"}],
        resume=resume,
    )
    try:
        events = [event async for event in stream]
        return events, stream.error
    finally:
        await stream.aclose()


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("unexpected"),
        TypeError("unexpected"),
        FileNotFoundError("storage missing"),
        OpenSandboxStateOwnershipError("ownership"),
    ],
)
async def test_unknown_or_integrity_failure_terminates_run(failure):
    @tool
    async def operation() -> str:
        """执行需要保持故障语义的操作"""
        raise failure

    runtime = (
        TinkerFin()
        .with_namespace("policy")
        .build(
            model=Model(responses=[proposal(1)]),
            tools=[operation],
            middleware=tool_execution_policy(),
        )
    )
    events, error = await collect(runtime)
    assert error is not None
    assert sum(event.type == "RUN_ERROR" for event in events) == 1
    assert not any(event.type == "RUN_FINISHED" for event in events)


async def test_parallel_batch_over_budget_executes_no_partial_batch():
    calls = []

    @tool
    async def operation() -> str:
        """记录实际执行次数"""
        calls.append("attempt")
        return "ok"

    batch = AIMessage(
        content="",
        tool_calls=[
            {"id": f"call-{i}", "name": "operation", "args": {}}
            for i in range(TOOL_CALL_LIMIT + 1)
        ],
    )
    runtime = (
        TinkerFin()
        .with_namespace("policy")
        .build(
            model=Model(
                responses=[batch, AIMessage(content="本批工具未执行，已保留已有结果")]
            ),
            tools=[operation],
            middleware=tool_execution_policy(),
        )
    )
    events, error = await collect(runtime)
    assert error is None and calls == []
    assert (
        sum(event.type == "TOOL_CALL_RESULT" for event in events) == TOOL_CALL_LIMIT + 1
    )
    assert sum(event.type == "RUN_FINISHED" for event in events) == 1
    assert any(
        event.type == "TEXT_MESSAGE_CONTENT" and "本批工具未执行" in event.delta
        for event in events
    )


async def test_user_cancellation_propagates_and_releases_tool():
    started, released = asyncio.Event(), asyncio.Event()

    @tool
    async def operation() -> str:
        """等候取消并释放本次操作"""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
        return "unreachable"

    runtime = (
        TinkerFin()
        .with_namespace("policy")
        .build(
            model=Model(responses=[proposal(1)]),
            tools=[operation],
            middleware=tool_execution_policy(),
        )
    )
    task = asyncio.create_task(collect(runtime))
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released.is_set()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_approval_resume_preserves_distinct_user_requests():
    calls = []

    @tool
    async def operation(label: str) -> str:
        """经审批后执行"""
        calls.append(label)
        return "ok"

    model = Model(
        responses=[
            proposal(1, args={"label": "first"}),
            AIMessage(content="完成"),
            proposal(2, args={"label": "second"}),
            AIMessage(content="再次完成"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("review")
        .build(
            model=model,
            tools=[operation],
            middleware=tool_execution_policy(),
            interrupt_on={"operation": True},
        )
    )
    for index in range(2):
        events, error = await collect(runtime, run_id=f"start-{index}")
        assert error is None
        outcome = events[-1].outcome
        assert isinstance(outcome, RunFinishedInterruptOutcome)
        assert len(calls) == index
        resume = AgUiResumeRequest.model_validate(
            {
                "entries": [
                    {
                        "interruptId": pending.id,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                    for pending in outcome.interrupts
                ]
            }
        )
        after, error = await collect(runtime, run_id=f"resume-{index}", resume=resume)
        assert error is None, (index, getattr(error, "cause", None))
        assert len(calls) == index + 1
        assert sum(event.type == "TOOL_CALL_RESULT" for event in after) == 1
        assert sum(event.type == "RUN_FINISHED" for event in after) == 1
