"""Public cancellation-safe joins for host-owned runtime tasks."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Literal

import pytest
from langchain.agents.middleware.types import InputAgentState
from langchain_core.runnables import RunnableConfig

from tinkerfin import (
    AgentRuntime,
    RunIdentity,
    RunObservationError,
    TinkerFin,
)
from tinkerfin._tasks import join_task
from tinkerfin_contracts import (
    NativeStateObservation,
    ObservationBoundary,
    RunObservationSession,
    RunSourceContext,
    RunTerminalObservation,
    RuntimeObservation,
)


async def test_join_task_returns_the_owned_task_result() -> None:
    """Hosts receive the result after the owned task settles normally."""

    async def produce() -> str:
        await asyncio.sleep(0)
        return "settled"

    assert await join_task(asyncio.create_task(produce())) == "settled"


async def test_join_task_preserves_repeated_caller_cancellation() -> None:
    """Repeated caller cancellation cannot interrupt owned task settlement."""

    entered = asyncio.Event()
    release = asyncio.Event()

    async def settle() -> None:
        entered.set()
        await release.wait()

    owned = asyncio.create_task(settle())
    waiter = asyncio.create_task(join_task(owned))
    await asyncio.wait_for(entered.wait(), timeout=2)

    waiter.cancel("first cancellation")
    await asyncio.sleep(0)
    waiter.cancel("second cancellation")
    await asyncio.sleep(0)
    assert not waiter.done()
    assert not owned.cancelled()

    release.set()
    with pytest.raises(asyncio.CancelledError, match="first cancellation"):
        await waiter
    assert owned.done()
    assert owned.exception() is None


async def test_join_task_distinguishes_owned_task_cancellation() -> None:
    """Owned task cancellation propagates without marking the joining caller."""

    async def cancelled() -> None:
        raise asyncio.CancelledError("owned cancellation")

    current = asyncio.current_task()
    assert current is not None
    before = current.cancelling()
    with pytest.raises(asyncio.CancelledError, match="owned cancellation"):
        await join_task(asyncio.create_task(cancelled()))
    assert current.cancelling() == before


@pytest.mark.parametrize("outcome", ["result", "cancelled", "failed"])
@pytest.mark.parametrize("pending_at_entry", [False, True])
@pytest.mark.parametrize("repeat", [False, True])
async def test_join_task_settles_owned_outcome_before_caller_cancellation(
    outcome: Literal["result", "cancelled", "failed"],
    pending_at_entry: bool,
    repeat: bool,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    failure = RuntimeError("owned settlement failed")

    async def settle() -> str:
        await release.wait()
        if outcome == "cancelled":
            raise asyncio.CancelledError("owned cancellation")
        if outcome == "failed":
            raise failure
        return "settled"

    owned = asyncio.create_task(settle())

    async def joining() -> str | None:
        entered.set()
        if pending_at_entry:
            current = asyncio.current_task()
            assert current is not None
            current.cancel("first caller cancellation")
        return await join_task(owned)

    caller = asyncio.create_task(joining())
    try:
        await entered.wait()
        if not pending_at_entry:
            caller.cancel("first caller cancellation")
        await asyncio.sleep(0)
        if repeat:
            caller.cancel("second caller cancellation")
            await asyncio.sleep(0)
        assert not caller.done()
        assert not owned.done()
        release.set()
        with pytest.raises(
            asyncio.CancelledError, match="first caller cancellation"
        ) as captured:
            await caller
        assert caller.cancelled()
        assert owned.done()
        if outcome == "failed":
            assert owned.exception() is failure
            assert any(
                "RuntimeError: owned settlement failed" in note
                for note in captured.value.__notes__
            )
        elif outcome == "cancelled":
            assert owned.cancelled()
        else:
            assert owned.result() == "settled"
    finally:
        release.set()
        await asyncio.gather(caller, owned, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["result", "cancelled", "failed"])
@pytest.mark.parametrize("pending_at_entry", [False, True])
@pytest.mark.parametrize("suppress_owned", [False, True])
async def test_join_task_handles_an_already_completed_owned_task(
    outcome: Literal["result", "cancelled", "failed"],
    pending_at_entry: bool,
    suppress_owned: bool,
) -> None:
    failure = RuntimeError("completed task failed")

    async def complete() -> str:
        if outcome == "failed":
            raise failure
        if outcome == "cancelled":
            raise asyncio.CancelledError("completed task cancelled")
        return "completed"

    owned = asyncio.create_task(complete())
    await asyncio.gather(owned, return_exceptions=True)

    async def joining() -> str | None:
        if pending_at_entry:
            current = asyncio.current_task()
            assert current is not None
            current.cancel("pending caller cancellation")
        return await join_task(owned, suppress_task_cancellation=suppress_owned)

    caller = asyncio.create_task(joining())
    if pending_at_entry:
        with pytest.raises(asyncio.CancelledError, match="pending caller cancellation"):
            await caller
        assert caller.cancelled()
    elif outcome == "failed":
        with pytest.raises(RuntimeError) as captured:
            await caller
        assert captured.value is failure
    elif outcome == "cancelled" and not suppress_owned:
        with pytest.raises(asyncio.CancelledError, match="completed task cancelled"):
            await caller
    else:
        assert await caller == ("completed" if outcome == "result" else None)


@pytest.mark.parametrize("suppress_owned", [False, True])
async def test_join_task_explicit_owned_cancellation_waits_for_cleanup(
    suppress_owned: bool,
) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def owned_work() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    owned = asyncio.create_task(owned_work())
    await entered.wait()
    if suppress_owned:
        assert (
            await join_task(owned, cancel=True, suppress_task_cancellation=True) is None
        )
    else:
        with pytest.raises(asyncio.CancelledError):
            await join_task(owned, cancel=True)
    assert owned.cancelled()
    assert closed.is_set()


async def test_join_task_does_not_repeat_an_owned_task_cancellation() -> None:
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    async def owned_work() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            closed.set()

    owned = asyncio.create_task(owned_work())
    await entered.wait()
    owned.cancel("existing cancellation")
    await cleaning.wait()
    joined = asyncio.create_task(join_task(owned, cancel=True))
    try:
        await asyncio.sleep(0)
        assert owned.cancelling() == 1
        assert not joined.done()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="existing cancellation"):
            await joined
        assert closed.is_set()
    finally:
        release.set()
        await asyncio.gather(joined, owned, return_exceptions=True)


class _ProcessControl(BaseException):
    pass


async def test_join_task_does_not_hide_process_control_behind_caller_cancellation() -> (
    None
):
    release = asyncio.Event()
    control = _ProcessControl("owned process control")

    async def controlled() -> None:
        await release.wait()
        raise control

    owned = asyncio.create_task(controlled())
    caller = asyncio.create_task(join_task(owned))
    await asyncio.sleep(0)
    caller.cancel("caller cancellation")
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(_ProcessControl) as captured:
        await caller
    assert captured.value is control
    assert owned.done()
    assert owned.exception() is control


@pytest.mark.parametrize("control_name", ["KeyboardInterrupt", "SystemExit"])
async def test_join_task_preserves_process_control_in_an_isolated_runner(
    control_name: str,
) -> None:
    code = """
