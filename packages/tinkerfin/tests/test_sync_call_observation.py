"""Managed synchronous Tool calls must settle without crossing event loops."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from tinkerfin import (
    RunIdentity,
    RunObservationError,
    TinkerFin,
    trace_contribution,
)
from tinkerfin.runtime_profile import (
    DeepAgentsV2RuntimeProfile,
    DeepAgentsV3RuntimeProfile,
)
from tinkerfin.subagents import SubAgent
from tinkerfin_contracts import (
    ContextContributionObservation,
    ObservationBoundary,
    RunSourceContext,
    RuntimeObservation,
    ToolExecutionObservation,
)
from tinkerfin_tracing import Tracer


class _ToolModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        return self


class _Session:
    def __init__(self, *, fail_start: bool) -> None:
        self.fail_start = fail_start
        self.phases: list[str] = []
        self.call_ids: list[str] = []
        self.namespaces: list[tuple[str, ...]] = []
        self.contribution_parents: list[str | None] = []
        self.closed = False
        self.failure: asyncio.Future[BaseException] = (
            asyncio.get_running_loop().create_future()
        )

    async def observe(self, observation: RuntimeObservation) -> None:
        if isinstance(observation, ToolExecutionObservation):
            if self.fail_start and observation.phase == "started":
                raise RuntimeError("tool observation unavailable")
            if observation.tool_name.startswith("echo_"):
                self.phases.append(observation.phase)
                self.call_ids.append(observation.execution_id)
                self.namespaces.append(observation.graph_namespace)
        if isinstance(observation, ContextContributionObservation):
            self.contribution_parents.append(observation.parent_call_id)

    async def force(self, boundary: ObservationBoundary) -> None:
        return None

    def failure_waiter(self) -> Awaitable[BaseException]:
        return self.failure

    async def aclose(self) -> None:
        self.closed = True


class _Observer:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def open_run(self, context: RunSourceContext) -> _Session:
        return self.session


async def _case(profile_name: str, scenario: str) -> dict[str, object]:
    calls: list[str] = []
    entered = asyncio.Event()
    release = threading.Event()
    parallel = threading.Barrier(2, timeout=3)
    loop = asyncio.get_running_loop()

    @tool
    def echo_sync(value: str) -> str:
        """Echo a value after recording that execution actually began."""
        calls.append(value)
        loop.call_soon_threadsafe(entered.set)
        if scenario == "cancel" and not release.wait(timeout=5):
            raise TimeoutError("test Tool was not released")
        if scenario == "parallel":
            parallel.wait()
        if scenario == "scope":

            async def contribute() -> None:
                async with trace_contribution(kind="custom", name="echo service"):
                    pass

            asyncio.run_coroutine_threadsafe(contribute(), loop).result(timeout=2)
        return value

    @tool
    async def echo_async(value: str) -> str:
        """Echo a value through the asynchronous control case."""
        calls.append(value)
        return value

    selected = echo_async if scenario == "async" else echo_sync
    model = _ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-echo", "name": selected.name, "args": {"value": "one"}}
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    if scenario == "parallel":
        model.responses[0] = AIMessage(
            content="",
            tool_calls=[
                {"id": f"call-{value}", "name": selected.name, "args": {"value": value}}
                for value in ("one", "two")
            ],
        )
    session = _Session(fail_start=scenario == "failure")
    runtime = TinkerFin(
        runtime_profile=DeepAgentsV2RuntimeProfile()
        if profile_name == "v2"
        else DeepAgentsV3RuntimeProfile()
    ).with_namespace("test")
    tracer = Tracer()
    if scenario != "no_observer":
        runtime = runtime.with_observer(tracer).with_observer(_Observer(session))
    if scenario == "subagent":
        subagent: SubAgent = {
            "name": "worker",
            "description": "Echo a value",
            "system_prompt": "Use the echo Tool.",
            "model": model,
            "tools": [selected],
        }
        parent = _ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "delegate",
                            "name": "task",
                            "args": {
                                "description": "Echo one",
                                "subagent_type": "worker",
                            },
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
        agent = runtime.build(model=parent, tools=[], subagents=[subagent])
    else:
        agent = runtime.build(model=model, tools=[selected])

    async def invoke() -> None:
        await agent.ainvoke(
            thread_id=RunIdentity(
                namespace="test",
                thread_id="thread-sync-callback",
                run_id="run-sync-callback",
            ).thread_id,
            run_id=RunIdentity(
                namespace="test",
                thread_id="thread-sync-callback",
                run_id="run-sync-callback",
            ).run_id,
            input={"messages": [HumanMessage(content="echo")]},
        )

    task = asyncio.create_task(invoke())
    outcome = "success"
    try:
        if scenario == "cancel":
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel("cancel synchronous Tool")
        await task
    except RunObservationError:
        outcome = "observer_failed"
    except asyncio.CancelledError:
        outcome = "cancelled"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return {
        "calls": calls,
        "phases": session.phases,
        "outcome": outcome,
        "closed": session.closed,
        "call_ids": session.call_ids,
        "namespaces": session.namespaces,
        "contribution_parents": session.contribution_parents,
    }


_SCENARIOS = (
    "sync",
    "async",
    "no_observer",
    "failure",
    "cancel",
    "parallel",
    "subagent",
    "scope",
)


@pytest.mark.parametrize("profile", ("v2", "v3"))
async def test_sync_tool_callback_lifecycle_in_an_isolated_process(
    profile: str,
) -> None:
    # Each scenario gets a fresh Runner, including executor shutdown. Sharing only
    # imports also verifies that completed runs leave subsequent runs usable.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        profile,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "LANGSMITH_TRACING": "false"},
    )
    assert process.stdout is not None and process.stderr is not None
    stderr = asyncio.create_task(process.stderr.read())
    try:
        for scenario in _SCENARIOS:
            # Keep each scenario's watchdog independent of imports and prior cases.
            # A result is sent only after its Runner and executor have shut down.
            line = await asyncio.wait_for(process.stdout.readline(), timeout=8)
            assert line, (await stderr).decode()
            name, result = json.loads(line)
            assert name == scenario
            _assert_result(scenario, result)
        await asyncio.wait_for(process.wait(), timeout=8)
        assert process.returncode == 0, (await stderr).decode()
        assert await process.stdout.read() == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await stderr


def _assert_result(scenario: str, result: dict[str, Any]) -> None:
    if scenario == "failure":
        assert result["outcome"] == "observer_failed"
        assert result["calls"] == []
    elif scenario == "cancel":
        assert result["outcome"] == "cancelled"
        assert result["phases"] == ["started", "cancelled"]
    else:
        assert result["outcome"] == "success"
        assert sorted(result["calls"]) == (
            ["one", "two"] if scenario == "parallel" else ["one"]
        )
        if scenario != "no_observer":
            for call_id in set(result["call_ids"]):
                assert [
                    phase
                    for phase, identity in zip(
                        result["phases"], result["call_ids"], strict=True
                    )
                    if identity == call_id
                ] == ["started", "completed"]
            assert len(set(result["call_ids"])) == (2 if scenario == "parallel" else 1)
        if scenario == "subagent":
            assert all(result["namespaces"])
        if scenario == "scope":
            assert result["contribution_parents"] == [result["call_ids"][0]] * 2
    if scenario != "no_observer":
        assert result["closed"] is True


if __name__ == "__main__":
    for scenario in _SCENARIOS:
        result = asyncio.run(_case(sys.argv[1], scenario))
        print(json.dumps([scenario, result]), flush=True)
