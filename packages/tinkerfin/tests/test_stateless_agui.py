from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextvars import ContextVar
from types import ModuleType

import pytest
from ag_ui.core import (
    BaseEvent,
    RunErrorEvent,
    RunStartedEvent,
)
from langchain.agents.middleware.types import InputAgentState
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from tinkerfin import (
    AgentRuntime,
    AgUiRunStream,
    RunIdentity,
    TinkerFinStreamProtocolError,
)
from tinkerfin_native_stream import NativeStreamContractError


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


class _SourceGraph:
    def __init__(self, source_factory: Callable[[], AsyncIterator[object]]) -> None:
        self._source_factory = source_factory

    def astream(
        self,
        *_args: object,
        **_options: object,
    ) -> AsyncIterator[object]:
        return self._source_factory()


setattr(
    _SourceGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)

_DEFINITION_FACTORY: ContextVar[Callable[..., AgentRuntime[None]] | None] = ContextVar(
    "tinkerfin_test_definition_factory", default=None
)


@pytest.fixture(autouse=True)
def _bind_definition_factory(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> Iterator[None]:
    token = _DEFINITION_FACTORY.set(definition_factory)
    try:
        yield
    finally:
        _DEFINITION_FACTORY.reset(token)


def _agui_stream(
    source_factory: Callable[[], AsyncIterator[object]],
    *,
    identity: RunIdentity | None = None,
    timeout: float | None = None,
    settlement_timeout: float | None = None,
    expose_reasoning_events: bool = False,
    expose_subagent_events: bool = True,
    on_event: Callable[[BaseEvent], Awaitable[None]] | None = None,
) -> AgUiRunStream:
    definition_factory = _DEFINITION_FACTORY.get()
    assert definition_factory is not None
    runtime = definition_factory(_SourceGraph(source_factory))
    return runtime.open_agui_run(
        thread_id=(_identity() if identity is None else identity).thread_id,
        run_id=(_identity() if identity is None else identity).run_id,
        stream_timeout=timeout,
        cleanup_timeout=settlement_timeout,
        include_reasoning_events=expose_reasoning_events,
        include_subagent_events=expose_subagent_events,
        on_agui_event=on_event,
        input=InputAgentState(messages=[]),
    )


def _initialization_failure(
    error: Exception, *, identity: RunIdentity, parent_run_id: str | None = None
) -> AgUiRunStream:
    build = _DEFINITION_FACTORY.get()
    assert build is not None
    return build(error).open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        input=InputAgentState(messages=[]),
        parent_run_id=parent_run_id,
    )


def test_agui_rejects_noncanonical_identity_at_construction() -> None:
    with pytest.raises(ValidationError):
        _identity(thread_id=" ")
    with pytest.raises(ValidationError):
        _identity(run_id="")


def test_agui_rejects_infinite_total_timeout() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    with pytest.raises(ValueError, match="finite and non-negative"):
        _agui_stream(parts, timeout=float("inf"))


@pytest.mark.asyncio
async def test_agui_stream_publishes_its_idempotent_cancel_callback() -> None:
    async def parts() -> AsyncIterator[object]:
        await asyncio.Event().wait()
        if False:  # pragma: no cover - supplies the async iterator shape
            yield None

    identity = _identity()
    stream = _agui_stream(parts, identity=identity)

    assert stream.messaging_identity == identity
    assert stream.messaging_cancel_callback == stream.abort
    assert (await anext(stream)).type.value == "RUN_STARTED"
    tail = await stream.messaging_cancel_callback()

    assert [event.type.value for event in tail] == ["RUN_ERROR"]
    assert await stream.messaging_cancel_callback() == []


@pytest.mark.asyncio
async def test_agui_stream_keeps_its_immutable_identity() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - supplies the async iterator shape
            yield None

    identity = _identity()
    stream = _agui_stream(parts, identity=identity)

    started = await anext(stream)

    assert isinstance(started, RunStartedEvent)
    assert stream.messaging_identity == identity
    assert started.input is None
    await stream.aclose()


