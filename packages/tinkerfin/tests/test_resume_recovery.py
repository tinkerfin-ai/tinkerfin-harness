"""Approval continuation after completed siblings and partial durable execution."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from ag_ui.core import BaseEvent, RunFinishedEvent, RunFinishedInterruptOutcome
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, interrupt

from tinkerfin import AgentRuntime, AgUiResumeCheckpoint, AgUiResumeRequest, TinkerFin
from tinkerfin._agent_spec import AgentSpec
from tinkerfin._checkpoint import NamespaceCheckpointer


def _review(name: str) -> dict[str, object]:
    return {
        "action_requests": [{"name": name, "args": {}}],
        "review_configs": [
            {"action_name": name, "allowed_decisions": ["approve", "reject"]}
        ],
    }


def _request(terminal: BaseEvent) -> AgUiResumeRequest:
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
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


def _input(*names: str) -> InputAgentState:
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": {}, "id": f"call-{name}"} for name in names
                ],
            )
        ]
    }


def _completed(name: str) -> dict[str, object]:
    return {"messages": [ToolMessage(content="done", tool_call_id=f"call-{name}")]}


def _runtime(
    saver: InMemorySaver,
    graph: StateGraph[MessagesState],
    monkeypatch: pytest.MonkeyPatch,
) -> AgentRuntime[None]:
    def build(spec: AgentSpec[None], **kwargs: object) -> object:
        return graph.compile(checkpointer=spec.checkpointer).with_config(
            {"configurable": {"__pregel_durability": "sync"}}
        )

    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", build)
    return (
        TinkerFin(checkpointer=saver)
        .with_namespace("recovery")
        .build(model="provider:model", tools=[])
    )


async def test_first_approval_preserves_a_completed_parallel_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[str] = []

    async def completed(state: MessagesState) -> dict[str, object]:
        del state
        actions.append("completed")
        return _completed("first")

    async def reviewed(state: MessagesState) -> dict[str, object]:
        del state
        interrupt(_review("second"))
        actions.append("reviewed")
        return _completed("second")

    graph = StateGraph(MessagesState)
    graph.add_node("completed", completed)
    graph.add_node("reviewed", reviewed)
    for name in ("completed", "reviewed"):
        graph.add_edge(START, name)
        graph.add_edge(name, END)
    runtime = _runtime(InMemorySaver(), graph, monkeypatch)
    initial = runtime.open_agui_run(
        thread_id="thread", run_id="A", input=_input("first", "second")
    )
    events = [event async for event in initial]
    assert initial.error is None
    assert actions == ["completed"]
    resumed = runtime.open_agui_run(
        thread_id="thread", run_id="B", resume=_request(events[-1])
    )
    result = [event async for event in resumed]
    assert resumed.error is None
    assert isinstance(result[-1], RunFinishedEvent)
    assert result[-1].outcome is not None
    assert result[-1].outcome.type == "success"
    assert actions == ["completed", "reviewed"]


@pytest.mark.parametrize("native_decision", ["approve", "reject"])
async def test_another_native_run_cannot_be_counted_as_this_approval(
    monkeypatch: pytest.MonkeyPatch,
    native_decision: str,
) -> None:
    actions: list[str] = []

    async def first(state: MessagesState) -> dict[str, object]:
        del state
        answer = interrupt(_review("first"))
        assert isinstance(answer, dict)
        actions.append(cast(str, answer["decisions"][0]["type"]))
        return _completed("first")

    async def second(state: MessagesState) -> dict[str, object]:
        del state
        interrupt(_review("second"))
        actions.append("second")
        return _completed("second")

    graph = StateGraph(MessagesState)
    graph.add_node("first", first)
    graph.add_node("second", second)
    for name in ("first", "second"):
        graph.add_edge(START, name)
        graph.add_edge(name, END)
    saver = InMemorySaver()
    runtime = _runtime(saver, graph, monkeypatch)
    initial = runtime.open_agui_run(
        thread_id="thread", run_id="A", input=_input("first", "second")
    )
    events = [event async for event in initial]
    assert initial.error is None
    request = _request(events[-1])

    async def stop_before_submission(checkpoint: AgUiResumeCheckpoint) -> None:
        assert checkpoint.identity.run_id == "B"
        raise RuntimeError("checkpoint callback failed")

    staged = runtime.open_agui_run(
        thread_id="thread",
        run_id="B",
        resume=request,
        on_resume_saved=stop_before_submission,
    )
    [event async for event in staged]
    assert staged.error is not None
    snapshot = await graph.compile(
        checkpointer=NamespaceCheckpointer(saver, "recovery")
    ).aget_state({"configurable": {"thread_id": "thread"}})
    first_id = next(
        task.interrupts[0].id for task in snapshot.tasks if task.name == "first"
    )
    native = runtime.open_run(
        thread_id="thread",
        run_id="C",
        input=Command(resume={first_id: {"decisions": [{"type": native_decision}]}}),
    )
    [part async for part in native]
    assert native.error is None
    assert actions == [native_decision]
    retried = runtime.open_agui_run(thread_id="thread", run_id="B", resume=request)
    result = [event async for event in retried]
    assert retried.error is not None
    assert result[-1].type.value == "RUN_ERROR"
    assert actions == [native_decision]


class _CrashSnapshotSaver(InMemorySaver):
    """Capture only serialized durable storage before cancellation adds ERROR writes."""

    def __init__(self, channel: str) -> None:
        super().__init__()
        self.channel = channel
        self.armed = False
        self.snapshot: InMemorySaver | None = None
        self.ready = asyncio.Event()

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await super().aput_writes(config, writes, task_id, task_path)
        if (
            self.armed
            and self.snapshot is None
            and {"__resume__", self.channel} <= {channel for channel, _ in writes}
        ):
            restored = InMemorySaver()
            restored.storage = copy.deepcopy(self.storage)
            restored.writes = copy.deepcopy(self.writes)
            restored.blobs = copy.deepcopy(self.blobs)
            self.snapshot = restored
            self.ready.set()


class _ResumeWriteFailureSaver(InMemorySaver):
    """Capture a process-loss boundary before the live Graph can persist its error."""

    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure
        self.armed = False
        self.snapshot: InMemorySaver | None = None

    def capture(self) -> None:
        saved = InMemorySaver()
        saved.storage = copy.deepcopy(self.storage)
        saved.writes = copy.deepcopy(self.writes)
        saved.blobs = copy.deepcopy(self.blobs)
        self.snapshot = saved

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        channels = {channel for channel, _ in writes}
        reservation = "_tinkerfin_resume_receipt" in channels
        native = "__resume__" in channels
        fail = (
            self.armed
            and self.snapshot is None
            and (
                (reservation and "reservation" in self.failure)
                or (native and "native" in self.failure)
            )
        )
        if fail and self.failure.startswith("before"):
            self.capture()
            raise OSError("test-owned saver write failed before persistence")
        await super().aput_writes(config, writes, task_id, task_path)
        if fail:
            self.capture()
            if self.failure == "reservation_cancel":
                raise asyncio.CancelledError
            raise OSError("test-owned saver write failed after persistence")


@pytest.mark.parametrize(
    "failure",
    [
        "before_reservation",
        "after_reservation",
        "before_native",
        "after_native",
        "reservation_cancel",
    ],
)
async def test_original_approval_recovers_from_each_decision_write_window(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    saver = _ResumeWriteFailureSaver(failure)

    async def reviewed(state: MessagesState) -> dict[str, object]:
        del state
        interrupt(_review("first"))
        return _completed("first")

    graph = StateGraph(MessagesState)
    graph.add_node("reviewed", reviewed)
    graph.add_edge(START, "reviewed")
    graph.add_edge("reviewed", END)
    runtime = _runtime(saver, graph, monkeypatch)
    initial = runtime.open_agui_run(
        thread_id="thread", run_id="A", input=_input("first")
    )
    events = [event async for event in initial]
    assert initial.error is None
    request = _request(events[-1])
    saver.armed = True
    broken = runtime.open_agui_run(thread_id="thread", run_id="B", resume=request)
    try:
        [event async for event in broken]
    except asyncio.CancelledError:
        assert failure == "reservation_cancel"
    finally:
        try:
            await broken.aclose()
        except asyncio.CancelledError:
            assert failure == "reservation_cancel"
    assert saver.snapshot is not None
    recovered = _runtime(saver.snapshot, graph, monkeypatch).open_agui_run(
        thread_id="thread", run_id="B", resume=request
    )
    result = [event async for event in recovered]
    assert recovered.error is None
    terminal = result[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "success"


async def test_native_resume_retains_opaque_values_without_fabricating_json_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[object] = []

    async def paused(state: MessagesState) -> dict[str, object]:
        del state
        observed.append(interrupt("Provide a native value"))
        return {}

    graph = StateGraph(MessagesState)
    graph.add_node("paused", paused)
    graph.add_edge(START, "paused")
    graph.add_edge("paused", END)
    runtime = _runtime(InMemorySaver(), graph, monkeypatch)
    initial = runtime.open_run(thread_id="thread", run_id="A", input=_input())
    [part async for part in initial]
    assert initial.error is None
    value = {
        "when": datetime(2026, 9, 10, tzinfo=UTC),
        "raw": b"binary",
        "set": {1, 2},
    }
    resumed = runtime.open_run(
        thread_id="thread", run_id="B", input=Command(resume=value)
    )
    [part async for part in resumed]
    assert resumed.error is None
    assert observed == [value]


async def test_approval_retry_rejects_an_ordinary_run_reusing_its_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visits: list[bool] = []

    async def paused(state: MessagesState) -> dict[str, object]:
        del state
        visits.append(True)
        interrupt(_review("first"))
        return _completed("first")

    graph = StateGraph(MessagesState)
    graph.add_node("paused", paused)
    graph.add_edge(START, "paused")
    graph.add_edge("paused", END)
    runtime = _runtime(InMemorySaver(), graph, monkeypatch)
    initial = runtime.open_agui_run(
        thread_id="thread", run_id="A", input=_input("first")
    )
    events = [event async for event in initial]
    request = _request(events[-1])
    approved = runtime.open_agui_run(thread_id="thread", run_id="B", resume=request)
    [event async for event in approved]
    assert approved.error is None
    ordinary = runtime.open_run(thread_id="thread", run_id="B", input=_input("next"))
    [part async for part in ordinary]
    assert ordinary.error is None
    before_retry = len(visits)
    retried = runtime.open_agui_run(thread_id="thread", run_id="B", resume=request)
    [event async for event in retried]
    assert retried.error is not None
    assert "resume intent" in str(retried.error)
    assert len(visits) == before_retry


class _DecisionSaveControl(BaseException):
    pass


class _BlockedDecisionSaver(InMemorySaver):
    """Hold one real saver boundary until cancellation has reached a sibling."""

    def __init__(self, boundary: str, failure: BaseException) -> None:
        super().__init__()
        self.boundary = boundary
        self.failure = failure
        self.armed = False
        self.reads_armed = False
        self.receipt_written = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def fail_after_release(self) -> None:
        self.entered.set()
        try:
            await self.release.wait()
            raise self.failure
        finally:
            self.finished.set()

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        channels = {channel for channel, _ in writes}
        receipt = "_tinkerfin_resume_receipt" in channels
        native = "__resume__" in channels
        target = (
            self.armed
            and not self.entered.is_set()
            and (
                (receipt and self.boundary.startswith("receipt"))
                or (native and self.boundary.startswith("native"))
            )
        )
        if target and self.boundary.endswith("before"):
            await self.fail_after_release()
        await super().aput_writes(config, writes, task_id, task_path)
        if receipt:
            self.receipt_written = True
        if target:
            await self.fail_after_release()

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        target = (
            self.armed
            and not self.entered.is_set()
            and (
                (self.boundary == "initial_read" and self.reads_armed)
                or (self.boundary == "readback" and self.receipt_written)
            )
        )
        if target:
            await self.fail_after_release()
        return await super().aget_tuple(config)


def _failure_text(error: BaseException | None) -> str:
    seen: set[int] = set()
    parts: list[str] = []
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        parts.append(f"{error} {getattr(error, '__notes__', ())}")
        error = error.__cause__ or error.__context__
    return " ".join(parts)


@pytest.mark.parametrize(
    "boundary",
    [
        "initial_read",
        "receipt_before",
        "receipt_after",
        "readback",
        "native_before",
        "native_after",
    ],
)
@pytest.mark.parametrize("control", [False, True])
async def test_decision_storage_failure_survives_concurrent_run_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    control: bool,
) -> None:
    failure = (
        _DecisionSaveControl("decision-save-failed")
        if control
        else OSError("decision-save-failed")
    )
    saver = _BlockedDecisionSaver(boundary, failure)
    holding = False
    sibling_entered = asyncio.Event()
    sibling_closed = asyncio.Event()
    block = asyncio.Event()

    async def first(state: MessagesState) -> dict[str, object]:
        del state
        interrupt(_review("first"))
        if holding:
            await sibling_entered.wait()
            saver.reads_armed = True
        return _completed("first")

    async def second(state: MessagesState) -> dict[str, object]:
        del state
        interrupt(_review("second"))
        if holding:
            sibling_entered.set()
            try:
                await block.wait()
            finally:
                sibling_closed.set()
        return _completed("second")

    graph = StateGraph(MessagesState)
    graph.add_node("first", first)
    graph.add_node("second", second)
    for name in ("first", "second"):
        graph.add_edge(START, name)
        graph.add_edge(name, END)
    runtime = _runtime(saver, graph, monkeypatch)
    initial = runtime.open_agui_run(
        thread_id="thread", run_id="A", input=_input("first", "second")
    )
    events = [event async for event in initial]
    assert initial.error is None
    holding = saver.armed = True
    resumed = runtime.open_agui_run(
        thread_id="thread", run_id="B", resume=_request(events[-1])
    )

    async def consume() -> list[BaseEvent]:
        return [event async for event in resumed]

    consumer = asyncio.create_task(consume())
    outcome: BaseException | None = None
    close_error: BaseException | None = None
    try:
        await saver.entered.wait()
        consumer.cancel()
        await sibling_closed.wait()
        saver.release.set()
        try:
            await consumer
        except (OSError, asyncio.CancelledError, _DecisionSaveControl) as error:
            outcome = error
    finally:
        saver.release.set()
        block.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        try:
            await resumed.aclose()
        except (OSError, asyncio.CancelledError, _DecisionSaveControl) as error:
            close_error = error
    assert saver.finished.is_set()
    assert "decision-save-failed" in " ".join(
        _failure_text(error) for error in (outcome, close_error, resumed.error)
    )
    if control:
        assert outcome is failure or close_error is failure
    else:
        assert isinstance(outcome, asyncio.CancelledError)


@pytest.mark.parametrize("next_interrupt", [False, True])
async def test_partial_crash_resumes_only_unconsumed_original_decisions(
    monkeypatch: pytest.MonkeyPatch,
    next_interrupt: bool,
) -> None:
    saver = _CrashSnapshotSaver("__interrupt__" if next_interrupt else "messages")
    crashing = False
    right_waiting = asyncio.Event()
    release = asyncio.Event()
    actions: list[str] = []

    async def left(state: MessagesState) -> dict[str, object]:
        del state
        interrupt(_review("first"))
        if crashing:
            await right_waiting.wait()
        actions.append("first")
        if next_interrupt:
            interrupt(_review("third"))
            actions.append("third")
        return _completed("first")

    async def right(state: MessagesState) -> dict[str, object]:
        del state
        if crashing:
            right_waiting.set()
            await release.wait()
        interrupt(_review("second"))
        actions.append("second")
        return _completed("second")

    graph = StateGraph(MessagesState)
    graph.add_node("left", left)
    graph.add_node("right", right)
    for name in ("left", "right"):
        graph.add_edge(START, name)
        graph.add_edge(name, END)
    runtime = _runtime(saver, graph, monkeypatch)
    initial = runtime.open_agui_run(
        thread_id="thread", run_id="A", input=_input("first", "second", "third")
    )
    events = [event async for event in initial]
    assert initial.error is None
    request = _request(events[-1])
    assert len(request.entries) == 2
    crashing = saver.armed = True
    submitting = runtime.open_agui_run(thread_id="thread", run_id="B", resume=request)

    async def consume() -> list[BaseEvent]:
        return [event async for event in submitting]

    running = asyncio.create_task(consume())
    try:
        await saver.ready.wait()
        assert saver.snapshot is not None
        recovered = saver.snapshot
        assert actions == ["first"]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await submitting.aclose()
    crashing = False
    view = NamespaceCheckpointer(recovered, "recovery")
    before = await view.aget_tuple({"configurable": {"thread_id": "thread"}})
    assert before is not None
    assert (
        sum(channel == "__resume__" for _, channel, _ in before.pending_writes or ())
        == 1
    )
    assert all(channel != "__error__" for _, channel, _ in before.pending_writes or ())
    resumed = _runtime(recovered, graph, monkeypatch).open_agui_run(
        thread_id="thread", run_id="B", resume=request
    )
    result = [event async for event in resumed]
    assert resumed.error is None
    terminal = result[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert actions.count("second") == 1
    assert "third" not in actions
    if next_interrupt:
        assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
        assert len(terminal.outcome.interrupts) == 1
        original = await view.aget_tuple(before.config)
        assert original is not None
        assert all(
            len(cast(list[object], values)) == 1
            for _, channel, values in original.pending_writes or ()
            if channel == "__resume__"
        )
    else:
        assert terminal.outcome is not None
        assert terminal.outcome.type == "success"
        assert actions.count("first") == 1
