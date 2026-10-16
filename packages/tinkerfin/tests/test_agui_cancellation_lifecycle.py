"""Cancellation durability, observer failures, and ownership boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from ag_ui.core import RunErrorEvent, RunFinishedEvent
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver, PersistentDict
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine
from test_agui_cancellation import _proposal
from test_agui_history import _ToolModel

from tinkerfin import AgentRuntime, AgUiResumeRequest, TinkerFin
from tinkerfin_contracts import (
    ObservationBoundary,
    RunObservationSession,
    RunSourceContext,
    RuntimeObservation,
)
from tinkerfin_tracing import SqlAlchemyTraceStore, Tracer
from tinkerfin_tracing.sql_schema import projection_checkpoints


async def _pending_question(
    saver: InMemorySaver, tracer: Tracer, observer: _ControlledObserver | None = None
) -> tuple[AgentRuntime[None], AgUiResumeRequest]:
    builder = (
        TinkerFin(checkpointer=saver)
        .with_namespace("cancellation")
        .with_observer(tracer)
        .with_plan()
    )
    if observer is not None:
        builder = builder.with_observer(observer)
    runtime = builder.build(
        model=_ToolModel(
            responses=[_proposal("clarification"), AIMessage(content="Done")]
        )
    )
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id="thread",
            run_id="before",
            mode="plan",
            messages=[{"id": "user", "role": "user", "content": "Prepare"}],
        )
    ]
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent) and terminal.outcome is not None
    assert terminal.outcome.type == "interrupt"
    return runtime, AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": terminal.outcome.interrupts[0].id,
                    "status": "cancelled",
                }
            ]
        }
    )


def _disk_saver(path: Path) -> InMemorySaver:
    filenames = iter(path / f"{name}.pickle" for name in ("storage", "writes", "blobs"))

    class DiskDictionary(PersistentDict):
        def __init__(self, factory: Callable[[], object] | None = None) -> None:
            filename = next(filenames)
            super().__init__(factory, filename=str(filename))
            if filename.exists():
                self.load()

    return InMemorySaver(factory=DiskDictionary)


async def test_completed_cancellation_reopens_durable_stores_without_building_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    saver = await asyncio.to_thread(_disk_saver, tmp_path)
    try:
        tracer = Tracer(store=SqlAlchemyTraceStore(engine))
        runtime, request = await _pending_question(saver, tracer)
        first = runtime.open_agui_run(
            thread_id="thread", run_id="cancel", resume=request
        )
        assert isinstance([event async for event in first][-1], RunErrorEvent)
        assert first.error is None
        second = runtime.open_agui_run(
            thread_id="thread", run_id="next-cancel", resume=request
        )
        assert isinstance([event async for event in second][-1], RunErrorEvent)
        assert second.error is None
        expected = (
            await runtime.agui.history(tracer).get("thread")
        ).snapshot.model_dump(exclude={"observed_at"})
        before = await tracer.store.snapshot(runtime.thread_identity("thread"))
        # Only this test owns the database. Drop disposable materializations to
        # verify a cold rebuild from unchanged Ledger facts after reopening.
        async with engine.begin() as connection:
            await connection.execute(delete(projection_checkpoints))
    finally:
        await asyncio.to_thread(saver.__exit__, None, None, None)
        await engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    reopened = await asyncio.to_thread(_disk_saver, tmp_path)
    try:
        tracer = Tracer(store=SqlAlchemyTraceStore(engine))
        runtime = (
            TinkerFin(checkpointer=reopened)
            .with_namespace("cancellation")
            .with_observer(tracer)
            .with_plan()
            .build(model=_ToolModel(responses=[AIMessage(content="Must not execute")]))
        )

        async def reject_graph(*args: object, **kwargs: object) -> None:
            raise AssertionError("replay must not construct a Graph")

        monkeypatch.setattr(
            "tinkerfin.deep_agent._AgentDefinition._create_run_graph", reject_graph
        )
        replay = runtime.open_agui_run(
            thread_id="thread", run_id="cancel", parent_run_id="before", resume=request
        )
        events = [event async for event in replay]
        assert replay.error is None
        assert (
            isinstance(events[-1], RunErrorEvent)
            and events[-1].code == "resume_cancelled"
        )
        after = await tracer.store.snapshot(runtime.thread_identity("thread"))
        assert after.key == before.key and after.as_of_seq == before.as_of_seq
        assert (await runtime.agui.history(tracer).get("thread")).snapshot.model_dump(
            exclude={"observed_at"}
        ) == expected
    finally:
        await asyncio.to_thread(reopened.__exit__, None, None, None)
        await engine.dispose()


class _ControlledSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.block = False
        self.written = asyncio.Event()
        self.release = asyncio.Event()

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        cancellation = task_id.startswith("tinkerfin-abandon:")
        if cancellation and self.fail:
            raise OSError("cancellation storage unavailable")
        await super().aput_writes(config, writes, task_id, task_path)
        if cancellation and self.block:
            self.written.set()
            await self.release.wait()


@pytest.mark.parametrize("cancel_after_write", [False, True])
async def test_failed_or_cancelled_save_never_reports_completed_abandonment(
    cancel_after_write: bool,
) -> None:
    saver = _ControlledSaver()
    tracer = Tracer()
    runtime, request = await _pending_question(saver, tracer)
    saver.fail = not cancel_after_write
    saver.block = cancel_after_write
    stream = runtime.open_agui_run(thread_id="thread", run_id="cancel", resume=request)

    async def collect() -> list[object]:
        return [event async for event in stream]

    if cancel_after_write:
        task = asyncio.create_task(collect())
        try:
            await saver.written.wait()
            task.cancel()
            saver.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            saver.release.set()
            await asyncio.gather(task, return_exceptions=True)
    else:
        events = await collect()
        assert (
            isinstance(events[-1], RunErrorEvent)
            and events[-1].code != "resume_cancelled"
        )
        assert stream.error is not None
    before = await tracer.store.snapshot(runtime.thread_identity("thread"))
    saver.fail = saver.block = False
    repeated = runtime.open_agui_run(
        thread_id="thread", run_id="cancel", resume=request
    )
    events = [event async for event in repeated]
    assert repeated.error is not None
    assert (
        isinstance(events[-1], RunErrorEvent) and events[-1].code != "resume_cancelled"
    )
    assert (
        await tracer.store.snapshot(runtime.thread_identity("thread"))
    ).as_of_seq == before.as_of_seq


class _ControlledObserver:
    def __init__(self, phase: str = "") -> None:
        self.phase = phase
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        if context.identity.run_id == "cancel" and self.phase == "open":
            raise RuntimeError("observer open unavailable")
        owner = self

        class Session:
            def __init__(self) -> None:
                self.failure: asyncio.Future[BaseException] = (
                    asyncio.get_running_loop().create_future()
                )

            async def observe(self, observation: RuntimeObservation) -> None:
                if context.identity.run_id != "cancel":
                    return
                if (
                    observation.kind == "run.input"
                    and owner.block
                    and context.input_kind == "abandon"
                    and not context.resume
                ):
                    owner.entered.set()
                    await owner.release.wait()
                if observation.kind == owner.phase:
                    raise RuntimeError("observer unavailable")

            async def force(self, boundary: ObservationBoundary) -> None:
                pass

            def failure_waiter(self) -> asyncio.Future[BaseException]:
                return self.failure

            async def aclose(self) -> None:
                self.failure.cancel()
                if context.identity.run_id == "cancel" and owner.phase == "close":
                    raise RuntimeError("observer close unavailable")

        return Session()


@pytest.mark.parametrize(
    "phase", ["open", "run.started", "run.input", "run.terminal", "run.closed", "close"]
)
async def test_replay_preserves_current_observer_failures(phase: str) -> None:
    tracer = Tracer()
    observer = _ControlledObserver()
    runtime, request = await _pending_question(InMemorySaver(), tracer, observer)
    first = runtime.open_agui_run(thread_id="thread", run_id="cancel", resume=request)
    assert isinstance([event async for event in first][-1], RunErrorEvent)
    assert first.error is None
    before = await tracer.store.snapshot(runtime.thread_identity("thread"))
    observer.phase = phase
    replay = runtime.open_agui_run(thread_id="thread", run_id="cancel", resume=request)
    events = [event async for event in replay]
    assert replay.error is not None
    assert (
        isinstance(events[-1], RunErrorEvent) and events[-1].code != "resume_cancelled"
    )
    assert (
        await tracer.store.snapshot(runtime.thread_identity("thread"))
    ).as_of_seq == before.as_of_seq


async def test_active_cancellation_writer_cannot_be_replayed() -> None:
    tracer = Tracer()
    observer = _ControlledObserver()
    runtime, request = await _pending_question(InMemorySaver(), tracer, observer)
    observer.block = True
    stream = runtime.open_agui_run(thread_id="thread", run_id="cancel", resume=request)

    async def collect() -> list[object]:
        return [event async for event in stream]

    task = asyncio.create_task(collect())
    try:
        await observer.entered.wait()
        other = runtime.open_agui_run(
            thread_id="thread", run_id="cancel", resume=request
        )
        events = [event async for event in other]
        assert other.error is not None
        assert (
            isinstance(events[-1], RunErrorEvent)
            and events[-1].code != "resume_cancelled"
        )
    finally:
        observer.release.set()
        await task
    assert stream.error is None


async def test_closed_trace_without_checkpoint_evidence_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = Tracer()
    runtime, request = await _pending_question(InMemorySaver(), tracer)

    async def no_checkpoint_fact(*args: object, **kwargs: object) -> None:
        pass

    with monkeypatch.context() as patch:
        patch.setattr(
            "tinkerfin._agui_cancellation.save_cancellation", no_checkpoint_fact
        )
        original = runtime.open_agui_run(
            thread_id="thread", run_id="cancel", resume=request
        )
        events = [event async for event in original]
        assert (
            isinstance(events[-1], RunErrorEvent)
            and events[-1].code == "resume_cancelled"
        )
    before = await tracer.store.snapshot(runtime.thread_identity("thread"))
    retry = runtime.open_agui_run(thread_id="thread", run_id="cancel", resume=request)
    events = [event async for event in retry]
    assert retry.error is not None
    assert (
        isinstance(events[-1], RunErrorEvent) and events[-1].code != "resume_cancelled"
    )
    assert (
        await tracer.store.snapshot(runtime.thread_identity("thread"))
    ).as_of_seq == before.as_of_seq
    new = runtime.open_agui_run(thread_id="thread", run_id="new-cancel", resume=request)
    events = [event async for event in new]
    assert new.error is None
    assert (
        isinstance(events[-1], RunErrorEvent) and events[-1].code == "resume_cancelled"
    )
    assert (
        await runtime.agui.history(tracer).get("thread")
    ).snapshot.available_heads == ("new-cancel",)
