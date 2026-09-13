"""Preparation outcomes stay owned when cancellation interrupts their delivery."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Literal

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

import tinkerfin._tasks as task_module
from tinkerfin import TinkerFin
from tinkerfin_contracts import PreparedWorkspace, RunIdentity


class _Control(BaseException):
    pass


def _contains(error: BaseException | None, target: BaseException) -> bool:
    pending = [] if error is None else [error]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if item is target:
            return True
        if id(item) in seen:
            continue
        seen.add(id(item))
        pending.extend(
            value for value in (item.__cause__, item.__context__) if value is not None
        )
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
    return False


@pytest.mark.parametrize("protocol", ["native", "agui"])
@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("outcome", ["cancel", "ordinary", "control", "success"])
async def test_preparation_result_is_received_before_cancelled_caller_exits(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Literal["native", "agui"],
    repeat: bool,
    outcome: Literal["cancel", "ordinary", "control", "success"],
) -> None:
    entered, closing, release, repeated = (asyncio.Event() for _ in range(4))
    completed: list[int] = []
    failure = (
        RuntimeError("preparation failed")
        if outcome == "ordinary"
        else _Control("preparation control")
        if outcome == "control"
        else None
    )
    cause = OSError("preparation original cause")
    if failure is not None:
        failure.__cause__ = cause
    graph = StateGraph(MessagesState)
    graph.add_node("reply", lambda state: {"messages": [AIMessage(content="healthy")]})
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    compiled = graph.compile()

    class Workspace:
        @asynccontextmanager
        async def prepare(
            self, identity: RunIdentity
        ) -> AsyncIterator[PreparedWorkspace[None, StateBackend]]:
            try:
                entered.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                closing.set()
                await release.wait()
                owner = asyncio.current_task()
                assert owner is not None
                completed.append(owner.cancelling())
                if failure is not None:
                    raise failure
                if outcome == "success":
                    yield PreparedWorkspace(None, StateBackend())
                    return
                raise
            raise AssertionError("preparation resumed without cancellation")

    monkeypatch.setattr(
        "tinkerfin.deep_agent.create_agent_graph", lambda *args, **kwargs: compiled
    )
    runtime = (
        TinkerFin()
        .with_namespace("test")
        .build(model="provider:model", backend=Workspace())
    )
    run = (runtime.open_run if protocol == "native" else runtime.open_agui_run)(
        thread_id="preparation", run_id="cancelled", input={"messages": []}
    )
    pending: asyncio.Task[None] | None = None
    real_wait = asyncio.wait
    controlled = ModuleType("controlled_task_wait")
    controlled.__dict__.update(vars(asyncio))

    async def wait(*args, **kwargs):
        try:
            return await real_wait(*args, **kwargs)
        except asyncio.CancelledError:
            if asyncio.current_task() is pending:
                repeated.set()
            raise

    setattr(controlled, "wait", wait)
    monkeypatch.setattr(task_module, "asyncio", controlled)
    pending = asyncio.create_task(run.messaging_owner_preflight())
    primary: BaseException | None = None
    close_failure: BaseException | None = None
    try:
        await entered.wait()
        pending.cancel("first caller cancellation")
        await closing.wait()
        if repeat:
            pending.cancel("second caller cancellation")
            await repeated.wait()
        release.set()
        try:
            await pending
        except BaseException as error:  # noqa: BLE001 - the test owns process-control delivery
            primary = error
        try:
            await run.aclose()
        except BaseException as error:  # noqa: BLE001 - inspect only actually delivered failures
            close_failure = error
        assert completed == [1]
        if outcome == "control":
            assert primary is failure
        else:
            assert isinstance(primary, asyncio.CancelledError)
        if failure is None:
            assert close_failure is None
        else:
            assert _contains(primary, failure)
            assert _contains(primary, cause)
            if close_failure is not None:
                assert _contains(close_failure, failure)
        if outcome == "ordinary":
            assert run.error is failure
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