@pytest.mark.asyncio
async def test_initialization_failure_uses_the_standard_complete_lifecycle() -> None:
    identity = _identity()

    stream = _initialization_failure(
        RuntimeError("cannot initialize runtime"),
        identity=identity,
    )
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    started = events[0]
    failed = events[1]
    assert isinstance(started, RunStartedEvent)
    assert started.input is None
    assert isinstance(failed, RunErrorEvent)
    assert failed.code == "runtime_initialization_error"
    assert failed.raw_event == {
        "threadId": "thread-1",
        "runId": "run-1",
        "initializationFailed": True,
    }


@pytest.mark.asyncio
async def test_initialization_failure_marker_uses_canonical_identity_and_parent() -> (
    None
):
    identity = _identity(thread_id="thread-1")
    stream = _initialization_failure(
        RuntimeError("cannot initialize runtime"),
        identity=identity,
        parent_run_id="run-parent",
    )

    events = [event async for event in stream]

    assert events[0].raw_event == {
        "threadId": "thread-1",
        "runId": "run-1",
        "parentRunId": "run-parent",
        "initializationFailed": True,
    }
    assert events[-1].raw_event == events[0].raw_event


@pytest.mark.asyncio
async def test_initialization_failure_cancel_tail_keeps_release_marker() -> None:
    stream = _initialization_failure(
        RuntimeError("cannot initialize runtime"),
        identity=_identity(),
    )

    started = await anext(stream)
    tail = await stream.messaging_cancel_callback()

    assert isinstance(started, RunStartedEvent)
    assert started.raw_event == {
        "threadId": "thread-1",
        "runId": "run-1",
        "initializationFailed": True,
    }
    assert [event.type.value for event in tail] == ["RUN_ERROR"]
    assert tail[0].raw_event == {
        "threadId": "thread-1",
        "runId": "run-1",
        "initializationFailed": True,
    }


@pytest.mark.asyncio
async def test_agui_observes_converted_events_before_delivery() -> None:
    upstream_closed = asyncio.Event()
    order: list[tuple[str, str]] = []

    async def parts() -> AsyncIterator[object]:
        try:
            yield {
                "type": "values",
                "ns": (),
                "data": {"answer": 42},
                "interrupts": (),
            }
        finally:
            upstream_closed.set()

    async def on_event(event: BaseEvent) -> None:
        order.append(("observed", event.type.value))

    stream = _agui_stream(parts, on_event=on_event)
    events: list[BaseEvent] = []
    async for event in stream:
        events.append(event)
        order.append(("delivered", event.type.value))

    assert [event.type.value for event in events] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
    ]
    assert isinstance(events[0], RunStartedEvent)
    assert events[0].input is None
    assert order == [
        item
        for event in events
        for item in (
            ("observed", event.type.value),
            ("delivered", event.type.value),
        )
    ]
    assert upstream_closed.is_set()


@pytest.mark.asyncio
async def test_agui_abort_rejects_reentry_from_its_event_observer() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    stream: AgUiRunStream | None = None

    async def on_event(_: BaseEvent) -> None:
        assert stream is not None
        await stream.abort()

    stream = _agui_stream(parts, on_event=on_event)

    with pytest.raises(RuntimeError, match="from a run callback"):
        await anext(stream)


