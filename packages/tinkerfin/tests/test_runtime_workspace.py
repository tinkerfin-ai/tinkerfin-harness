"""Run-scoped workspace borrowing and role-local business tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.tools import tool
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.store.memory import InMemoryStore
from pydantic import Field
from test_runtime_store import _Model

from tinkerfin import TinkerFin
from tinkerfin.deep_agent import create_graph
from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_messaging import MemoryBackend, Messaging


class _Workspace:
    def __init__(self, backend: BackendProtocol | None = None) -> None:
        self.backend = StateBackend() if backend is None else backend
        self.opened: list[RunIdentity] = []
        self.closed: list[RunIdentity] = []
        self.context = ContextVar("test_workspace", default="outside")

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncGenerator[PreparedWorkspace[Path, BackendProtocol]]:
        self.opened.append(identity)
        token = self.context.set(identity.namespace)
        try:
            yield PreparedWorkspace(
                Path("/files") / identity.namespace,
                self.backend,
                filesystem_instructions="File paths are relative to the selected workspace.",
            )
        finally:
            self.context.reset(token)
            self.closed.append(identity)


class _RecordingModel(_Model):
    seen: list[set[str]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        self.seen.append({tool.name for tool in tools if isinstance(tool, BaseTool)})
        return super().bind_tools(tools, **kwargs)


async def test_static_tools_allow_middleware_without_registered_tools() -> None:
    workspace = _Workspace()

    @tool
    async def inspect_workspace() -> str:
        """Inspect available files."""
        return "ready"

    model = _RecordingModel(responses=[AIMessage(content="ready")])
    runtime = (
        TinkerFin()
        .with_namespace("user-a")
        .build(
            model=model,
            backend=workspace,
            tools=[inspect_workspace],
            middleware=[PatchToolCallsMiddleware()],
        )
    )
    stream = runtime.open_run(
        thread_id="conversation", run_id="run", input={"messages": []}
    )
    try:
        assert [part async for part in stream]
    finally:
        await stream.aclose()
    assert model.seen and "inspect_workspace" in model.seen[0]
    assert (
        workspace.opened
        == workspace.closed
        == [runtime.run_identity("conversation", "run")]
    )


@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_cancelled_workspace_preparation_releases_without_building(
    protocol: str,
) -> None:
    entered = asyncio.Event()

    class WaitingWorkspace(_Workspace):
        @asynccontextmanager
        async def prepare(
            self, identity: RunIdentity
        ) -> AsyncGenerator[PreparedWorkspace[Path, BackendProtocol]]:
            async with super().prepare(identity) as prepared:
                entered.set()
                await asyncio.Event().wait()
                yield prepared

    workspace = WaitingWorkspace()
    model = _RecordingModel(responses=[AIMessage(content="done")])
    runtime = (
        TinkerFin().with_namespace("company").build(model=model, backend=workspace)
    )
    stream = (runtime.open_run if protocol == "native" else runtime.open_agui_run)(
        thread_id="thread", run_id="run", input={"messages": []}
    )
    preparing = asyncio.create_task(stream.messaging_owner_preflight())
    try:
        await entered.wait()
        await stream.aclose()
        with pytest.raises(asyncio.CancelledError):
            await preparing
    finally:
        if not preparing.done():
            preparing.cancel()
        await asyncio.gather(preparing, return_exceptions=True)
    assert model.seen == []
    assert len(workspace.opened) == 1 and workspace.closed == workspace.opened


async def test_direct_graph_rejects_lazy_workspace_before_io() -> None:
    workspace = _Workspace()
    runtime = (
        TinkerFin()
        .with_namespace("company")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            backend=workspace,
        )
    )
    with pytest.raises(ValueError, match="managed Runtime"):
        await create_graph(runtime)
    assert workspace.opened == []


async def test_messaging_replay_does_not_reopen_workspace() -> None:
    workspace = _Workspace()
    runtime = (
        TinkerFin()
        .with_namespace("company")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            backend=workspace,
        )
    )
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="workspace-runs")
        for _ in range(2):
            body = await channel.open_sse(
                runtime.open_agui_run(
                    thread_id="thread", run_id="run", input={"messages": []}
                ),
                after=0,
            )
            assert [frame async for frame in body]
    assert workspace.closed == workspace.opened
    assert len(workspace.opened) == 1


@pytest.mark.parametrize("child", [False, True])
def test_workspace_filesystem_cannot_be_replaced_by_custom_middleware(
    child: bool,
) -> None:
    workspace = _Workspace()
    filesystem = FilesystemMiddleware(backend=StateBackend())
    with pytest.raises(ValueError, match="Workspace owns its filesystem"):
        TinkerFin().with_namespace("company").build(
            model=_Model(responses=[AIMessage(content="done")]),
            backend=workspace,
            middleware=[] if child else [filesystem],
            subagents=[
                {
                    "name": "worker",
                    "description": "Read",
                    "system_prompt": "Read",
                    "middleware": [filesystem],
                }
            ]
            if child
            else [],
        )
    assert not workspace.opened


async def test_prepared_composite_backend_cannot_bypass_runtime_store_isolation() -> (
    None
):
    workspace = _Workspace(
        CompositeBackend(
            default=StateBackend(),
            routes={
                "/memory/": StoreBackend(
                    store=InMemoryStore(), namespace=lambda _: ("memory",)
                ),
            },
        )
    )
    runtime = (
        TinkerFin()
        .with_namespace("company")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            backend=workspace,
        )
    )
    with pytest.raises(ValueError, match="StoreBackend must use the Runtime store"):
        await runtime.ainvoke(thread_id="thread", run_id="run", input={"messages": []})
    assert len(workspace.opened) == 1 and workspace.closed == workspace.opened
