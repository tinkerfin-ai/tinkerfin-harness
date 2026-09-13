"""Admission, source context, and cancellation through the public Runtime API."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from types import ModuleType
from typing import Literal

import pytest
from deepagents.backends import StateBackend
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from redis.asyncio import Redis
from test_runtime_api import _open
from test_runtime_observation import _ControlSession, _Observer

import tinkerfin.redis._lease_lock as lease_module
from tinkerfin import TinkerFin
from tinkerfin.coordination import RunCoordinationOwnershipLostError, RunCoordinator
from tinkerfin.redis import RedisRunCoordinator
from tinkerfin_contracts import PreparedWorkspace, RunIdentity, RunTerminalObservation


class _LeaseClock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.waiting = asyncio.Event()
        self.tick = asyncio.Event()
        controlled = ModuleType("controlled_asyncio")
        controlled.__dict__.update(vars(asyncio))
        setattr(controlled, "sleep", self.sleep)
        monkeypatch.setattr(lease_module, "asyncio", controlled)

    async def sleep(self, seconds: float) -> None:
        self.waiting.set()
        await self.tick.wait()
        self.tick.clear()


class _LeaseRedis(Redis):
    """Execute the real lease lifecycle with a signal-controlled failed renewal."""

    def __init__(self) -> None:
        self.owner: object | None = None
        self.renewed = asyncio.Event()
        self.renewal_task: asyncio.Task[object] | None = None
        self.release_calls = 0

    async def ping(self, **kwargs: object) -> bool:
        return True

    async def eval(self, script: str, numkeys: int, *args: object) -> int:  # pyright: ignore[reportIncompatibleMethodOverride] - redis-py annotates Lua replies as str, but integer Lua replies are int
        if numkeys == 2:
            self.owner = args[2]
            return 1
        if "PEXPIRE" in script:
            self.owner = None
            self.renewal_task = asyncio.current_task()
            self.renewed.set()
            return 0
        assert "DEL" in script
        self.release_calls += 1
        previous = self.owner
        self.owner = None
        return int(previous == args[1])

    async def aclose(self, close_connection_pool: bool | None = None) -> None:
        raise AssertionError("The Runtime must not close a borrowed Redis client")


def _builder(
    graph: object,
    monkeypatch: pytest.MonkeyPatch,
    coordinator: RunCoordinator | None = None,
) -> TinkerFin:
    def create(*args: object, **kwargs: object) -> object:
        return graph

    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", create)
    return TinkerFin(run_coordinator=coordinator).with_namespace("company")


async def _lose_lease(clock: _LeaseClock, client: _LeaseRedis) -> None:
    await clock.waiting.wait()
    clock.tick.set()
    await client.renewed.wait()
    assert client.renewal_task is not None
    await asyncio.shield(client.renewal_task)


@pytest.mark.parametrize("protocol", ["native", "agui"])
@pytest.mark.parametrize("observer", [False, True])
@pytest.mark.parametrize("external_close", [False, True])
async def test_lease_loss_cancels_graph_and_preserves_failure_through_close(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Literal["native", "agui"],
    observer: bool,
    external_close: bool,
) -> None:
    clock, client = _LeaseClock(monkeypatch), _LeaseRedis()
    coordinator = RedisRunCoordinator.from_client(client)
    entered, work, cleanup_started, cleanup_release = (
        asyncio.Event() for _ in range(4)
    )
    actions: list[str] = []
    order: list[str] = []
    cancelled = cleanup_interrupted = False

    async def first(state: MessagesState) -> dict[str, object]:
        nonlocal cancelled, cleanup_interrupted
        entered.set()
        try:
            await work.wait()
            actions.append("first effect")
            return {}
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            cleanup_started.set()
            try:
                await cleanup_release.wait()
            except asyncio.CancelledError:
                cleanup_interrupted = True
                raise
            order.append("source closed")

    async def second(state: MessagesState) -> dict[str, object]:
        actions.append("next node")
        return {}

    graph = StateGraph(MessagesState)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    builder = _builder(graph.compile(), monkeypatch, coordinator)

    async def terminal(value: RunTerminalObservation) -> None:
        order.append("terminal")

    if observer:
        builder = builder.with_observer(on_terminal=terminal)
    run = _open(builder.build(model="provider:model"), protocol)

    async def consume() -> None:
        async for _ in run:
            pass

    async with coordinator:
        consumer = asyncio.create_task(consume())
        closer: asyncio.Task[None] | None = None
        outcome: BaseException | None = None
        try:
            await entered.wait()
            await _lose_lease(clock, client)
            # Renewal completion is not cancellation convergence. Keep work blocked
            # until the Graph child itself confirms cancellation and enters cleanup.
            await cleanup_started.wait()
            assert cancelled and not work.is_set() and not actions
            if external_close:
                close_started = asyncio.Event()

                async def close() -> None:
                    close_started.set()
                    await run.aclose()

                closer = asyncio.create_task(close())
                await close_started.wait()
            cleanup_release.set()
            try:
                await consumer
            except BaseException as error:  # noqa: BLE001 - assert the public failure below
                outcome = error
            if closer is not None:
                await closer
            await run.aclose()
        finally:
            cleanup_release.set()
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            if closer is not None:
                await asyncio.gather(closer, return_exceptions=True)
            await run.aclose()

    assert not actions and cancelled and not cleanup_interrupted
    assert client.release_calls == 1
    assert isinstance(run.error, RunCoordinationOwnershipLostError)
    if isinstance(outcome, asyncio.CancelledError):
        assert any(
            "RunCoordinationOwnershipLostError" in note
            for note in getattr(outcome, "__notes__", ())
        )
    if observer:
        assert order == ["source closed", "terminal"]


@pytest.mark.parametrize("protocol", ["native", "agui"])
@pytest.mark.parametrize("finish", [False, True])
async def test_coordinator_context_remains_valid_until_runtime_closes(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Literal["native", "agui"],
    finish: bool,
) -> None:
    context = ContextVar("admission_context", default="outside")
    observed: list[str] = []

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncIterator[None]:
        token = context.set(identity.namespace)
        try:
            yield
        finally:
            observed.append(context.get())
            context.reset(token)

    async def node(state: MessagesState) -> dict[str, object]:
        observed.append(context.get())
        return {}

    graph = StateGraph(MessagesState)
    graph.add_node("node", node)
    graph.add_edge(START, "node")
    graph.add_edge("node", END)
    run = _open(
        _builder(graph.compile(), monkeypatch, coordinate).build(
            model="provider:model"
        ),
        protocol,
    )
    try:
        if finish:
            async for _ in run:
                pass
        else:
            await run.messaging_owner_preflight()
    finally:
        await run.aclose()
    assert observed == ["company"] * (2 if finish else 1)
    assert context.get() == "outside" and run.error is None


class _ContextGraph:
    def __init__(self) -> None:
        self.context = ContextVar("source_context", default="outside")
        self.observed: list[str] = []
        self.closed = False

    async def astream(
        self, *args: object, **kwargs: object
    ) -> AsyncIterator[Mapping[str, object]]:
        token = self.context.set("inside")
        try:
            for index in range(2):
                self.observed.append(self.context.get())
                yield {
                    "type": "values",
                    "ns": (),
                    "data": {"counter": index},
                    "interrupts": (),
                }
        finally:
            self.context.reset(token)
            self.closed = True


setattr(
    _ContextGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


@pytest.mark.parametrize("protocol", ["native", "agui"])
@pytest.mark.parametrize("observer", [False, True])
@pytest.mark.parametrize("finish", [False, True])
async def test_source_context_is_valid_across_pulls_and_early_close(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Literal["native", "agui"],
    observer: bool,
    finish: bool,
) -> None:
    graph = _ContextGraph()
    builder = _builder(graph, monkeypatch)

    async def terminal(value: RunTerminalObservation) -> None:
        assert graph.closed

    if observer:
        builder = builder.with_observer(on_terminal=terminal)
    run = _open(builder.build(model="provider:model"), protocol)
    try:
        if finish:
            async for _ in run:
                pass
        else:
            if protocol == "agui":
                await anext(run)
            await anext(run)
    finally:
        await run.aclose()
    assert graph.closed and graph.observed
    assert all(value == "inside" for value in graph.observed)
    assert run.error is None and graph.context.get() == "outside"


@pytest.mark.parametrize("cancelled_resource", ["workspace", "coordinator"])
async def test_workspace_and_coordinator_cleanup_preserve_cancellation(
    monkeypatch: pytest.MonkeyPatch, cancelled_resource: str
) -> None:
    closed: list[str] = []

    def fail(resource: str) -> None:
        closed.append(resource)
        if resource == cancelled_resource:
            raise asyncio.CancelledError(f"{resource} cancelled")
        raise RuntimeError(f"{resource} cleanup failed")

    class Workspace:
        @asynccontextmanager
        async def prepare(
            self, identity: RunIdentity
        ) -> AsyncIterator[PreparedWorkspace[None, StateBackend]]:
            try:
                yield PreparedWorkspace(None, StateBackend())
            finally:
                fail("workspace")

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncIterator[None]:
        try:
            yield
        finally:
            fail("coordinator")

    runtime = _builder(_ContextGraph(), monkeypatch, coordinate).build(
        model="provider:model", backend=Workspace()
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        await runtime.ainvoke(thread_id="thread", run_id="run", input={"messages": []})
    assert closed == ["workspace", "coordinator"]
    assert any("failed" in note for note in getattr(caught.value, "__notes__", ()))


class _ProcessControl(BaseException):
    """Exercise process-control propagation without stopping the test process."""


class _ClosingIterator:
    """Expose explicit same-Task close after natural exhaustion or a pull failure."""

    def __init__(
        self,
        *,
        pull_failure: BaseException | None = None,
        close_failure: BaseException | None = None,
    ) -> None:
        self.pull_failure = pull_failure
        self.close_failure = close_failure
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.context = ContextVar("closing_source_context", default="outside")
        self.token: Token[str] | None = None
        self.close_calls = 0
        self.closed = False
        self.interrupted = False

    def __aiter__(self) -> _ClosingIterator:
        return self

    async def __anext__(self) -> Mapping[str, object]:
        if self.token is None:
            self.token = self.context.set("inside")
            return {
                "type": "values",
                "ns": (),
                "data": {"counter": 1},
                "interrupts": (),
            }
        if self.pull_failure is not None:
            raise self.pull_failure
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.interrupted = True
            raise
        finally:
            assert self.token is not None and self.context.get() == "inside"
            self.context.reset(self.token)
            self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


class _ClosingGraph:
    def __init__(self, source: _ClosingIterator) -> None:
        self.source = source

    def astream(self, *args: object, **kwargs: object) -> _ClosingIterator:
        return self.source


setattr(
    _ClosingGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


@pytest.mark.parametrize("protocol", ["native", "agui"])
@pytest.mark.parametrize("control", [False, True])
async def test_explicit_iterator_close_retains_pull_and_cleanup_causes(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Literal["native", "agui"],
    control: bool,
) -> None:
    pull = RuntimeError("source pull failed")
    close = (_ProcessControl if control else RuntimeError)("source close failed")
    cause = OSError("source close original cause")
    close.__cause__ = cause
    source = _ClosingIterator(pull_failure=pull, close_failure=close)
    source.release_close.set()
    run = _open(
        _builder(_ClosingGraph(source), monkeypatch).build(model="provider:model"),
        protocol,
    )
    errors: list[BaseException] = []
    try:
        async for _ in run:
            pass
    except BaseException as error:  # noqa: BLE001 - check both control and ordinary failures
        errors.append(error)
    finally:
        try:
            await run.aclose()
        except BaseException as error:  # noqa: BLE001 - include retained close evidence
            errors.append(error)
    if run.error is not None:
        errors.append(run.error)
    assert source.closed and source.close_calls == 1 and not source.interrupted
    assert {id(pull), id(close), id(cause)} <= set().union(
        *(_failure_objects(error) for error in errors)
    )
    if control:
        assert any(error is close for error in errors)


@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_lease_loss_before_resource_release_downgrades_success(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Literal["native", "agui"],
) -> None:
    clock, client = _LeaseClock(monkeypatch), _LeaseRedis()
    coordinator = RedisRunCoordinator.from_client(client)
    source = _ClosingIterator()
    terminals: list[RunTerminalObservation] = []

    async def terminal(value: RunTerminalObservation) -> None:
        assert source.closed and client.release_calls == 1
        terminals.append(value)

    run = _open(
        _builder(_ClosingGraph(source), monkeypatch, coordinator)
        .with_observer(on_terminal=terminal)
        .build(model="provider:model"),
        protocol,
    )
    errors: list[BaseException] = []

    async def consume() -> None:
        try:
            async for _ in run:
                pass
        except BaseException as error:  # noqa: BLE001 - observe the public stop outcome
            errors.append(error)

    async with coordinator:
        consumer = asyncio.create_task(consume())
        try:
            await source.close_started.wait()
            await _lose_lease(clock, client)
            source.release_close.set()
            await consumer
        finally:
            source.release_close.set()
            await asyncio.gather(consumer, return_exceptions=True)
            try:
                await run.aclose()
            except BaseException as error:  # noqa: BLE001 - retain idempotent cleanup failure
                errors.append(error)

    assert source.closed and not source.interrupted and source.close_calls == 1
    assert client.release_calls == 1
    assert [value.outcome for value in terminals] == ["failed"]
    assert isinstance(run.error, RunCoordinationOwnershipLostError)
    if protocol == "native":
        assert any(
            isinstance(error, RunCoordinationOwnershipLostError) for error in errors
        )


def _failure_objects(
    error: BaseException, ancestry: frozenset[int] = frozenset()
) -> set[int]:
    assert id(error) not in ancestry, "Run failures must not form a causal cycle"
    ancestry = ancestry | {id(error)}
    found = {id(error)}
    children = [error.__cause__, error.__context__]
    if isinstance(error, BaseExceptionGroup):
        children.extend(error.exceptions)
    for child in children:
        if child is not None:
            found.update(_failure_objects(child, ancestry))
    return found


@pytest.mark.parametrize("protocol", ["native", "agui"])
@pytest.mark.parametrize("source_kind", ["ordinary", "cancel", "control"])
@pytest.mark.parametrize("coordinator_control", [False, True])
async def test_all_resource_failures_keep_original_causes_without_cycles(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Literal["native", "agui"],
    source_kind: str,
    coordinator_control: bool,
) -> None:
    source_fault = {
        "ordinary": OSError("source failed"),
        "cancel": asyncio.CancelledError("source cancelled"),
        "control": _ProcessControl("source control"),
    }[source_kind]
    source_cause = LookupError("source original cause")
    source_fault.__cause__ = source_cause
    workspace_fault = OSError("workspace failed")
    coordinator_fault = (
        _ProcessControl("coordinator control")
        if coordinator_control
        else OSError("coordinator failed")
    )
    closed: list[str] = []

    class Source(_ContextGraph):
        async def astream(
            self, *args: object, **kwargs: object
        ) -> AsyncIterator[Mapping[str, object]]:
            try:
                async for part in super().astream():
                    yield part
            finally:
                closed.append("source")
                raise source_fault

    setattr(
        Source.astream, "__signature__", inspect.signature(CompiledStateGraph.astream)
    )

    class Workspace:
        @asynccontextmanager
        async def prepare(
            self, identity: RunIdentity
        ) -> AsyncIterator[PreparedWorkspace[None, StateBackend]]:
            try:
                yield PreparedWorkspace(None, StateBackend())
            finally:
                closed.append("workspace")
                raise workspace_fault

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncIterator[None]:
        try:
            yield
        finally:
            closed.append("coordinator")
            raise coordinator_fault

    run = _open(
        _builder(Source(), monkeypatch, coordinate).build(
            model="provider:model", backend=Workspace()
        ),
        protocol,
    )
    failures: list[BaseException] = []
    try:
        async for _ in run:
            pass
    except BaseException as error:  # noqa: BLE001 - assert ordinary and control failures below
        failures.append(error)
    finally:
        try:
            await run.aclose()
        except BaseException as error:  # noqa: BLE001 - verify repeat close retains the same evidence
            failures.append(error)
    if run.error is not None:
        failures.append(run.error)
    assert failures and closed == ["source", "workspace", "coordinator"]
    found = set().union(*(_failure_objects(error) for error in failures))
    assert {
        id(source_fault),
        id(source_cause),
        id(workspace_fault),
        id(coordinator_fault),
    } <= found
    if source_kind == "control" or coordinator_control:
        assert any(isinstance(error, _ProcessControl) for error in failures)
    elif source_kind == "cancel":
        assert any(isinstance(error, asyncio.CancelledError) for error in failures)


@pytest.mark.parametrize("protocol", ["native", "agui"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_observer_cleanup_retains_control_and_other_original_causes(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Literal["native", "agui"],
    reverse: bool,
) -> None:
    control = _ProcessControl("observer control")
    original = OSError("observer original cause")
    wrapped = RuntimeError("observer cleanup failed")
    wrapped.__cause__ = original
    sessions = [
        _ControlSession(phase="close", error=control),
        _ControlSession(phase="close", error=wrapped),
    ]
    if reverse:
        sessions.reverse()
    builder = _builder(_ContextGraph(), monkeypatch)
    for session in sessions:
        builder = builder.with_observer(_Observer(session))
    run = _open(builder.build(model="provider:model"), protocol)
    with pytest.raises(_ProcessControl) as caught:
        try:
            async for _ in run:
                pass
        finally:
            await run.aclose()
    assert {id(control), id(original), id(wrapped)} <= _failure_objects(caught.value)
    assert all(session.closed == 1 for session in sessions)