@pytest.mark.asyncio
async def test_agui_abort_rejects_child_task_reentry_from_event_observer() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    stream: AgUiRunStream | None = None
    abort_task: asyncio.Task[list[BaseEvent]] | None = None

    async def on_event(_: BaseEvent) -> None:
        nonlocal abort_task
        if abort_task is None:
            assert stream is not None
            abort_task = asyncio.create_task(stream.abort())
            await asyncio.sleep(0)

    stream = _agui_stream(parts, on_event=on_event)
    consumer = asyncio.create_task(anext(stream))
    try:
        consumer_outcome = (await asyncio.gather(consumer, return_exceptions=True))[0]
        assert abort_task is not None
        abort_outcome = (await asyncio.gather(abort_task, return_exceptions=True))[0]

        assert isinstance(consumer_outcome, BaseEvent)
        assert isinstance(abort_outcome, RuntimeError)
        assert "from a run callback" in str(abort_outcome)
    finally:
        await asyncio.gather(consumer, return_exceptions=True)
        if abort_task is not None:
            await asyncio.gather(abort_task, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_agui_abort_allows_external_task_while_event_observer_is_active() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    observer_started = asyncio.Event()
    hold_observer = asyncio.Event()

    async def on_event(event: BaseEvent) -> None:
        if event.type.value == "RUN_STARTED" and not observer_started.is_set():
            observer_started.set()
            await hold_observer.wait()

    stream = _agui_stream(parts, on_event=on_event)
    consumer = asyncio.create_task(anext(stream))
    await observer_started.wait()

    try:
        abort_tail = await stream.abort()

        assert consumer.cancelled()
        assert [event.type.value for event in abort_tail] == ["RUN_ERROR"]
    finally:
        hold_observer.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_agui_aclose_from_observer_child_task_preserves_current_event() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    stream: AgUiRunStream | None = None
    close_task: asyncio.Task[None] | None = None

    async def on_event(_: BaseEvent) -> None:
        nonlocal close_task
        if close_task is None:
            assert stream is not None
            close_task = asyncio.create_task(stream.aclose())
            await asyncio.sleep(0)

    stream = _agui_stream(parts, on_event=on_event)
    consumer = asyncio.create_task(anext(stream))
    try:
        consumer_outcome = (await asyncio.gather(consumer, return_exceptions=True))[0]
        assert close_task is not None
        close_outcome = (await asyncio.gather(close_task, return_exceptions=True))[0]

        assert isinstance(consumer_outcome, BaseEvent)
        assert close_outcome is None
    finally:
        await asyncio.gather(consumer, return_exceptions=True)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_agui_abort_from_terminal_observer_is_an_idempotent_noop() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    stream: AgUiRunStream | None = None
    abort_tail: list[BaseEvent] | None = None

    async def on_event(event: BaseEvent) -> None:
        nonlocal abort_tail
        if event.type.value == "RUN_FINISHED":
            assert stream is not None
            abort_tail = await stream.abort()

    stream = _agui_stream(parts, on_event=on_event)
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_FINISHED"]
    assert abort_tail == []


@pytest.mark.asyncio
async def test_agui_conversion_error_emits_one_terminal_without_package_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def parts() -> AsyncIterator[object]:
        yield {
            "type": "not-a-stream-mode",
            "ns": (),
            "data": {"api_token": "SECRET-RUNTIME"},
        }

    stream = _agui_stream(parts)
    with caplog.at_level(logging.DEBUG, logger="tinkerfin"):
        events = [event async for event in stream]

    terminals = [event for event in events if isinstance(event, RunErrorEvent)]
    assert len(terminals) == 1
    assert terminals[0].message == "Agent run failed"
    assert isinstance(stream.error, TinkerFinStreamProtocolError)
    assert isinstance(stream.error.cause, NativeStreamContractError)
    assert isinstance(stream.error.cause.cause, ValidationError)
    assert caplog.records == []
    assert "SECRET-RUNTIME" not in caplog.text


@pytest.mark.asyncio
async def test_agui_abort_after_error_terminal_does_not_emit_another_terminal() -> None:
    async def parts() -> AsyncIterator[object]:
        yield {"type": "not-a-stream-mode", "ns": (), "data": {}}

    stream = _agui_stream(parts)
    events = [event async for event in stream]

    assert len([event for event in events if isinstance(event, RunErrorEvent)]) == 1
    assert await stream.abort() == []


class _BlockingParts:
    def __init__(self) -> None:
        self.pull_started = asyncio.Event()
        self.release_pull = asyncio.Event()
        self.closed = asyncio.Event()

    def __aiter__(self) -> _BlockingParts:
        return self

    async def __anext__(self) -> object:
        self.pull_started.set()
        await self.release_pull.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.release_pull.set()
        self.closed.set()


async def _collect_events(stream: AsyncIterator[BaseEvent]) -> list[BaseEvent]:
    return [event async for event in stream]


class _SlowClosingParts:
    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.closed = asyncio.Event()
        self._yielded = False

    def __aiter__(self) -> _SlowClosingParts:
        return self

    async def __anext__(self) -> object:
        if not self._yielded:
            self._yielded = True
            return {
                "type": "values",
                "ns": (),
                "data": {"value": 1},
                "interrupts": (),
            }
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        self.closed.set()


class _InterruptedClosingParts:
    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_completed = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _InterruptedClosingParts:
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        self.close_completed.set()


class _TwoStageFailingCloseParts:
    def __init__(self) -> None:
        self._yielded = False
        self.close_calls = 0

    def __aiter__(self) -> _TwoStageFailingCloseParts:
        return self

    async def __anext__(self) -> object:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return {"type": "not-a-stream-mode", "ns": (), "data": {}}

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise asyncio.CancelledError("close awaitable cancelled itself")
        raise RuntimeError("second close failed")


class _CancelledFailingCloseParts:
    def __init__(self) -> None:
        self._yielded = False
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _CancelledFailingCloseParts:
        return self

    async def __anext__(self) -> object:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return {"type": "not-a-stream-mode", "ns": (), "data": {}}

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.release_close.wait()
        raise RuntimeError("native close failed")


class _BudgetedClosingParts:
    def __init__(self) -> None:
        self.pull_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _BudgetedClosingParts:
        return self

    async def __anext__(self) -> object:
        self.pull_started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked source unexpectedly resumed")

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        self.closed.set()


@pytest.mark.asyncio
async def test_agui_conversion_error_survives_two_upstream_close_failures() -> None:
    parts = _TwoStageFailingCloseParts()
    from tinkerfin.runtime import AgUiEventStream

    stream = AgUiEventStream(
        parts=parts,
        identity=_identity(),
        expose_reasoning_events=False,
        expose_subagent_events=True,
        prior_tool_call_ids=frozenset(),
        timeout=None,
        on_event=None,
    )

    events = await _collect_events(stream)

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert isinstance(stream.error, TinkerFinStreamProtocolError)
    assert isinstance(stream.error.cause, NativeStreamContractError)
    assert isinstance(stream.error.cause.cause, ValidationError)
    assert any(
        "CancelledError: close awaitable cancelled itself" in note
        for note in stream.error.__notes__
    )
    assert any(
        "RuntimeError: second close failed" in note for note in stream.error.__notes__
    )
    assert parts.close_calls == 2


@pytest.mark.asyncio
async def test_agui_caller_cancellation_keeps_conversion_and_cleanup_evidence() -> None:
    parts = _CancelledFailingCloseParts()
    stream = _agui_stream(lambda: parts)
    assert (await anext(stream)).type.value == "RUN_STARTED"
    consumer = asyncio.create_task(anext(stream))
    await parts.close_started.wait()
    consumer.cancel("caller stopped")
    parts.release_close.set()

    try:
        with pytest.raises(asyncio.CancelledError, match="caller stopped") as raised:
            await consumer
        notes = raised.value.__notes__
        assert any("TinkerFinStreamProtocolError" in note for note in notes)
        assert any("RuntimeError: native close failed" in note for note in notes)
        assert isinstance(stream.error, TinkerFinStreamProtocolError)
        assert isinstance(stream.error.cause, NativeStreamContractError)
        assert isinstance(stream.error.cause.cause, ValidationError)
    finally:
        parts.release_close.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.gather(stream.aclose(), return_exceptions=True)


def test_agui_rejects_invalid_settlement_timeout() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    for invalid in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite and non-negative"):
            _agui_stream(parts, settlement_timeout=invalid)


@pytest.mark.asyncio
async def test_agui_settlement_timeout_retains_close_for_a_second_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadlines: dict[int, asyncio.Timeout] = {}

    def timeout(delay: float | None) -> asyncio.Timeout:
        deadline = asyncio.Timeout(None)
        deadlines[id(asyncio.current_task())] = deadline
        return deadline

    controlled = ModuleType("controlled_asyncio")
    controlled.__dict__.update(vars(asyncio))
    setattr(controlled, "timeout", timeout)
    monkeypatch.setattr("tinkerfin._runtime_agui.asyncio", controlled)
    parts = _BudgetedClosingParts()
    stream = _agui_stream(lambda: parts, settlement_timeout=30)
    assert (await anext(stream)).type.value == "RUN_STARTED"
    active_pull = asyncio.create_task(anext(stream))
    await parts.pull_started.wait()
    closing = asyncio.create_task(stream.aclose())

    try:
        await parts.close_started.wait()
        # Expire only this caller's wait once native cleanup is demonstrably active.
        deadlines[id(closing)].reschedule(0)
        with pytest.raises(TimeoutError, match="settlement timed out") as raised:
            await closing
        assert type(raised.value).__name__ == "AgUiSettlementTimeoutError"
        assert parts.close_started.is_set()
        assert not parts.close_cancelled.is_set()
        assert not parts.closed.is_set()

        parts.release_close.set()
        await stream.aclose()
    finally:
        parts.release_close.set()
        await asyncio.gather(active_pull, closing, return_exceptions=True)
        await asyncio.gather(stream.aclose(), return_exceptions=True)

    assert parts.closed.is_set()
    assert parts.close_calls == 1
    assert active_pull.cancelled()


@pytest.mark.asyncio
async def test_agui_abort_cancels_active_pull_and_returns_observed_tail_once() -> None:
    parts = _BlockingParts()
    observed: list[str] = []

    async def on_event(event: BaseEvent) -> None:
        observed.append(event.type.value)

    stream = _agui_stream(lambda: parts, on_event=on_event)
    started = await anext(stream)
    assert started.type.value == "RUN_STARTED"
    pending = asyncio.create_task(anext(stream))
    await parts.pull_started.wait()

    try:
        tail = await stream.abort()
    finally:
        if not pending.done():
            pending.cancel()
        outcome = (await asyncio.gather(pending, return_exceptions=True))[0]
        await stream.aclose()

    assert isinstance(outcome, asyncio.CancelledError)
    assert parts.closed.is_set()
    assert [event.type.value for event in tail] == ["RUN_ERROR"]
    terminal = tail[0]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "cancelled"
    assert observed == ["RUN_STARTED", "RUN_ERROR"]
    assert await stream.abort() == []


@pytest.mark.asyncio
async def test_agui_zero_timeout_starts_lifecycle_without_pulling_parts() -> None:
    pulls = 0

    async def parts() -> AsyncIterator[object]:
        nonlocal pulls
        pulls += 1
        yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}

    stream = _agui_stream(parts, timeout=0)
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "stream_timeout"
    assert isinstance(stream.error, TimeoutError)
    assert pulls == 0