import asyncio
import sys
from tinkerfin._tasks import join_task
control_type = {"KeyboardInterrupt": KeyboardInterrupt, "SystemExit": SystemExit}[sys.argv[1]]
async def main():
    release = asyncio.Event()
    async def controlled():
        await release.wait()
        raise control_type("owned process control")
    owned = asyncio.create_task(controlled())
    async def joining():
        try:
            await join_task(owned)
        except BaseException as error:
            print("JOIN=" + type(error).__name__, flush=True)
            raise
    caller = asyncio.create_task(joining())
    await asyncio.sleep(0)
    caller.cancel("caller cancellation")
    await asyncio.sleep(0)
    release.set()
    await caller
try:
    asyncio.run(main())
except BaseException as error:
    print("RUNNER=" + type(error).__name__, flush=True)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        control_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    text = stdout.decode()
    assert f"JOIN={control_name}" in text
    assert f"RUNNER={control_name}" in text


class _RecordingObserver:
    def __init__(self) -> None:
        self.failure: asyncio.Future[BaseException] = (
            asyncio.get_running_loop().create_future()
        )
        self.observations: list[RuntimeObservation] = []
        self.closed = 0

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        del context
        return self

    async def observe(self, observation: RuntimeObservation) -> None:
        self.observations.append(observation)

    async def force(self, boundary: ObservationBoundary) -> None:
        del boundary

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        return self.failure

    async def aclose(self) -> None:
        self.closed += 1


