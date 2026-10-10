"""Delegated retries resume the exact reviewed attempt through managed runs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from types import FunctionType
from typing import Any, cast

import pytest
from ag_ui.core import RunFinishedEvent
from langchain.agents.middleware import AgentMiddleware, ToolRetryMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from tinkerfin import (
    AgentRuntime,
    AgUiResumeRequest,
    TinkerFin,
)
from tinkerfin._delegation_journal import DELEGATION_RECORD_CHANNEL
from tinkerfin_contracts import RuntimeObserver
from tinkerfin_native_stream import NativeValuesStreamPart, validate_native_stream_part


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


class _Child(_Model):
    attempts: int = 0
    failure: Exception | None = None
    failures: int = 1

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.failure or ValueError("retry this delegated request")
        response = self.responses[self.i]
        self.i = (self.i + 1) % len(self.responses)
        return ChatResult(generations=[ChatGeneration(message=response)])


class _OpaqueFailure(Exception):
    def __init__(self, marker: object) -> None:
        super().__init__("provider failure with nonserializable attributes")
        self.marker = marker


@pytest.mark.parametrize("opaque", [False, True])
async def test_fresh_runtime_resumes_reviewed_retry_without_repeating_failure(
    opaque: bool,
) -> None:
    executed: list[str] = []
    decisions: list[str] = []
    marker = object()

    @tool
    async def work() -> str:
        """Execute approved work."""
        executed.append("approved")
        return "done"

    def should_retry(error: Exception) -> bool:
        decisions.append(type(error).__name__)
        if opaque:
            assert isinstance(error, _OpaqueFailure) and error.marker is marker
            return True
        return isinstance(error, ValueError)

    child = _Child(
        failure=_OpaqueFailure(marker) if opaque else None,
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "work", "id": "work-call", "args": {}}],
            ),
            AIMessage(content="child complete"),
        ],
    )
    main = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "id": "delegate",
                        "args": {"description": "Do work", "subagent_type": "worker"},
                    }
                ],
            ),
            AIMessage(content="complete"),
        ]
    )
    saver = InMemorySaver()
    relocated = False

    def build():
        callback = should_retry
        if relocated:
            callback = FunctionType(
                should_retry.__code__.replace(
                    co_filename="/installed/application/policies.py",
                    co_firstlineno=should_retry.__code__.co_firstlineno + 50,
                ),
                should_retry.__globals__,
                name=should_retry.__name__,
                argdefs=should_retry.__defaults__,
                closure=should_retry.__closure__,
            )
            callback.__qualname__ = should_retry.__qualname__
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("durable")
            .build(
                model=main,
                subagents=[
                    {
                        "name": "worker",
                        "description": "Execute reviewed work",
                        "system_prompt": "Complete the assigned work",
                        "model": child,
                        "tools": [work],
                        "interrupt_on": {"work": True},
                    }
                ],
                middleware=[
                    ToolRetryMiddleware(
                        tools=["task"],
                        max_retries=1,
                        retry_on=callback,
                        initial_delay=0,
                        jitter=False,
                        on_failure="error",
                    )
                ],
            )
        )

    first = build().open_run(
        thread_id="thread",
        run_id="request",
        input={"messages": [{"role": "user", "content": "Do work"}]},
    )
    before = [part async for part in first]
    assert first.error is None
    interrupts = [
        item
        for part in before
        if isinstance(
            parsed := validate_native_stream_part(part), NativeValuesStreamPart
        )
        and not parsed.ns
        for item in parsed.interrupts
    ]
    assert len(interrupts) == 1 and executed == []
    relocated = True
    after = build().open_run(
        thread_id="thread",
        run_id="resume",
        input=Command(resume={interrupts[0].id: {"decisions": [{"type": "approve"}]}}),
    )
    parts = [part async for part in after]
    assert after.error is None
    assert executed == ["approved"]
    assert decisions == ["_OpaqueFailure" if opaque else "ValueError"]
    assert child.attempts == 3
    assert any(
        isinstance(parsed := validate_native_stream_part(part), NativeValuesStreamPart)
        and not parsed.ns
        and cast(list[BaseMessage], parsed.data["messages"])[-1].content == "complete"
        for part in parts
    )


class _ReviewCase:
    def __init__(self, saver: BaseCheckpointSaver[Any] | None = None) -> None:
        self.saver = saver or InMemorySaver()
        self.executed: list[str] = []
        self.decisions: list[Exception] = []

        @tool
        async def work(value: str) -> str:
            """Execute the approved value."""
            self.executed.append(value)
            return value

        def retry(error: Exception) -> bool:
            self.decisions.append(error)
            return True

        self.work = work
        self.child = _Child(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "work", "id": "work", "args": {"value": "approved"}}
                    ],
                ),
                AIMessage(content="child complete"),
            ]
        )
        self.main = _Model(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "id": "delegate",
                            "args": {
                                "description": "Do work",
                                "subagent_type": "worker",
                            },
                        }
                    ],
                ),
                AIMessage(content="complete"),
            ]
        )
        self.policy: ToolRetryMiddleware[Any, Any] = ToolRetryMiddleware(
            tools=["task"],
            max_retries=1,
            retry_on=retry,
            initial_delay=0,
            jitter=False,
            on_failure="error",
        )
        self.inner: list[AgentMiddleware[Any, Any, Any]] = []
        self.policy_enabled = True
        self.observers: tuple[RuntimeObserver, ...] = ()

    def build(self) -> AgentRuntime[None]:
        builder = TinkerFin(checkpointer=self.saver).with_namespace("durable")
        for observer in self.observers:
            builder = builder.with_observer(observer)
        return builder.build(
            model=self.main,
            subagents=[
                {
                    "name": "worker",
                    "description": "Execute reviewed work",
                    "system_prompt": "Complete assigned work",
                    "model": self.child,
                    "tools": [self.work],
                    "interrupt_on": {"work": True},
                }
            ],
            middleware=[
                *([self.policy] if self.policy_enabled else []),
                *self.inner,
            ],
        )

    async def pause(self) -> str:
        parts = [
            part
            async for part in self.build().open_run(
                thread_id="thread",
                run_id="request",
                input={"messages": [{"role": "user", "content": "Do work"}]},
            )
        ]
        return _interrupt(parts)


def _interrupt(parts: Sequence[Mapping[str, object]]) -> str:
    values = [
        item.id
        for part in parts
        if isinstance(
            parsed := validate_native_stream_part(part), NativeValuesStreamPart
        )
        and not parsed.ns
        for item in parsed.interrupts
    ]
    assert len(values) == 1
    return values[0]


class _OutcomeGate(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = True

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        if self.active and any(
            channel == DELEGATION_RECORD_CHANNEL and value["kind"] == "outcome"
            for channel, value in writes
        ):
            self.active = False
            self.entered.set()
            await self.release.wait()
        await super().aput_writes(config, writes, task_id, task_path)


async def test_cancelled_outcome_save_recovers_without_reexecuting_failed_body() -> (
    None
):
    saver = _OutcomeGate()
    case = _ReviewCase(saver)
    running = asyncio.create_task(case.pause())
    try:
        await saver.entered.wait()
        running.cancel()
        saver.release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        saver.release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert case.child.attempts == 1
    recovered = [
        part
        async for part in case.build().open_run(
            thread_id="thread", run_id="retry", input=None
        )
    ]
    interrupt_id = _interrupt(recovered)
    _ = [
        part
        async for part in case.build().open_run(
            thread_id="thread",
            run_id="resume",
            input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
        )
    ]
    assert case.child.attempts == 3 and len(case.decisions) == 1
    assert case.executed == ["approved"]


class _FailedOutcomeSaver(InMemorySaver):
    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        if any(
            channel == DELEGATION_RECORD_CHANNEL and value["kind"] == "outcome"
            for channel, value in writes
        ):
            raise OSError("outcome storage unavailable")
        await super().aput_writes(config, writes, task_id, task_path)


async def test_failed_outcome_commit_never_starts_the_next_attempt() -> None:
    case = _ReviewCase(_FailedOutcomeSaver())
    with pytest.raises(OSError, match="outcome storage unavailable"):
        await case.pause()
    assert case.child.attempts == 1 and len(case.decisions) == 1
    assert case.executed == []


async def test_parallel_retried_delegations_resume_their_own_edited_approvals() -> None:
    case = _ReviewCase()
    children = {
        name: _Child(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "work", "id": "shared-work", "args": {"value": name}}
                    ],
                ),
                AIMessage(content=f"{name} complete"),
            ]
        )
        for name in ("first", "second")
    }
    parent = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "id": f"delegate-{name}",
                        "args": {"description": "Do work", "subagent_type": name},
                    }
                    for name in children
                ],
            ),
            AIMessage(content="complete"),
        ]
    )

    def build() -> AgentRuntime[None]:
        return (
            TinkerFin(checkpointer=case.saver)
            .with_namespace("parallel")
            .build(
                model=parent,
                middleware=[case.policy],
                subagents=[
                    {
                        "name": name,
                        "description": "Do work",
                        "system_prompt": "Do work",
                        "model": model,
                        "tools": [case.work],
                        "interrupt_on": {"work": True},
                    }
                    for name, model in children.items()
                ],
            )
        )

    first = build().open_agui_run(
        thread_id="thread",
        run_id="request",
        messages=[{"id": "user", "role": "user", "content": "Do both tasks"}],
    )
    events = [event async for event in first]
    terminal = events[-1]
    assert first.error is None and isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    assert len(terminal.outcome.interrupts) == 2
    from tinkerfin_agui_adapter import parse_tool_review_interrupt

    entries = []
    for item in terminal.outcome.interrupts:
        review = parse_tool_review_interrupt(item)
        entries.append(
            {
                "interruptId": item.id,
                "status": "resolved",
                "payload": {
                    "type": "edit",
                    "edited_action": {
                        "name": "work",
                        "args": {
                            "value": f"{review.original_args.root['value']}-approved"
                        },
                    },
                },
            }
        )
    resumed = build().open_agui_run(
        thread_id="thread",
        run_id="resume",
        parent_run_id="request",
        resume=AgUiResumeRequest.model_validate({"entries": entries}),
    )
    _ = [event async for event in resumed]
    assert resumed.error is None
    assert sorted(case.executed) == ["first-approved", "second-approved"]
    assert len(case.decisions) == 2
    assert all(child.attempts == 3 for child in children.values())


async def test_default_exhaustion_keeps_provider_diagnostics_private_and_main_can_continue() -> (
    None
):
    case = _ReviewCase()
    case.child.failure = _OpaqueFailure(object())
    case.child.failures = 3
    case.policy.on_failure = "continue"
    stream = case.build().open_agui_run(
        thread_id="thread",
        run_id="request",
        messages=[{"id": "user", "role": "user", "content": "Do work"}],
    )
    events = [event async for event in stream]
    assert stream.error is None and isinstance(events[-1], RunFinishedEvent)
    public_outputs = [
        event.model_dump_json()
        for event in events
        if event.type
        in {
            "STATE_SNAPSHOT",
            "STATE_DELTA",
            "MESSAGES_SNAPSHOT",
            "TOOL_CALL_RESULT",
        }
    ]
    assert all("nonserializable attributes" not in output for output in public_outputs)
    assert any(
        "The delegated task could not be completed." in output
        for output in public_outputs
    )
    private = [
        value
        for checkpoint in [item async for item in case.saver.alist(None)]
        for _task, channel, value in checkpoint.pending_writes or ()
        if channel == DELEGATION_RECORD_CHANNEL
    ]
    assert any("nonserializable attributes" in str(value) for value in private)
    assert case.executed == [] and case.child.attempts == 2
