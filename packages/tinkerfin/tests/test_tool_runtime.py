"""Execution-time workspace access preserves context, ownership, and tool schemas."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from deepagents import DeepAgentState
from deepagents.backends import StateBackend
from langchain.tools import ToolRuntime as NativeToolRuntime
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel
from test_agent_construction import Model, delegate

from tinkerfin import TinkerFin, TinkerFinLifecycleError
from tinkerfin.tools import ToolRuntime
from tinkerfin_contracts import PreparedWorkspace, RunIdentity


@dataclass(frozen=True)
class Context:
    customer: str


class Workspace:
    def __init__(self) -> None:
        self.opened: list[RunIdentity] = []
        self.closed: list[RunIdentity] = []

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[Path, StateBackend]]:
        self.opened.append(identity)
        try:
            yield PreparedWorkspace(Path("/files") / identity.run_id, StateBackend())
        finally:
            self.closed.append(identity)


def call(name: str, call_id: str = "call") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}])


@pytest.mark.parametrize("role", ["root", "general-purpose", "worker"])
async def test_runtime_preserves_business_context_and_injects_each_local_role(
    role: str,
) -> None:
    workspace = Workspace()
    context = Context(customer="customer")
    seen: list[ToolRuntime[Context, Path]] = []

    @tool
    async def inspect_workspace(runtime: ToolRuntime[Context, Path]) -> str:
        """Read the workspace name."""
        assert runtime.context is context
        assert runtime.workspace == Path("/files/run")
        assert runtime.identity.run_id == "run"
        assert runtime.tool_call_id == "call"
        assert runtime.tools
        assert "messages" in runtime.state
        seen.append(runtime)
        return "ready"

    schema = inspect_workspace.tool_call_schema
    assert isinstance(schema, type) and issubclass(schema, BaseModel)
    assert schema.model_json_schema().get("properties") == {}
    responses: list[BaseMessage] = [
        call("inspect_workspace"),
        AIMessage(content="ready"),
    ]
    if role != "root":
        responses = [delegate(role), *responses, AIMessage(content="done")]
    runtime = (
        TinkerFin()
        .with_namespace("example")
        .build(
            model=Model(responses=responses),
            context_schema=Context,
            tools=[inspect_workspace],
            backend=workspace,
            subagents=[
                {
                    "name": "worker",
                    "description": "Read files",
                    "system_prompt": "Read files",
                }
            ]
            if role == "worker"
            else [],
        )
    )
    assert not workspace.opened
    result = await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        context=context,
        input={"messages": [{"role": "user", "content": "Inspect"}]},
    )
    assert len(seen) == 1
    assert workspace.closed == workspace.opened
    assert "_ToolRunScope" not in str(result)
    assert "_scope" not in str(result)
    with pytest.raises(TinkerFinLifecycleError, match="closed"):
        _ = seen[0].workspace


async def test_absent_workspace_fails_clearly_but_run_identity_is_available() -> None:
    @tool
    async def inspect_workspace(runtime: ToolRuntime) -> str:
        """Read the workspace name."""
        assert runtime.identity.namespace == "example"
        with pytest.raises(TinkerFinLifecycleError, match="no configured workspace"):
            _ = runtime.workspace
        return "no files"

    runtime = (
        TinkerFin()
        .with_namespace("example")
        .build(
            model=Model(
                responses=[call("inspect_workspace"), AIMessage(content="done")]
            ),
            tools=[inspect_workspace],
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "Inspect"}]},
    )


async def test_model_arguments_cannot_replace_the_injected_runtime() -> None:
    workspace = Workspace()

    @tool
    async def inspect_workspace(runtime: ToolRuntime[None, Path]) -> str:
        """Read the workspace name."""
        assert runtime.workspace == Path("/files/run")
        return "ready"

    runtime = (
        TinkerFin()
        .with_namespace("example")
        .build(
            model=Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "call",
                                "name": "inspect_workspace",
                                "args": {"runtime": {"workspace": "/forged"}},
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            tools=[inspect_workspace],
            backend=workspace,
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "Inspect"}]},
    )
    assert workspace.closed == workspace.opened


async def test_concurrent_runs_keep_separate_workspaces_and_contexts() -> None:
    workspace = Workspace()
    entered = {name: asyncio.Event() for name in ("first", "second")}
    release = asyncio.Event()
    seen: list[tuple[str, Path, Context]] = []

    @tool
    async def inspect_workspace(runtime: ToolRuntime[Context, Path]) -> str:
        """Inspect an independent workspace."""
        name = runtime.identity.run_id
        entered[name].set()
        await release.wait()
        seen.append((name, runtime.workspace, runtime.context))
        return "ready"

    async def run(name: str) -> None:
        runtime = (
            TinkerFin()
            .with_namespace("example")
            .build(
                model=Model(
                    responses=[call("inspect_workspace"), AIMessage(content="done")]
                ),
                tools=[inspect_workspace],
                backend=workspace,
                context_schema=Context,
            )
        )
        await runtime.ainvoke(
            thread_id=name,
            run_id=name,
            context=Context(customer=name),
            input={"messages": [{"role": "user", "content": "Inspect"}]},
        )

    async with asyncio.TaskGroup() as group:
        group.create_task(run("first"))
        group.create_task(run("second"))
        await asyncio.gather(*(signal.wait() for signal in entered.values()))
        assert not workspace.closed
        release.set()
    assert sorted(seen) == [
        (name, Path("/files") / name, Context(customer=name))
        for name in ("first", "second")
    ]
    assert set(workspace.closed) == set(workspace.opened)


@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_tool_settlement_precedes_workspace_revocation(failure: str) -> None:
    workspace = Workspace()
    entered = asyncio.Event()
    release = asyncio.Event()
    seen: list[ToolRuntime[None, Path]] = []
    settled = []

    @tool
    async def inspect_workspace(runtime: ToolRuntime[None, Path]) -> str:
        """Wait while holding a borrowed workspace."""
        seen.append(runtime)
        entered.set()
        try:
            await release.wait()
            raise RuntimeError("tool failed")
        finally:
            assert runtime.workspace == Path("/files/run")
            assert not workspace.closed
            settled.append("tool")

    runtime = (
        TinkerFin()
        .with_namespace("example")
        .build(
            model=Model(responses=[call("inspect_workspace")]),
            tools=[inspect_workspace],
            backend=workspace,
        )
    )
    task = asyncio.create_task(
        runtime.ainvoke(
            thread_id="thread",
            run_id="run",
            input={"messages": [{"role": "user", "content": "Inspect"}]},
        )
    )
    try:
        await entered.wait()
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            with pytest.raises(RuntimeError, match="tool failed"):
                await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert settled == ["tool"]
    assert workspace.closed == workspace.opened
    with pytest.raises(TinkerFinLifecycleError, match="closed"):
        _ = seen[0].workspace


async def test_approval_resume_borrows_a_fresh_workspace_with_the_resume_identity() -> (
    None
):
    workspace = Workspace()
    seen: list[ToolRuntime[None, Path, DeepAgentState]] = []

    @tool
    async def inspect_workspace(runtime: ToolRuntime[None, Path]) -> str:
        """Inspect an approved workspace."""
        seen.append(runtime)
        assert runtime.workspace == Path("/files/resume")
        return "ready"

    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("example")
        .build(
            model=Model(
                responses=[call("inspect_workspace"), AIMessage(content="done")]
            ),
            tools=[inspect_workspace],
            backend=workspace,
            interrupt_on={"inspect_workspace": True},
        )
    )
    first = await runtime.ainvoke(
        thread_id="thread",
        run_id="initial",
        input={"messages": [{"role": "user", "content": "Inspect"}]},
    )
    assert first.get("__interrupt__")
    assert not seen
    assert workspace.closed == workspace.opened
    await runtime.ainvoke(
        thread_id="thread",
        run_id="resume",
        input=Command(resume={"decisions": [{"type": "approve"}]}),
    )
    assert len(seen) == 1 and seen[0].identity.run_id == "resume"
    assert [item.run_id for item in workspace.closed] == ["initial", "resume"]


async def test_compiled_subagents_do_not_receive_local_workspace_access() -> None:
    from langchain.agents import create_agent

    workspace = Workspace()
    invoked = []

    @tool
    async def external_tool(runtime: NativeToolRuntime) -> str:
        """Use only the external graph's tool runtime."""
        assert not isinstance(runtime, ToolRuntime)
        assert not hasattr(runtime, "workspace")
        invoked.append("external")
        return "ready"

    external = create_agent(
        model=Model(responses=[call("external_tool"), AIMessage(content="done")]),
        tools=[external_tool],
    )
    runtime = (
        TinkerFin()
        .with_namespace("example")
        .build(
            model=Model(responses=[delegate("external"), AIMessage(content="done")]),
            backend=workspace,
            subagents=[
                {
                    "name": "external",
                    "description": "External tools",
                    "runnable": external,
                }
            ],
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "Delegate"}]},
    )
    assert invoked == ["external"]
    assert workspace.closed == workspace.opened


async def test_standalone_graph_cannot_claim_managed_identity() -> None:
    from tinkerfin.deep_agent import create_graph

    @tool
    async def inspect_workspace(runtime: ToolRuntime) -> str:
        """Require managed access explicitly."""
        with pytest.raises(TinkerFinLifecycleError, match="managed run"):
            _ = runtime.identity
        with pytest.raises(TinkerFinLifecycleError, match="managed run"):
            _ = runtime.workspace
        return "standalone"

    graph = await create_graph(
        TinkerFin()
        .with_namespace("example")
        .build(
            model=Model(
                responses=[call("inspect_workspace"), AIMessage(content="done")]
            ),
            tools=[inspect_workspace],
        )
    )
    await graph.ainvoke({"messages": [{"role": "user", "content": "Inspect"}]})