class _ClosingGraph:
    def __init__(self, error: BaseException | None) -> None:
        self.started = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.error = error

    async def astream(
        self,
        input: InputAgentState,
        config: RunnableConfig | None = None,
        **options: object,
    ) -> AsyncIterator[Mapping[str, object]]:
        del input, config, options
        self.started.set()
        try:
            await asyncio.Event().wait()
            yield {"type": "values", "ns": (), "data": {"messages": []}}
        finally:
            self.cleaning.set()
            await self.release.wait()
            self.closed.set()
            if self.error is not None:
                raise self.error


async def test_observer_operation_cancellation_keeps_terminal_delivery_available(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    class CancellingObserver(_RecordingObserver):
        async def observe(self, observation: RuntimeObservation) -> None:
            await super().observe(observation)
            if isinstance(observation, NativeStateObservation):
                raise asyncio.CancelledError("observer operation cancelled")

    class ValuesGraph:
        async def astream(
            self,
            input: InputAgentState,
            config: RunnableConfig | None = None,
            **options: object,
        ) -> AsyncIterator[Mapping[str, object]]:
            del input, config, options
            yield {"type": "values", "ns": (), "data": {"messages": []}}

    observer = CancellingObserver()
    healthy = _RecordingObserver()
    definition = definition_factory(
        ValuesGraph(),
        tinkerfin=TinkerFin()
        .with_namespace("test")
        .with_observer(observer)
        .with_observer(healthy),
    )
    stream = definition.open_run(
        thread_id=RunIdentity(
            namespace="test", thread_id="operation-cancel", run_id="native"
        ).thread_id,
        run_id=RunIdentity(
            namespace="test", thread_id="operation-cancel", run_id="native"
        ).run_id,
        input={"messages": []},
    )
    with pytest.raises(asyncio.CancelledError, match="observer operation cancelled"):
        await anext(stream)
    assert observer.closed == healthy.closed == 1
    assert [
        item.outcome
        for item in healthy.observations
        if isinstance(item, RunTerminalObservation)
    ] == ["cancelled"]


@pytest.mark.parametrize("source_fails", [False, True])
@pytest.mark.parametrize("cleanup_kind", ["error", "cancel", "control"])
async def test_runtime_keeps_observer_close_control_visible_after_source_failure(
    definition_factory: Callable[..., AgentRuntime[None]],
    source_fails: bool,
    cleanup_kind: Literal["error", "cancel", "control"],
) -> None:
    source_error = RuntimeError("source failed")
    cleanup_error = (
        _ProcessControl("close control")
        if cleanup_kind == "control"
        else asyncio.CancelledError("close cancelled")
        if cleanup_kind == "cancel"
        else RuntimeError("close failed")
    )

    class ClosingObserver(_RecordingObserver):
        async def aclose(self) -> None:
            await super().aclose()
            raise cleanup_error

    class SourceGraph:
        async def astream(
            self,
            input: InputAgentState,
            config: RunnableConfig | None = None,
            **options: object,
        ) -> AsyncIterator[Mapping[str, object]]:
            del input, config, options
            if source_fails:
                raise source_error
            yield {"type": "values", "ns": (), "data": {"messages": []}}

    observer = ClosingObserver()
    definition = definition_factory(
        SourceGraph(),
        tinkerfin=TinkerFin().with_namespace("test").with_observer(observer),
    )
    stream = definition.open_run(
        thread_id=RunIdentity(
            namespace="test", thread_id="close-control", run_id="native"
        ).thread_id,
        run_id=RunIdentity(
            namespace="test", thread_id="close-control", run_id="native"
        ).run_id,
        input={"messages": []},
    )
    expected = (
        _ProcessControl
        if cleanup_kind == "control"
        else asyncio.CancelledError
        if cleanup_kind == "cancel"
        else RuntimeError
        if source_fails
        else RunObservationError
    )
    with pytest.raises(expected) as captured:
        async for _part in stream:
            pass
    assert observer.closed == 1
    if cleanup_kind == "control":
        assert captured.value is cleanup_error
    if source_fails and cleanup_kind != "error":
        assert any("source failed" in note for note in captured.value.__notes__)


@pytest.mark.parametrize("cancel_registered_first", [False, True])
@pytest.mark.parametrize("source_fails", [False, True])
async def test_runtime_close_control_is_independent_of_observer_order(
    definition_factory: Callable[..., AgentRuntime[None]],
    cancel_registered_first: bool,
    source_fails: bool,
) -> None:
    control = _ProcessControl("observer close control")
    cancellation = asyncio.CancelledError("observer close cancelled")

    class ClosingObserver(_RecordingObserver):
        def __init__(self, error: BaseException) -> None:
            super().__init__()
            self.error = error

        async def aclose(self) -> None:
            await super().aclose()
            raise self.error

    class SourceGraph:
        async def astream(
            self,
            input: InputAgentState,
            config: RunnableConfig | None = None,
            **options: object,
        ) -> AsyncIterator[Mapping[str, object]]:
            del input, config, options
            if source_fails:
                raise RuntimeError("source failed")
            yield {"type": "values", "ns": (), "data": {"messages": []}}

    cancel_observer = ClosingObserver(cancellation)
    control_observer = ClosingObserver(control)
    observers = (
        (cancel_observer, control_observer)
        if cancel_registered_first
        else (control_observer, cancel_observer)
    )
    definition = definition_factory(
        SourceGraph(),
        tinkerfin=TinkerFin()
        .with_namespace("test")
        .with_observer(observers[0])
        .with_observer(observers[1]),
    )
    stream = definition.open_run(
        thread_id=RunIdentity(
            namespace="test", thread_id="close-order", run_id="native"
        ).thread_id,
        run_id=RunIdentity(
            namespace="test", thread_id="close-order", run_id="native"
        ).run_id,
        input={"messages": []},
    )
    with pytest.raises(_ProcessControl) as captured:
        async for _part in stream:
            pass
    assert captured.value is control
    assert cancel_observer.closed == control_observer.closed == 1
    assert any("observer close cancelled" in note for note in control.__notes__)


@pytest.mark.parametrize("cleanup_kind", ["normal", "failed", "control"])
@pytest.mark.parametrize("cancel_count", [0, 1, 2])
async def test_runtime_preserves_cancellation_when_observer_and_source_cleanup_fail(
    definition_factory: Callable[..., AgentRuntime[None]],
    caplog: pytest.LogCaptureFixture,
    cleanup_kind: Literal["normal", "failed", "control"],
    cancel_count: int,
) -> None:
    failed = _RecordingObserver()
    healthy = _RecordingObserver()
    error = (
        _ProcessControl("source cleanup control")
        if cleanup_kind == "control"
        else RuntimeError("source cleanup failed")
        if cleanup_kind == "failed"
        else None
    )
    graph = _ClosingGraph(error)
    definition = definition_factory(
        graph,
        tinkerfin=TinkerFin()
        .with_namespace("test")
        .with_observer(failed)
        .with_observer(healthy),
    )
    stream = definition.open_run(
        thread_id=RunIdentity(
            namespace="test", thread_id="join-precedence", run_id="cleanup"
        ).thread_id,
        run_id=RunIdentity(
            namespace="test", thread_id="join-precedence", run_id="cleanup"
        ).run_id,
        input={"messages": []},
    )
    caller = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(graph.started.wait(), 2)
        failed.failure.set_result(RuntimeError("observer failed"))
        await asyncio.wait_for(graph.cleaning.wait(), 2)
        for index in range(cancel_count):
            caller.cancel(f"caller cancellation {index + 1}")
            await asyncio.sleep(0)
        graph.release.set()
        if cleanup_kind == "control":
            with pytest.raises(_ProcessControl) as controlled:
                await caller
            assert controlled.value is error
        elif cancel_count:
            with pytest.raises(
                asyncio.CancelledError, match="caller cancellation 1"
            ) as captured:
                await caller
            assert caller.cancelled()
            if cleanup_kind == "failed":
                assert any(
                    "source cleanup failed" in note for note in captured.value.__notes__
                )
        else:
            with pytest.raises(RunObservationError):
                await caller
        assert graph.closed.is_set()
        assert failed.closed == healthy.closed == 1
        assert [
            item.outcome
            for item in healthy.observations
            if isinstance(item, RunTerminalObservation)
        ] == ["cancelled" if cancel_count and cleanup_kind != "control" else "failed"]
        assert "exception in shielded future" not in caplog.text
    finally:
        graph.release.set()
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await stream.aclose()


@pytest.mark.parametrize("phase", ["idle", "active", "backpressure"])
@pytest.mark.parametrize("control_name", ["KeyboardInterrupt", "SystemExit"])
async def test_runner_shutdown_settles_runtime_observer_deliveries(
    phase: str,
    control_name: str,
) -> None:
    """Runner shutdown cannot leave receipts waiting for a cancelled worker."""

    code = """
import asyncio
import json
import runpy
import sys
from unittest.mock import patch
from tinkerfin import RunIdentity, TinkerFin, trace_contribution
from tinkerfin_contracts import ContextContributionObservation, NativeStateObservation

helpers = runpy.run_path(sys.argv[1])
phase = sys.argv[2]
control_type = {"KeyboardInterrupt": KeyboardInterrupt, "SystemExit": SystemExit}[sys.argv[3]]
state = {}

class Observer(helpers["_RecordingObserver"]):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.operation_closed = False

    async def observe(self, item):
        await super().observe(item)
        selected = ContextContributionObservation if phase == "backpressure" else NativeStateObservation
        if phase != "idle" and isinstance(item, selected) and not self.entered.is_set():
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.operation_closed = True

class Graph:
    def __init__(self):
        self.started = asyncio.Event()
        self.work = []
        self.closed = False

    async def astream(self, input, config=None, **kwargs):
        self.started.set()
        try:
            if phase == "idle":
                await asyncio.Event().wait()
            elif phase == "backpressure":
                async def contribute(index):
                    async with trace_contribution(kind="custom", name=f"operation-{index}"):
                        pass
                self.work = [asyncio.create_task(contribute(i)) for i in range(3)]
                await asyncio.gather(*self.work)
            yield {"type": "values", "ns": (), "data": {"messages": []}}
        finally:
            for task in self.work:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*self.work, return_exceptions=True)
            self.closed = True

async def main():
    observer = Observer()
    graph = Graph()
    state.update(observer=observer, graph=graph)
    factory = TinkerFin().with_namespace("test").with_observer(observer)
    with patch("tinkerfin.deep_agent.create_agent_graph", return_value=graph):
        definition = factory.build(model="provider:model", tools=[])
        stream = definition.open_run(thread_id="shutdown", run_id=phase, input={"messages": []})
        caller = asyncio.create_task(anext(stream))
        state["caller"] = caller
        await graph.started.wait()
        if phase != "idle":
            await observer.entered.wait()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        raise control_type("process shutdown")

try:
    asyncio.run(main())
except BaseException as error:
    observer = state["observer"]
    graph = state["graph"]
    print(json.dumps({"control": type(error).__name__, "sessions_closed": observer.closed,
        "source_closed": graph.closed, "caller_done": state["caller"].done(),
        "owned_done": all(task.done() for task in graph.work),
        "operation_closed": observer.operation_closed}), flush=True)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        code,
        str(Path(__file__).resolve()),
        phase,
        control_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    import json

    result = json.loads(stdout)
    assert result["control"] == control_name
    assert result["sessions_closed"] == 1
    assert result["source_closed"]
    assert result["caller_done"] and result["owned_done"]
    assert phase == "idle" or result["operation_closed"]
