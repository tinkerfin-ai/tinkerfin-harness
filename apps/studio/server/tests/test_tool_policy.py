"""工具失败后的纠错、有限执行和控制信号传播"""

import asyncio

import pytest
from ag_ui.core import RunFinishedInterruptOutcome
from httpx import ConnectError
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import ToolException, tool
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import AgUiResumeRequest, TinkerFin
from tinkerfin_sandbox import OpenSandboxStateOwnershipError
from tinkerfin_studio.agent.tool_policy import TOOL_CALL_LIMIT, tool_execution_policy
from tinkerfin_tracing import Tracer


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


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
        ValueError("secret"),
        ToolException("secret"),
        ConnectError("secret"),
        TimeoutError("secret"),
        PermissionError("secret"),
    ],
)
async def test_expected_failure_reaches_model_once_and_preserves_failed_trace(failure):
    calls = []

    @tool
    async def operation() -> str:
        """执行一次可纠正的操作"""
        calls.append("attempt")
        if len(calls) == 1:
            raise failure
        return "已完成"

    tracer = Tracer()
    runtime = (
        TinkerFin()
        .with_namespace("policy")
        .with_observer(tracer)
        .build(
            model=Model(
                responses=[proposal(1), proposal(2), AIMessage(content="操作已完成")]
            ),
            tools=[operation],
            middleware=tool_execution_policy(),
        )
    )
    events, error = await collect(runtime)
    assert error is None
    assert calls == ["attempt", "attempt"]
    results = [
        event.model_dump(by_alias=True)
        for event in events
        if event.type == "TOOL_CALL_RESULT"
    ]
    assert len(results) == 2
    assert "secret" not in str(results)
    assert [result["toolCallId"] for result in results] == [
        event.tool_call_id for event in events if event.type == "TOOL_CALL_START"
    ]
    assert results[1]["content"] == "已完成"
    assert sum(event.type == "RUN_FINISHED" for event in events) == 1
    assert not any(event.type == "RUN_ERROR" for event in events)
    history = await tracer.get(runtime.thread_identity("thread"))
    assert "操作已完成" in str(history.messages)
    graph = await tracer.query(runtime.thread_identity("thread"))
    assert sorted(node.status for node in graph.nodes if node.kind == "tool") == [
        "failed",
        "succeeded",
    ]


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


@pytest.mark.parametrize("failed", [False, True])
async def test_repeated_tools_end_at_budget_with_paired_results(failed):
    calls = []

    @tool
    async def operation() -> str:
        """执行受总量约束的工具"""
        calls.append("attempt")
        if failed:
            raise ValueError("invalid")
        return "ok"

    tracer = Tracer()
    runtime = (
        TinkerFin()
        .with_namespace("bounded")
        .with_observer(tracer)
        .build(
            model=Model(responses=[proposal(i) for i in range(TOOL_CALL_LIMIT + 1)]),
            tools=[operation],
            middleware=tool_execution_policy(),
        )
    )
    events, error = await collect(runtime)
    assert error is None and len(calls) == TOOL_CALL_LIMIT
    results = [
        event.model_dump(by_alias=True)
        for event in events
        if event.type == "TOOL_CALL_RESULT"
    ]
    assert {result["toolCallId"] for result in results} == {
        event.tool_call_id for event in events if event.type == "TOOL_CALL_START"
    }
    assert len(results) == TOOL_CALL_LIMIT + 1
    assert sum(event.type == "RUN_FINISHED" for event in events) == 1
    history = await tracer.get(runtime.thread_identity("thread"))
    assert "limit" in str(history.messages).lower()


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
            model=Model(responses=[batch]),
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


async def test_new_user_turn_resets_exhausted_budget():
    calls = []

    @tool
    async def operation() -> str:
        """记录各用户轮次的真实调用"""
        calls.append("executed")
        return "ok"

    model = Model(
        responses=[
            *(proposal(i) for i in range(TOOL_CALL_LIMIT + 1)),
            proposal(TOOL_CALL_LIMIT + 1),
            AIMessage(content="新任务完成"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("budget-reset")
        .build(
            model=model,
            tools=[operation],
            middleware=tool_execution_policy(),
        )
    )
    first, error = await collect(runtime, run_id="first")
    assert error is None and len(calls) == TOOL_CALL_LIMIT
    assert first[-1].type == "RUN_FINISHED"
    second, error = await collect(runtime, run_id="second")
    assert error is None and len(calls) == TOOL_CALL_LIMIT + 1
    assert sum(event.type == "TOOL_CALL_RESULT" for event in second) == 1
    assert second[-1].type == "RUN_FINISHED"


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
