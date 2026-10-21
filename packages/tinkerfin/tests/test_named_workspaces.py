"""Auxiliary workspaces preserve the default files and durable review boundary."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from pathlib import Path

import pytest
from ag_ui.core import (
    RunFinishedEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from deepagents.backends import CompositeBackend, LocalShellBackend
from deepagents.backends.protocol import BackendProtocol
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_compaction import RecordingModel
from test_prepared_workspace_tools import ProvidingWorkspace
from test_workspace_file_paths import NativeWorkspace, consume, decision

from tinkerfin import TinkerFin, TinkerFinLifecycleError
from tinkerfin_contracts import PreparedWorkspace, RunIdentity


def call(name: str, identifier: str, **arguments: object) -> AIMessage:
    return AIMessage(
        id=f"message-{identifier}",
        content="",
        tool_calls=[{"name": name, "id": identifier, "args": arguments}],
    )


@pytest.mark.parametrize("review", [False, True])
async def test_sequential_auxiliary_file_and_shell_operations_keep_only_current_targets(
    tmp_path: Path, review: bool
) -> None:
    computer = ProvidingWorkspace(tmp_path / "computer")
    computer.backend = CompositeBackend(
        default=LocalShellBackend(root_dir=computer.directory, virtual_mode=False),
        routes={},
        artifacts_root=str(computer.directory / ".artifacts"),
    )
    model = RecordingModel(
        responses=[
            call("computer_tools", "discover"),
            call(
                "computer_write_file", "write", file_path="hello.txt", content="hello"
            ),
            call("computer_execute", "execute", command="cat hello.txt"),
            call("computer_read_file", "read", file_path="hello.txt"),
            AIMessage(content="done"),
        ]
    )
    saver = InMemorySaver()

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("account")
            .build(
                model=model,
                workspaces={"computer": computer},
                interrupt_on={"computer_write_file": True, "computer_execute": True}
                if review
                else None,
            )
        )

    events = await consume(
        build().open_agui_run(
            thread_id="thread",
            run_id="proposal",
            messages=[{"id": "user", "role": "user", "content": "write and verify"}],
        )
    )
    if review:
        assert not (computer.directory / "hello.txt").exists()
        for run_id in ("approve-write", "approve-execute"):
            terminal = events[-1]
            assert isinstance(terminal, RunFinishedEvent)
            events = await consume(
                build().open_agui_run(
                    thread_id="thread",
                    run_id=run_id,
                    resume=decision(terminal, {"type": "approve"}),
                )
            )
            assert (computer.directory / "hello.txt").read_text() == "hello"
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "success"
    results = {
        item.tool_call_id: item
        for item in model.inputs[-1]
        if isinstance(item, ToolMessage)
    }
    for identifier in ("write", "execute", "read"):
        assert results[identifier].status == "success"
    assert "hello" in str(results["execute"].content)
    assert "hello" in str(results["read"].content)
    assert computer.closed == (
        ["proposal", "approve-write", "approve-execute"] if review else ["proposal"]
    )


@pytest.mark.parametrize("review", [False, True])
async def test_next_turn_rediscovery_recovers_undeclared_tool_without_execution(
    tmp_path: Path, review: bool
) -> None:
    computer = ProvidingWorkspace(tmp_path / "computer")
    computer.backend = CompositeBackend(
        default=LocalShellBackend(root_dir=computer.directory, virtual_mode=False),
        routes={},
        artifacts_root=str(computer.directory / ".artifacts"),
    )
    model = RecordingModel(
        responses=[
            call("computer_tools", "discover-first"),
            call(
                "computer_write_file", "write", file_path="hello.txt", content="hello"
            ),
            AIMessage(content="written"),
            call("computer_execute", "undeclared", command="touch forbidden.txt"),
            call("computer_tools", "rediscover"),
            call("computer_execute", "execute", command="cat hello.txt"),
            AIMessage(content="done"),
        ]
    )
    saver = InMemorySaver()

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("account")
            .build(
                model=model,
                workspaces={"computer": computer},
                interrupt_on={"computer_write_file": True, "computer_execute": True}
                if review
                else None,
            )
        )

    for turn in ("first", "second"):
        events = await consume(
            build().open_agui_run(
                thread_id="thread",
                run_id=turn,
                messages=[{"id": turn, "role": "user", "content": turn}],
            )
        )
        assert sum(isinstance(event, RunFinishedEvent) for event in events) == 1
        if turn == "second":
            rejected = next(
                event
                for event in events
                if isinstance(event, ToolCallStartEvent)
                and event.tool_call_name == "computer_execute"
            )
            lifecycle = [
                event.type
                for event in events
                if isinstance(
                    event, (ToolCallStartEvent, ToolCallEndEvent, ToolCallResultEvent)
                )
                and event.tool_call_id == rejected.tool_call_id
            ]
            assert lifecycle == ["TOOL_CALL_START", "TOOL_CALL_END", "TOOL_CALL_RESULT"]
        if review:
            assert events[-1].outcome.type == "interrupt"
            assert "forbidden.txt" not in str(events[-1].outcome.interrupts)
            events = await consume(
                build().open_agui_run(
                    thread_id="thread",
                    run_id=f"approve-{turn}",
                    resume=decision(events[-1], {"type": "approve"}),
                )
            )
        assert events[-1].outcome.type == "success"
    results = {
        item.tool_call_id: item
        for item in model.inputs[-1]
        if isinstance(item, ToolMessage)
    }
    assert results["undeclared"].status == "error"
    assert "computer_tools" in str(results["undeclared"].content)
    assert results["execute"].status == "success"
    assert "hello" in str(results["execute"].content)
    assert not (computer.directory / "forbidden.txt").exists()
    assert (computer.directory / "hello.txt").read_text() == "hello"
    assert computer.closed == (
        ["first", "approve-first", "second", "approve-second"]
        if review
        else ["first", "second"]
    )


async def test_workspace_prefix_cannot_shadow_default_file_tools(
    tmp_path: Path,
) -> None:
    workspace = ProvidingWorkspace(tmp_path / "computer")
    with pytest.raises(ValueError, match="reserved tool prefix"):
        TinkerFin().with_namespace("account").build(
            model=RecordingModel(responses=[AIMessage(content="done")]),
            workspaces={"write": workspace},
        )
    assert workspace.closed == []


async def test_default_workspace_does_not_prepare_unused_auxiliary_resource(
    tmp_path: Path,
) -> None:
    (tmp_path / "cloud-root").mkdir()
    (tmp_path / "computer-root").mkdir()
    cloud = NativeWorkspace(tmp_path / "cloud-root" / "project")
    computer = ProvidingWorkspace(tmp_path / "computer-root" / "project")
    runtime = (
        TinkerFin()
        .with_namespace("account")
        .build(
            model=RecordingModel(
                responses=[
                    call(
                        "write_file",
                        "cloud-write",
                        file_path="report.md",
                        content="cloud",
                    ),
                    AIMessage(content="done"),
                ]
            ),
            backend=cloud,
            workspaces={"computer": computer},
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "write"}]},
    )
    assert (cloud.directory / "report.md").read_text() == "cloud"
    assert not (computer.directory / "report.md").exists()
    assert computer.closed == []
    assert cloud.closed == ["run"]


@pytest.mark.parametrize("resolution", ["approve", "reject", "cancel"])
async def test_discovered_workspace_keeps_paths_and_scope_across_review(
    tmp_path: Path, resolution: str
) -> None:
    (tmp_path / "cloud-root").mkdir()
    (tmp_path / "computer-root").mkdir()
    cloud = NativeWorkspace(tmp_path / "cloud-root" / "project")
    computer = ProvidingWorkspace(tmp_path / "computer-root" / "project")
    model = RecordingModel(
        responses=[
            call("computer_tools", "discover"),
            call(
                "computer_inspect_paths",
                "inspect",
                action={"kind": "read", "paths": ["report.md"]},
            ),
            AIMessage(content="done"),
        ]
    )
    saver = InMemorySaver()

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("account")
            .build(
                model=model,
                backend=cloud,
                workspaces={"computer": computer},
                interrupt_on={"computer_inspect_paths": True},
            )
        )

    events = await consume(
        build().open_agui_run(
            thread_id="thread",
            run_id="proposal",
            messages=[{"id": "user", "role": "user", "content": "inspect"}],
        )
    )
    terminal = events[-1]
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    assert computer.effects == []
    payload = None if resolution == "cancel" else {"type": resolution}
    await consume(
        build().open_agui_run(
            thread_id="thread",
            run_id="resume",
            resume=decision(terminal, payload),
        )
    )
    assert computer.effects == (
        [{"action": {"kind": "read", "paths": [str(computer.directory / "report.md")]}}]
        if resolution == "approve"
        else []
    )
    assert computer.closed == (
        ["proposal"] if resolution == "cancel" else ["proposal", "resume"]
    )
    assert cloud.closed == ["proposal", "resume"]
    assert {origin.origin_identity.run_id for origin in computer.origins} == {
        "proposal"
    }
    assert len({origin.invocation_id for origin in computer.origins}) == 1


async def test_default_and_auxiliary_files_with_same_path_remain_separate(
    tmp_path: Path,
) -> None:
    (tmp_path / "cloud-root").mkdir()
    (tmp_path / "computer-root").mkdir()
    cloud = NativeWorkspace(tmp_path / "cloud-root" / "project")
    computer = ProvidingWorkspace(tmp_path / "computer-root" / "project")
    model = RecordingModel(
        responses=[
            call("computer_tools", "discover"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "cloud",
                        "name": "write_file",
                        "args": {"file_path": "report.md", "content": "cloud"},
                    },
                    {
                        "id": "local",
                        "name": "computer_write_file",
                        "args": {"file_path": "report.md", "content": "local"},
                    },
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    await (
        TinkerFin()
        .with_namespace("account")
        .build(
            model=model,
            backend=cloud,
            workspaces={"computer": computer},
        )
        .ainvoke(
            thread_id="thread",
            run_id="run",
            input={"messages": [{"role": "user", "content": "write"}]},
        )
    )
    assert (cloud.directory / "report.md").read_text() == "cloud"
    assert (computer.directory / "report.md").read_text() == "local"
    results = [item for item in model.inputs[-1] if isinstance(item, ToolMessage)]
    assert {item.name for item in results} >= {"write_file", "computer_write_file"}
    assert computer.closed == ["run"]


@pytest.mark.parametrize("role", ["general-purpose", "specialist"])
async def test_auxiliary_tools_follow_subagent_scope_and_review(
    tmp_path: Path, role: str
) -> None:
    computer = ProvidingWorkspace(tmp_path / "computer")
    delegate = call("task", "delegate", subagent_type=role, description="inspect files")
    discovery = call("computer_tools", "discover")
    inspect = call(
        "computer_inspect_paths",
        "inspect",
        action={"kind": "read", "paths": ["report.md"]},
    )
    child = RecordingModel(
        responses=[discovery, inspect, AIMessage(content="inspected")]
    )
    parent = RecordingModel(
        responses=(
            [
                delegate,
                discovery,
                inspect,
                AIMessage(content="inspected"),
                AIMessage(content="done"),
            ]
            if role == "general-purpose"
            else [delegate, AIMessage(content="done")]
        )
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("account")
        .build(
            model=parent,
            workspaces={"computer": computer},
            subagents=[]
            if role == "general-purpose"
            else [
                {
                    "name": role,
                    "description": "Inspect files",
                    "system_prompt": "Inspect files",
                    "model": child,
                }
            ],
            interrupt_on={"computer_inspect_paths": True},
        )
    )
    events = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="proposal",
            messages=[{"id": "user", "role": "user", "content": "inspect"}],
        )
    )
    assert computer.effects == []
    await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="approved",
            resume=decision(events[-1], {"type": "approve"}),
        )
    )
    assert len(computer.effects) == 1
    assert computer.origins[-1].graph_namespace
    assert computer.origins[-1].origin_identity.run_id == "proposal"
    assert computer.closed == ["proposal", "approved"]


async def test_later_parallel_auxiliary_writes_to_same_target_are_rejected(
    tmp_path: Path,
) -> None:
    computer = ProvidingWorkspace(tmp_path / "computer")
    model = RecordingModel(
        responses=[
            call("computer_tools", "discover"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": identifier,
                        "name": "computer_write_file",
                        "args": {"file_path": path, "content": identifier},
                    }
                    for identifier, path in (
                        ("first", "report.md"),
                        ("second", str(computer.directory / "report.md")),
                    )
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    await (
        TinkerFin()
        .with_namespace("account")
        .build(model=model, workspaces={"computer": computer})
        .ainvoke(
            thread_id="thread",
            run_id="run",
            input={"messages": [{"role": "user", "content": "write"}]},
        )
    )
    assert (computer.directory / "report.md").read_text() == "first"
    results = [
        item
        for item in model.inputs[-1]
        if isinstance(item, ToolMessage) and item.tool_call_id in {"first", "second"}
    ]
    assert {item.tool_call_id: item.status for item in results} == {
        "first": "success",
        "second": "error",
    }


async def test_resume_rejects_changed_auxiliary_authorization(tmp_path: Path) -> None:
    computer = ProvidingWorkspace(tmp_path / "computer")
    saver = InMemorySaver()
    model = RecordingModel(
        responses=[
            call("computer_tools", "discover"),
            call(
                "computer_inspect_paths",
                "inspect",
                action={"kind": "read", "paths": ["report.md"]},
            ),
            AIMessage(content="done"),
        ]
    )

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("account")
            .build(
                model=model,
                workspaces={"computer": computer},
                interrupt_on={"computer_inspect_paths": True},
            )
        )

    events = await consume(
        build().open_agui_run(
            thread_id="thread",
            run_id="proposal",
            messages=[{"id": "user", "role": "user", "content": "inspect"}],
        )
    )
    computer.workspace_scope_id = "different-device"
    stream = build().open_agui_run(
        thread_id="thread",
        run_id="approved",
        resume=decision(events[-1], {"type": "approve"}),
    )
    async with aclosing(stream):
        _events = [event async for event in stream]
    assert isinstance(stream.error, TinkerFinLifecycleError)
    assert computer.effects == []


async def test_cancelled_preparation_exits_provider_in_its_own_task(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    released = asyncio.Event()
    owners: list[asyncio.Task[object] | None] = []

    class WaitingWorkspace:
        @asynccontextmanager
        async def prepare(
            self, identity: RunIdentity
        ) -> AsyncIterator[PreparedWorkspace[object, BackendProtocol]]:
            owners.append(asyncio.current_task())
            try:
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError("preparation should be cancelled")
                yield
            finally:
                owners.append(asyncio.current_task())
                released.set()

    runtime = (
        TinkerFin()
        .with_namespace("account")
        .build(
            model=RecordingModel(responses=[call("computer_tools", "discover")]),
            workspaces={"computer": WaitingWorkspace()},
        )
    )
    run = asyncio.create_task(
        runtime.ainvoke(
            thread_id="thread",
            run_id="run",
            input={"messages": [{"role": "user", "content": "discover"}]},
        )
    )
    try:
        await entered.wait()
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    assert released.is_set()
    assert len(owners) == 2 and owners[0] is owners[1]


async def test_planning_uses_named_tools_without_bypassing_tool_review(
    tmp_path: Path,
) -> None:
    computer = ProvidingWorkspace(tmp_path / "computer")
    model = RecordingModel(
        responses=[
            call("computer_tools", "discover"),
            call(
                "computer_inspect_paths",
                "inspect",
                action={"kind": "read", "paths": ["report.md"]},
            ),
            AIMessage(content="Analysis complete"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("account")
        .with_plan(enabled=True)
        .build(
            model=model,
            workspaces={"computer": computer},
            interrupt_on={"computer_inspect_paths": True},
        )
    )
    events = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="proposal",
            mode="plan",
            messages=[{"id": "user", "role": "user", "content": "inspect"}],
        )
    )
    assert computer.effects == []
    resumed = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="approved",
            resume=decision(events[-1], {"type": "approve"}),
        )
    )
    assert len(computer.effects) == 1
    assert any("inspected" in event.model_dump_json() for event in resumed)


@pytest.mark.parametrize("remembered_tool", [False, True])
async def test_next_cloud_turn_ignores_discovered_but_offline_resource(
    tmp_path: Path,
    remembered_tool: bool,
) -> None:
    class SwitchableWorkspace(ProvidingWorkspace):
        available = True

        @asynccontextmanager
        async def prepare(self, identity: RunIdentity):
            if not self.available:
                raise ValueError("device offline")
            async with super().prepare(identity) as prepared:
                yield prepared

    computer = SwitchableWorkspace(tmp_path / "computer")
    model = RecordingModel(
        responses=[
            call("computer_tools", "discover"),
            AIMessage(content="discovered"),
            *(
                [call("computer_read_file", "unavailable", file_path="report.md")]
                if remembered_tool
                else []
            ),
            call("write_file", "cloud", file_path="/cloud.txt", content="cloud"),
            AIMessage(content="cloud answer"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("account")
        .build(
            model=model,
            workspaces={"computer": computer},
            interrupt_on={"computer_read_file": True},
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="first",
        input={"messages": [{"role": "user", "content": "discover"}]},
    )
    computer.available = False
    result = await runtime.ainvoke(
        thread_id="thread",
        run_id="second",
        input={"messages": [{"role": "user", "content": "answer in cloud"}]},
    )
    assert computer.closed == ["first"]
    assert len(model.inputs) == (5 if remembered_tool else 4)
    files = result["files"]
    assert isinstance(files, dict) and "/cloud.txt" in files
    if remembered_tool:
        rejected = next(
            item
            for item in model.inputs[-1]
            if isinstance(item, ToolMessage) and item.tool_call_id == "unavailable"
        )
        assert rejected.status == "error"


async def test_unknown_auxiliary_call_does_not_block_valid_parallel_review(
    tmp_path: Path,
) -> None:
    computer = ProvidingWorkspace(tmp_path / "computer")
    model = RecordingModel(
        responses=[
            call("computer_tools", "discover"),
            AIMessage(
                id="mixed-proposal",
                content="",
                tool_calls=[
                    {"name": "computer_missing", "id": "missing", "args": {}},
                    {
                        "name": "computer_write_file",
                        "id": "write",
                        "args": {"file_path": "report.md", "content": "saved"},
                    },
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("account")
        .build(
            model=model,
            workspaces={"computer": computer},
            interrupt_on={"computer_missing": True, "computer_write_file": True},
        )
    )
    events = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="proposal",
            messages=[{"id": "user", "role": "user", "content": "write"}],
        )
    )
    assert events[-1].outcome.type == "interrupt"
    assert not (computer.directory / "report.md").exists()
    assert "computer_missing" not in str(events[-1].outcome.interrupts)
    completed = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="approved",
            resume=decision(events[-1], {"type": "approve"}),
        )
    )
    assert completed[-1].outcome.type == "success"
    assert (computer.directory / "report.md").read_text() == "saved"
    assert computer.closed == ["proposal", "approved"]
    results = [item for item in model.inputs[-1] if isinstance(item, ToolMessage)]
    assert [(item.tool_call_id, item.status) for item in results] == [
        ("discover", "success"),
        ("missing", "error"),
        ("write", "success"),
    ]