@pytest.mark.asyncio
async def test_upstream_timeout_error_remains_a_runtime_error() -> None:
    async def parts() -> AsyncIterator[object]:
        raise TimeoutError("provider request timed out")
        yield  # pragma: no cover - keeps the function an async generator

    stream = _agui_stream(parts, timeout=1)

    events = await _collect_events(stream)

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "runtime_error"
    assert isinstance(stream.error, TimeoutError)
    assert str(stream.error) == "provider request timed out"


@pytest.mark.asyncio
async def test_agui_waits_for_cleanup_when_consumer_is_cancelled_during_failure() -> (
    None
):
    parts = _SlowClosingParts()

    async def on_event(event: BaseEvent) -> None:
        if event.type.value == "STATE_SNAPSHOT":
            raise RuntimeError("event observer failed")

    stream = _agui_stream(lambda: parts, on_event=on_event)
    assert (await anext(stream)).type.value == "RUN_STARTED"
    consumer = asyncio.create_task(anext(stream))
    await parts.close_started.wait()
    consumer.cancel("request cancelled during AG-UI cleanup")

    try:
        await asyncio.sleep(0)
        assert not consumer.done()
        parts.release_close.set()
        with pytest.raises(
            asyncio.CancelledError,
            match="request cancelled during AG-UI cleanup",
        ):
            await consumer
    finally:
        parts.release_close.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()

    assert parts.closed.is_set()


@pytest.mark.asyncio
async def test_agui_cancelled_terminal_pull_waits_for_upstream_close() -> None:
    parts = _InterruptedClosingParts()
    stream = _agui_stream(lambda: parts)
    assert (await anext(stream)).type.value == "RUN_STARTED"
    terminal_pull = asyncio.create_task(anext(stream))
    await parts.close_started.wait()

    terminal_pull.cancel("request cancelled during native parts close")
    await asyncio.sleep(0)
    assert not terminal_pull.done()
    assert not parts.close_cancelled.is_set()
    parts.release_close.set()

    try:
        with pytest.raises(
            asyncio.CancelledError,
            match="request cancelled during native parts close",
        ):
            await terminal_pull
        await stream.aclose()
    finally:
        parts.release_close.set()
        await asyncio.gather(terminal_pull, return_exceptions=True)

    assert parts.close_calls == 1
    assert parts.close_completed.is_set()
