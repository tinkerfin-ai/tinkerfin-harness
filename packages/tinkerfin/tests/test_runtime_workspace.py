"""Run-scoped workspace borrowing and role-local business tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace, TracebackType
from typing import Any, Self

import pytest
from ag_ui.core import RunErrorEvent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.tools import tool
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field
from test_runtime_store import _Model

from tinkerfin import AgUiResumeRequest, TinkerFin
from tinkerfin.deep_agent import create_graph
from tinkerfin_contracts import (
    PreparedWorkspace,
    RunIdentity,
    RunTerminalObservation,
)
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


class _PreparationDeadline:
    def __init__(self) -> None:
        self.owner: asyncio.Task[object] | None = None
        self.triggered = False
        self.cancellation_count = 0

    async def __aenter__(self) -> Self:
        self.owner = asyncio.current_task()
        assert self.owner is not None
        self.cancellation_count = self.owner.cancelling()
        return self

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del error_type, traceback
        if self.triggered and isinstance(error, asyncio.CancelledError):
            assert self.owner is not None
            if self.owner.uncancel() <= self.cancellation_count:
                raise TimeoutError from error

    def expire(self) -> None:
        assert self.owner is not None
        self.triggered = True
        self.owner.cancel()

    def expired(self) -> bool:
        return self.triggered


@pytest.mark.parametrize(
    ("resume_state", "cancel"),
    [
        ("ordinary", False),
        ("ordinary", True),
        ("no_saver", False),
        ("not_saved", False),
        ("unreadable", False),
    ],
)
async def test_stream_deadline_covers_workspace_preparation_and_matches_trace(
    monkeypatch: pytest.MonkeyPatch,
    resume_state: str,
    cancel: bool,
) -> None:
    entered = asyncio.Event()
    deadline = _PreparationDeadline()
    deadlines: list[float] = []
    terminals: list[RunTerminalObservation] = []
    released: list[str] = []

    class UnreadableSaver(MemorySaver):
        async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
            del config
            raise OSError("checkpoint unavailable")

    class WaitingWorkspace(_Workspace):
        @asynccontextmanager
        async def prepare(
            self, identity: RunIdentity
        ) -> AsyncGenerator[PreparedWorkspace[Path, BackendProtocol]]:
            async with super().prepare(identity) as prepared:
                entered.set()
                await asyncio.Event().wait()
                yield prepared

    def timeout_at(when: float) -> _PreparationDeadline:
        deadlines.append(when)
        return deadline

    async def record_terminal(observation: RunTerminalObservation) -> None:
        terminals.append(observation)

    async def not_saved() -> None:
        released.append("not_saved")

    controlled = ModuleType("controlled_asyncio")
    controlled.__dict__.update(vars(asyncio))
    setattr(controlled, "get_running_loop", lambda: SimpleNamespace(time=lambda: 100.0))
    setattr(controlled, "timeout_at", timeout_at)
    monkeypatch.setattr("tinkerfin._runtime_agui.asyncio", controlled)
    monkeypatch.setattr("tinkerfin._runtime_streams.asyncio", controlled)
    workspace = WaitingWorkspace()
    model = _RecordingModel(responses=[AIMessage(content="unused")])
    runtime = (
        TinkerFin(
            checkpointer=(
                UnreadableSaver()
                if resume_state == "unreadable"
                else MemorySaver()
                if resume_state == "not_saved"
                else None
            )
        )
        .with_namespace("company")
        .with_observer(on_terminal=record_terminal)
        .build(model, backend=workspace)
    )
    if resume_state == "ordinary":
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            input={"messages": []},
            stream_timeout=5,
        )
    else:
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            resume=AgUiResumeRequest.model_validate(
                {
                    "entries": [
                        {
                            "interruptId": "interrupt#0",
                            "status": "resolved",
                            "payload": {"type": "approve"},
                        }
                    ]
                }
            ),
            on_resume_not_saved=not_saved,
            stream_timeout=5,
        )
    assert (await anext(stream)).type.value == "RUN_STARTED"
    assert deadlines == []
    pending = asyncio.create_task(anext(stream))
    preparing = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait((preparing, pending), return_when=asyncio.FIRST_COMPLETED)
        if not preparing.done():
            pytest.fail(f"preparation did not start: {await pending}; {stream.error!r}")
        if cancel:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            deadline.expire()
            terminal = await pending
            assert isinstance(terminal, RunErrorEvent)
            assert terminal.code == "stream_timeout"
        assert deadlines == [105.0]
        assert len(terminals) == 1
        assert terminals[0].outcome == ("cancelled" if cancel else "failed")
        assert terminals[0].code == ("cancelled" if cancel else "stream_timeout")
        assert released == (["not_saved"] if resume_state == "not_saved" else [])
        assert workspace.closed == workspace.opened
        assert model.seen == []
    finally:
        preparing.cancel()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(preparing, pending, return_exceptions=True)
        await stream.aclose()


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
    if protocol == "agui":
        await anext(stream)
    preparing = asyncio.create_task(anext(stream))
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


@pytest.mark.parametrize(
    ("reason", "expected_code"),
    [
        ("busy", "workspace_busy"),
        ("file_conflict", "workspace_file_conflict"),
        (None, "runtime_initialization_error"),
        (["busy"], "runtime_initialization_error"),
        ("unknown", "runtime_initialization_error"),
    ],
)
async def test_workspace_preparation_reason_is_safe_and_has_one_terminal(
    reason: str | list[str] | None,
    expected_code: str,
) -> None:
    class ProviderFailure(Exception):
        workspace_failure: str | list[str] | None

    failure = ProviderFailure("private provider diagnostics")
    failure.workspace_failure = reason

    class UnavailableWorkspace(_Workspace):
        @asynccontextmanager
        async def prepare(
            self, identity: RunIdentity
        ) -> AsyncGenerator[PreparedWorkspace[Path, BackendProtocol]]:
            async with super().prepare(identity) as prepared:
                if failure is not None:
                    raise failure
                yield prepared

    workspace = UnavailableWorkspace()
    observed: list[RunTerminalObservation] = []

    async def on_terminal(event: RunTerminalObservation) -> None:
        observed.append(event)

    runtime = (
        TinkerFin()
        .with_namespace("workspace-failure")
        .with_observer(on_terminal=on_terminal)
        .build(model=_Model(responses=[AIMessage(content="unused")]), backend=workspace)
    )
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="run",
        messages=[{"id": "question", "role": "user", "content": "hello"}],
    )
    events = [event async for event in stream]
    assert [event.type for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == expected_code
    recorded = [item for item in observed if isinstance(item, RunTerminalObservation)]
    assert len(recorded) == 1 and recorded[0].code == terminal.code
    assert terminal.message == "Agent run failed"
    assert "private" not in terminal.model_dump_json()
    assert stream.error is failure
    assert workspace.opened == workspace.closed
