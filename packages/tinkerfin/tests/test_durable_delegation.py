"""Delegated retries resume the exact reviewed attempt through managed runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from functools import partial
from types import FunctionType
from typing import Any, NoReturn, cast

import pytest
from ag_ui.core import RunErrorEvent, RunFinishedEvent
from langchain.agents.middleware import AgentMiddleware, ToolRetryMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from tinkerfin import (
    AgentRuntime,
    AgUiResumeRequest,
    DelegationFailedError,
    DelegationReplayError,
    TinkerFin,
    TinkerFinLifecycleError,
)
from tinkerfin._delegation_journal import DELEGATION_RECORD_CHANNEL
from tinkerfin_contracts import RuntimeObserver, ToolExecutionObservation
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


@pytest.mark.parametrize("decision", ["approve", "reject", "edit", "cancelled"])
@pytest.mark.parametrize("extra_modes", [False, True])
async def test_public_agui_decisions_reach_the_reviewed_attempt(
    decision: str,
    extra_modes: bool,
) -> None:
    case = _ReviewCase()
    first = case.build().open_agui_run(
        thread_id="thread",
        run_id="request",
        messages=[{"id": "user", "role": "user", "content": "Do work"}],
        stream_mode=["messages", "tasks", "values", "checkpoints", "debug"]
        if extra_modes
        else None,
    )
    events = [event async for event in first]
    assert first.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    entry: dict[str, object] = {
        "interruptId": terminal.outcome.interrupts[0].id,
        "status": "cancelled" if decision == "cancelled" else "resolved",
    }
    if decision != "cancelled":
        entry["payload"] = (
            {
                "type": "edit",
                "edited_action": {"name": "work", "args": {"value": "edited"}},
            }
            if decision == "edit"
            else {"type": decision}
        )
    resumed = case.build().open_agui_run(
        thread_id="thread",
        run_id="resume",
        parent_run_id="request",
        resume=AgUiResumeRequest.model_validate({"entries": [entry]}),
        stream_mode=["messages", "tasks", "values", "checkpoints", "debug"]
        if extra_modes
        else None,
    )
    after = [event async for event in resumed]
    assert resumed.error is None
    if decision == "cancelled":
        assert (
            isinstance(after[-1], RunErrorEvent)
            and after[-1].code == "resume_cancelled"
        )
    else:
        assert isinstance(after[-1], RunFinishedEvent)
    assert case.executed == (
        ["approved"]
        if decision == "approve"
        else ["edited"]
        if decision == "edit"
        else []
    )
    assert len(case.decisions) == 1
    public = [event.model_dump_json() for event in [*events, *after]]
    assert all(
        DELEGATION_RECORD_CHANNEL not in value and "record_digest" not in value
        for value in public
    )


class _Rewrite(AgentMiddleware):
    description = "Do work"

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if request.tool_call["name"] == "task":
            request = request.override(
                tool_call={
                    **request.tool_call,
                    "args": {
                        **request.tool_call["args"],
                        "description": self.description,
                    },
                }
            )
        return await handler(request)


@pytest.mark.parametrize(
    "change", ["policy", "zero_retry", "no_policy", "filter", "effective_arguments"]
)
async def test_changed_policy_or_effective_request_is_rejected_before_execution(
    change: str,
) -> None:
    case = _ReviewCase()
    rewriting = _Rewrite()
    case.inner = [rewriting]
    interrupt_id = await case.pause()
    if change == "policy":
        case.policy.max_retries = 2
    elif change == "zero_retry":
        case.policy.max_retries = 0
        case.policy.on_failure = "continue"
    elif change == "no_policy":
        case.policy_enabled = False
    elif change == "filter":
        case.policy = ToolRetryMiddleware(
            tools=["work"], max_retries=1, on_failure="continue"
        )
    else:
        rewriting.description = "Do different work"
    resumed = case.build().open_run(
        thread_id="thread",
        run_id="resume",
        input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
    )
    with pytest.raises(DelegationReplayError, match="changed"):
        _ = [part async for part in resumed]
    assert case.executed == [] and case.child.attempts == 2


@pytest.mark.parametrize("binding", ["function", "argument", "keyword"])
async def test_changed_partial_policy_is_rejected_before_resuming_work(
    binding: str,
) -> None:
    def failed(label: str, error: Exception, *, suffix: str) -> str:
        del error
        return label + suffix

    def other_failed(label: str, error: Exception, *, suffix: str) -> str:
        del error
        return label + suffix

    case = _ReviewCase()
    case.policy.on_failure = partial(failed, "original", suffix="-policy")
    interrupt_id = await case.pause()
    case.policy.on_failure = partial(
        other_failed if binding == "function" else failed,
        "changed" if binding == "argument" else "original",
        suffix="-changed" if binding == "keyword" else "-policy",
    )
    case.child.failures = 3
    resumed = case.build().open_run(
        thread_id="thread",
        run_id="resume",
        input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
    )
    with pytest.raises(DelegationReplayError, match="changed"):
        _ = [part async for part in resumed]
    assert case.executed == [] and case.child.attempts == 2


async def test_reconstructed_partial_policy_resumes_the_reviewed_attempt() -> None:
    def retry(error: Exception, *, allowed: tuple[type[Exception], ...]) -> bool:
        return isinstance(error, allowed)

    case = _ReviewCase()
    case.policy.retry_on = partial(retry, allowed=(ValueError,))
    interrupt_id = await case.pause()
    case.policy.retry_on = partial(retry, allowed=(ValueError,))
    resumed = case.build().open_run(
        thread_id="thread",
        run_id="resume",
        input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
    )
    _ = [part async for part in resumed]
    assert resumed.error is None and case.executed == ["approved"]
    assert case.child.attempts == 3


async def test_changed_immutable_closure_policy_is_rejected_before_resuming() -> None:
    def formatter(message: str) -> Callable[[Exception], str]:
        def failure(error: Exception) -> str:
            del error
            return message

        return failure

    case = _ReviewCase()
    case.policy.on_failure = formatter("original-policy")
    interrupt_id = await case.pause()
    case.policy.on_failure = formatter("changed-policy")
    case.child.failures = 3
    resumed = case.build().open_run(
        thread_id="thread",
        run_id="resume",
        input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
    )
    with pytest.raises(DelegationReplayError, match="changed"):
        _ = [part async for part in resumed]
    assert case.executed == [] and case.child.attempts == 2


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("reconstructed", [False, True])
async def test_callback_counter_state_does_not_change_the_pending_policy(
    nested: bool, reconstructed: bool
) -> None:
    def callback() -> tuple[Callable[[Exception], bool], Callable[[], int]]:
        calls = 0

        def direct_retry(error: Exception) -> bool:
            nonlocal calls
            del error
            calls += 1
            return True

        def nested_retry(error: Exception) -> bool:
            del error

            def record() -> None:
                nonlocal calls
                calls += 1

            record()
            return True

        return (nested_retry if nested else direct_retry), lambda: calls

    case = _ReviewCase()
    case.policy.retry_on, count = callback()
    interrupt_id = await case.pause()
    assert count() == 1
    if reconstructed:
        case.policy.retry_on, count = callback()
    resumed = case.build().open_run(
        thread_id="thread",
        run_id="resume",
        input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
    )
    _ = [part async for part in resumed]
    assert resumed.error is None and case.executed == ["approved"]
    assert count() == (0 if reconstructed else 1) and case.child.attempts == 3


@pytest.mark.parametrize("binding", ["method", "instance", "partial", "default"])
def test_opaque_callback_bindings_are_rejected_before_persistent_runs(
    binding: str,
) -> None:
    class Formatter:
        def __init__(self) -> None:
            self.label = "failed"

        def format(self, error: Exception) -> str:
            del error
            return self.label

        def __call__(self, error: Exception) -> str:
            return self.format(error)

    state = Formatter()

    def failed(error: Exception, formatter: Formatter = state) -> str:
        return formatter(error)

    callback = (
        state.format
        if binding == "method"
        else state
        if binding == "instance"
        else partial(failed, formatter=state)
        if binding == "partial"
        else failed
    )
    for saver, maximum in ((None, 1), (InMemorySaver(), 0)):
        TinkerFin(checkpointer=saver).with_namespace("test").build(
            model=_Model(responses=[AIMessage(content="done")]),
            middleware=[ToolRetryMiddleware(max_retries=maximum, on_failure=callback)],
        )
    case = _ReviewCase()
    case.policy.on_failure = callback
    with pytest.raises(TypeError, match="persistent delegation retry callback"):
        case.build()
    assert case.child.attempts == 0 and case.executed == []


async def test_finished_backoff_is_not_repeated_after_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tinkerfin._durable_delegation as implementation

    waits: list[float] = []

    async def controlled_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(implementation, "time", lambda: 100.0)
    monkeypatch.setattr(implementation, "sleep", controlled_sleep)
    case = _ReviewCase()
    case.policy.initial_delay = 3.0
    interrupt_id = await case.pause()
    _ = [
        part
        async for part in case.build().open_run(
            thread_id="thread",
            run_id="resume",
            input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
        )
    ]
    assert waits == [3.0] and case.executed == ["approved"]


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


async def test_terminal_failure_keeps_live_cause_and_replays_a_safe_recorded_error() -> (
    None
):
    case = _ReviewCase()
    original = _OpaqueFailure(object())
    case.child.failure = original
    case.child.failures = 3
    with pytest.raises(_OpaqueFailure) as failed:
        await case.pause()
    assert failed.value is original
    with pytest.raises(DelegationFailedError) as replayed:
        _ = [
            part
            async for part in case.build().open_run(
                thread_id="thread", run_id="retry", input=None
            )
        ]
    assert case.child.attempts == 2 and len(case.decisions) == 2
    assert str(replayed.value) == "Delegated task failed"
    assert "nonserializable" in str(
        replayed.value.diagnostic_context["failure_message"]
    )


@pytest.mark.parametrize("callback", ["retry_on", "on_failure"])
async def test_failed_policy_callback_is_terminal_and_does_not_reexecute_work(
    callback: str,
) -> None:
    case = _ReviewCase()
    original = _OpaqueFailure(object())
    callback_failure = RuntimeError("retry callback failed")
    seen: list[Exception] = []
    case.child.failure = original
    case.child.failures = 10

    def failed(error: Exception) -> NoReturn:
        seen.append(error)
        raise callback_failure

    if callback == "retry_on":
        case.policy.retry_on = failed
    else:
        case.policy.on_failure = failed
    with pytest.raises(RuntimeError) as live:
        await case.pause()
    assert live.value is callback_failure
    attempts = 1 if callback == "retry_on" else 2
    assert case.child.attempts == attempts and seen == [original]
    with pytest.raises(DelegationFailedError) as replayed:
        _ = [
            part
            async for part in case.build().open_run(
                thread_id="thread", run_id="retry", input=None
            )
        ]
    assert case.child.attempts == attempts and seen == [original]
    assert replayed.value.diagnostic_context["policy_failure_type"] == (
        "builtins.RuntimeError"
    )
    assert replayed.value.diagnostic_context["policy_failure_message"] == (
        "retry callback failed"
    )
    assert str(replayed.value.diagnostic_context["failure_type"]).endswith(
        "._OpaqueFailure"
    )
    assert str(replayed.value) == "Delegated task failed"


def test_custom_retry_subclasses_are_rejected_only_for_persistent_delegation() -> None:
    class CustomRetry(ToolRetryMiddleware):
        pass

    for saver, maximum in ((None, 1), (InMemorySaver(), 0)):
        TinkerFin(checkpointer=saver).with_namespace("test").build(
            model=_Model(responses=[AIMessage(content="done")]),
            middleware=[CustomRetry(max_retries=maximum)],
        )
    with pytest.raises(ValueError, match="standard ToolRetryMiddleware"):
        TinkerFin(checkpointer=InMemorySaver()).with_namespace("test").build(
            model=_Model(responses=[AIMessage(content="done")]),
            middleware=[CustomRetry(max_retries=1)],
        )


async def test_multiple_reviews_and_repeated_resume_keep_the_original_attempt() -> None:
    case = _ReviewCase()
    case.child.responses = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "work", "id": "first-work", "args": {"value": "first"}}
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "work", "id": "second-work", "args": {"value": "second"}}
            ],
        ),
        AIMessage(content="child complete"),
    ]
    first_id = await case.pause()

    def request(identifier: str) -> AgUiResumeRequest:
        return AgUiResumeRequest.model_validate(
            {
                "entries": [
                    {
                        "interruptId": identifier,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                ]
            }
        )

    async def resume(run_id: str, parent: str, identifier: str) -> RunFinishedEvent:
        stream = case.build().open_agui_run(
            thread_id="thread",
            run_id=run_id,
            parent_run_id=parent,
            resume=request(identifier),
        )
        events = [event async for event in stream]
        assert stream.error is None
        terminal = events[-1]
        assert isinstance(terminal, RunFinishedEvent)
        return terminal

    second = await resume("review-first", "request", first_id)
    assert second.outcome is not None and second.outcome.type == "interrupt"
    second_id = second.outcome.interrupts[0].id
    repeated = await resume("review-first", "request", first_id)
    assert repeated.outcome is not None and repeated.outcome.type == "interrupt"
    assert repeated.outcome.interrupts[0].id == second_id
    assert case.executed == ["first"]
    _ = await resume("review-second", "review-first", second_id)
    _ = await resume("review-second", "review-first", second_id)
    assert case.executed == ["first", "second"]
    assert len(case.decisions) == 1 and case.child.attempts == 4


async def test_retry_exhaustion_runs_custom_failure_formatter_once() -> None:
    case = _ReviewCase()
    case.child.failures = 3
    failures: list[Exception] = []

    def failed(error: Exception) -> str:
        failures.append(error)
        return "The worker could not complete the request"

    case.policy.on_failure = failed
    parts = [
        part
        async for part in case.build().open_run(
            thread_id="thread",
            run_id="request",
            input={"messages": [{"role": "user", "content": "Do work"}]},
        )
    ]
    roots = [
        parsed
        for part in parts
        if isinstance(
            parsed := validate_native_stream_part(part), NativeValuesStreamPart
        )
        and not parsed.ns
    ]
    messages = cast(list[BaseMessage], roots[-1].data["messages"])
    response = next(message for message in messages if isinstance(message, ToolMessage))
    assert (
        response.status == "error"
        and response.content == "The worker could not complete the request"
    )
    assert case.child.attempts == 2 and len(case.decisions) == 2 and len(failures) == 1
    assert case.executed == []


async def test_interrupted_backoff_waits_only_for_the_saved_remaining_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tinkerfin._durable_delegation as implementation

    clock = [100.0]
    requested: asyncio.Queue[float] = asyncio.Queue(maxsize=1)
    release = asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        await requested.put(delay)
        await release.wait()

    monkeypatch.setattr(implementation, "time", lambda: clock[0])
    monkeypatch.setattr(implementation, "sleep", controlled_sleep)
    case = _ReviewCase()
    case.policy.initial_delay = 3.0
    first = asyncio.create_task(case.pause())
    try:
        assert await requested.get() == 3.0
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
    clock[0] = 101.0

    async def recover() -> list[Mapping[str, object]]:
        return [
            part
            async for part in case.build().open_run(
                thread_id="thread",
                run_id="retry",
                input=None,
            )
        ]

    recovered = asyncio.create_task(recover())
    try:
        assert await requested.get() == 2.0
        release.set()
        assert _interrupt(await recovered)
    finally:
        release.set()
        if not recovered.done():
            recovered.cancel()
            await asyncio.gather(recovered, return_exceptions=True)
    assert len(case.decisions) == 1 and case.child.attempts == 2


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


@pytest.mark.parametrize("standard_policy", [False, True])
async def test_custom_repetition_cannot_hide_a_persistent_delegation_violation(
    standard_policy: bool,
) -> None:
    class Repeating(AgentMiddleware):
        async def awrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        ) -> ToolMessage | Command[Any]:
            for _attempt in range(2):
                try:
                    return await handler(request)
                except (ValueError, DelegationReplayError):
                    continue
            return ToolMessage(
                content="Handled by custom middleware",
                tool_call_id=request.tool_call["id"] or "",
            )

    case = _ReviewCase()
    case.policy_enabled = standard_policy
    case.inner = [Repeating()]
    with pytest.raises(DelegationReplayError, match="cannot repeat"):
        await case.pause()
    assert case.child.attempts == 1 and case.executed == []


async def test_one_tool_entry_uses_one_policy_snapshot_during_execution() -> None:
    case = _ReviewCase()

    class ChangePolicy(AgentMiddleware):
        changed = False

        async def awrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        ) -> ToolMessage | Command[Any]:
            if not self.changed:
                case.policy.max_retries = 0
                self.changed = True
            return await handler(request)

    case.inner = [ChangePolicy()]
    interrupt_id = await case.pause()
    assert case.policy.max_retries == 0 and case.child.attempts == 2
    case.policy.max_retries = 1
    _ = [
        part
        async for part in case.build().open_run(
            thread_id="thread",
            run_id="resume",
            input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
        )
    ]
    assert case.executed == ["approved"] and len(case.decisions) == 1


async def test_zero_retry_review_keeps_its_ordinary_child_checkpoint_scope() -> None:
    case = _ReviewCase()
    case.policy.max_retries = 0
    case.child.failures = 0
    first = [
        part
        async for part in case.build().open_run(
            thread_id="thread",
            run_id="request",
            input={"messages": [{"role": "user", "content": "Do work"}]},
        )
    ]
    scopes = {
        parsed.ns
        for part in first
        if isinstance(
            parsed := validate_native_stream_part(part), NativeValuesStreamPart
        )
        and parsed.ns
    }
    assert len(scopes) == 1 and len(next(iter(scopes))) == 1
    _ = [
        part
        async for part in case.build().open_run(
            thread_id="thread",
            run_id="resume",
            input=Command(
                resume={_interrupt(first): {"decisions": [{"type": "approve"}]}}
            ),
        )
    ]
    assert case.executed == ["approved"] and case.decisions == []
    async for checkpoint in case.saver.alist(None):
        assert all(
            channel != DELEGATION_RECORD_CHANNEL
            for _task, channel, _value in checkpoint.pending_writes or ()
        )


async def test_uncheckpointed_retry_remains_ordinary_execution() -> None:
    case = _ReviewCase()
    runtime = (
        TinkerFin()
        .with_namespace("ordinary")
        .build(
            model=case.main,
            subagents=[
                {
                    "name": "worker",
                    "description": "Do work",
                    "system_prompt": "Do work",
                    "model": case.child,
                    "tools": [case.work],
                }
            ],
            middleware=[case.policy],
        )
    )
    result = await runtime.ainvoke(
        thread_id="thread",
        run_id="request",
        input={"messages": [{"role": "user", "content": "Do work"}]},
    )
    assert cast(list[BaseMessage], result["messages"])[-1].content == "complete"
    assert case.executed == ["approved"] and len(case.decisions) == 1


class _CorruptAncestrySaver(InMemorySaver):
    corrupt = False

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async for checkpoint in super().alist(
            config, filter=filter, before=before, limit=limit
        ):
            if self.corrupt and checkpoint.config.get("configurable", {}).get(
                "checkpoint_ns"
            ):
                checkpoint = checkpoint._replace(
                    metadata={
                        **checkpoint.metadata,
                        "parents": {"": "missing-dispatch"},
                    }
                )
            yield checkpoint


async def test_later_review_requires_the_child_runs_exact_dispatch_ancestry() -> None:
    saver = _CorruptAncestrySaver()
    case = _ReviewCase(saver)
    case.child.responses.insert(
        1,
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "work",
                    "id": "second",
                    "args": {"value": "second"},
                }
            ],
        ),
    )
    first_id = await case.pause()

    def approve(identifier: str) -> AgUiResumeRequest:
        return AgUiResumeRequest.model_validate(
            {
                "entries": [
                    {
                        "interruptId": identifier,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                ]
            }
        )

    first = case.build().open_agui_run(
        thread_id="thread",
        run_id="review-first",
        parent_run_id="request",
        resume=approve(first_id),
    )
    events = [event async for event in first]
    terminal = events[-1]
    assert first.error is None and isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    saver.corrupt = True
    second = case.build().open_agui_run(
        thread_id="thread",
        run_id="review-second",
        parent_run_id="review-first",
        resume=approve(terminal.outcome.interrupts[0].id),
    )
    _ = [event async for event in second]
    assert isinstance(second.error, TinkerFinLifecycleError)
    assert "root checkpoint ownership" in str(second.error)
    assert case.executed == ["approved"] and case.child.attempts == 3


async def test_resumed_callback_keeps_attempt_ownership_and_cached_failure_has_no_callback() -> (
    None
):
    from test_call_observation import _Observer, _Session

    session = _Session()
    case = _ReviewCase()
    case.observers = (_Observer(session),)
    interrupt_id = await case.pause()
    _ = [
        part
        async for part in case.build().open_run(
            thread_id="thread",
            run_id="resume",
            input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
        )
    ]
    starts = [
        item
        for item in session.observations
        if isinstance(item, ToolExecutionObservation)
        and item.tool_name == "task"
        and item.phase == "started"
    ]
    assert len(starts) == 3
    assert starts[0].graph_task_id != starts[1].graph_task_id
    assert starts[1].graph_task_id == starts[2].graph_task_id
    assert starts[1].graph_namespace == starts[2].graph_namespace
    assert starts[1].execution_id != starts[2].execution_id
    assert {item.tool_call_namespace for item in starts} == {()}
    assert (
        len({item.delegation.id for item in starts if item.delegation is not None}) == 1
    )
    assert all(item.delegation is not None for item in starts)


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
