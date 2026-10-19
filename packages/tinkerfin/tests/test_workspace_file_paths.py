"""Workspace targets stay consistent through proposal, review, and execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import pytest
from ag_ui.core import RunErrorEvent, RunFinishedEvent
from deepagents import DeepAgentState
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.backends.protocol import BackendProtocol, WriteResult
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import Field
from test_compaction import RecordingModel
from test_hitl_capability import _Model

from tinkerfin import AgUiResumeRequest, TinkerFin
from tinkerfin.tools import ToolRuntime
from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_tracing import Tracer


class ToolSchemaModel(RecordingModel):
    bound_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict, exclude=True)

    def bind_tools(self, tools, **kwargs):
        self.bound_schemas = {
            schema["function"]["name"]: schema["function"]
            for tool in tools
            for schema in [convert_to_openai_tool(tool)]
        }
        return super().bind_tools(tools, **kwargs)


class NativeWorkspace:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.home = directory.parent / "home"
        self.home.mkdir()
        self.directory.mkdir()
        self.resolutions: list[tuple[str | None, str]] = []
        self.closed: list[str] = []
        self.backend = CompositeBackend(
            default=FilesystemBackend(root_dir=directory, virtual_mode=False),
            routes={},
            artifacts_root=str(directory / ".artifacts"),
        )

    async def resolve(
        self, path: str | None, *, operation: Literal["read", "write"]
    ) -> str:
        self.resolutions.append((path, operation))
        if path is None:
            selected = self.directory
        elif path.startswith("~/"):
            selected = self.home / path[2:]
        else:
            selected = Path(path)
            if not selected.is_absolute():
                selected = self.directory / selected
        return str(await asyncio.to_thread(selected.resolve))

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[Path, BackendProtocol]]:
        try:
            yield PreparedWorkspace(
                self.directory,
                self.backend,
                file_path_resolver=self.resolve,
                filesystem_instructions=(
                    f"Relative paths start in {self.directory}. "
                    f"Absolute paths address the native filesystem; ~/ refers to {self.home}."
                ),
            )
        finally:
            self.closed.append(identity.run_id)


async def consume(stream):
    async with aclosing(stream):
        result = [event async for event in stream]
    assert stream.error is None, stream.error
    return result


def decision(terminal: RunFinishedEvent, payload: dict | None) -> AgUiResumeRequest:
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    return AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": item.id,
                    "status": "cancelled" if payload is None else "resolved",
                    **({} if payload is None else {"payload": payload}),
                }
                for item in terminal.outcome.interrupts
            ]
        }
    )


@pytest.mark.parametrize("form", ["relative", "home", "symlink_parent"])
async def test_approval_trace_and_execution_use_resolved_target_without_rewriting_model_output(
    tmp_path: Path, form: str
) -> None:
    workspace = NativeWorkspace(tmp_path / "project")
    if form == "symlink_parent":
        (tmp_path / "outside" / "child").mkdir(parents=True)
        (workspace.directory / "link").symlink_to(tmp_path / "outside" / "child")
        raw, target = "link/../报告.txt", tmp_path / "outside" / "报告.txt"
    elif form == "home":
        raw, target = "~/报告.txt", workspace.home / "报告.txt"
    else:
        raw, target = "报告.txt", workspace.directory / "报告.txt"
    proposal = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "write",
                "name": "write_file",
                "args": {"file_path": raw, "content": "report"},
            }
        ],
    )
    tracer = Tracer()
    model = ToolSchemaModel(responses=[proposal, AIMessage(content="done")])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .with_observer(tracer)
        .build(
            model=model,
            backend=workspace,
            interrupt_on={"write_file": True},
        )
    )
    events = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="first",
            messages=[{"id": "user", "role": "user", "content": "write"}],
        )
    )
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent) and terminal.outcome is not None
    assert terminal.outcome.type == "interrupt"
    assert terminal.outcome.interrupts[0].metadata is not None
    assert terminal.outcome.interrupts[0].metadata["deepagents"]["originalArgs"][
        "file_path"
    ] == str(target)
    assert not target.exists()
    original_arguments = "".join(
        event.delta for event in events if event.type.value == "TOOL_CALL_ARGS"
    )
    assert json.loads(original_arguments)["file_path"] == raw
    assert proposal.tool_calls[0]["args"]["file_path"] == raw
    assert "_tinkerfin_file_targets" not in json.dumps(
        [event.model_dump(mode="json") for event in events]
    )
    for name in (
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
    ):
        schema = model.bound_schemas[name]
        argument = "path" if name in {"ls", "glob", "grep"} else "file_path"
        path_field = schema["parameters"]["properties"][argument]
        assert "workspace filesystem instructions" in path_field["description"]
        assert "Must be absolute, not relative" not in json.dumps(schema)
        assert "given absolute path" not in schema["description"]
        if name in {"glob", "grep"}:
            assert path_field["default"] is None
    assert (
        model.bound_schemas["read_file"]["parameters"]["properties"]["offset"][
            "default"
        ]
        == 0
    )
    assert {"file_path", "content"} <= set(
        model.bound_schemas["write_file"]["parameters"]["required"]
    )
    assert "Relative paths start in" in model.inputs[0][0].text
    cloud_model = ToolSchemaModel(responses=[AIMessage(content="done")])
    await (
        TinkerFin()
        .with_namespace("cloud")
        .build(cloud_model)
        .ainvoke(
            thread_id="cloud",
            run_id="cloud",
            input={"messages": [{"role": "user", "content": "hello"}]},
        )
    )
    assert (
        "Must be absolute, not relative"
        in cloud_model.bound_schemas["write_file"]["parameters"]["properties"][
            "file_path"
        ]["description"]
    )
    await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="approved",
            resume=decision(terminal, {"type": "approve"}),
        )
    )
    assert target.read_text() == "report"
    sent_proposal = next(
        message
        for message in model.inputs[-1]
        if isinstance(message, AIMessage)
        and any(call["id"] == "write" for call in message.tool_calls)
    )
    assert sent_proposal.tool_calls[0]["args"]["file_path"] == raw
    sent_result = next(
        message
        for message in model.inputs[-1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "write"
    )
    assert str(target) in sent_result.text
    graph = await tracer.query(runtime.thread_identity("thread"))
    execution = next(node for node in graph.nodes if node.name == "write_file")
    assert execution.request == {"file_path": str(target), "content": "report"}
    assert workspace.resolutions == [(raw, "write"), (str(target), "write")]
    assert workspace.closed == ["first", "approved"]


async def test_edited_target_is_resolved_and_rechecked(tmp_path: Path) -> None:
    workspace = NativeWorkspace(tmp_path / "project")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "write",
                                "name": "write_file",
                                "args": {
                                    "file_path": "original.txt",
                                    "content": "original",
                                },
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            backend=workspace,
            interrupt_on={"write_file": True},
        )
    )
    first = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="first",
            messages=[{"id": "user", "role": "user", "content": "write"}],
        )
    )
    await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="edited",
            resume=decision(
                first[-1],
                {
                    "type": "edit",
                    "edited_action": {
                        "name": "write_file",
                        "args": {"file_path": "~/edited.txt", "content": "edited"},
                    },
                },
            ),
        )
    )
    assert (workspace.home / "edited.txt").read_text() == "edited"
    assert not (workspace.directory / "original.txt").exists()
    assert workspace.resolutions[-2:] == [
        ("~/edited.txt", "write"),
        (str(workspace.home / "edited.txt"), "write"),
    ]


@pytest.mark.parametrize("to_file", [False, True])
async def test_native_review_renames_tools_without_changing_proposal_history(
    tmp_path: Path, to_file: bool
) -> None:
    effects: list[str] = []

    @tool
    async def approved_note(message: str) -> str:
        """Record the approved message."""
        effects.append(message)
        return message

    workspace = NativeWorkspace(tmp_path / "project")
    original = {
        "id": "reviewed",
        "name": "approved_note" if to_file else "write_file",
        "args": {"message": "original"}
        if to_file
        else {"file_path": "original.txt", "content": "original"},
    }
    edited = {
        "name": "write_file" if to_file else "approved_note",
        "args": {"file_path": "~/edited.txt", "content": "edited"}
        if to_file
        else {"message": "edited"},
    }
    model = RecordingModel(
        responses=[
            AIMessage(content="", tool_calls=[original]),
            AIMessage(content="done"),
        ]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .with_observer(tracer)
        .build(
            model=model,
            backend=workspace,
            tools=[approved_note],
            interrupt_on={original["name"]: True},
        )
    )
    await consume(
        runtime.open_run(
            thread_id="thread",
            run_id="first",
            input={"messages": [{"role": "user", "content": "record"}]},
        )
    )
    assert not effects and not (workspace.directory / "original.txt").exists()
    before = await tracer.query(runtime.thread_identity("thread"))
    proposal_node = next(node for node in before.nodes if node.source_id == "reviewed")
    await consume(
        runtime.open_run(
            thread_id="thread",
            run_id="edited",
            input=Command(
                resume={"decisions": [{"type": "edit", "edited_action": edited}]}
            ),
        )
    )
    target = workspace.home / "edited.txt"
    assert effects == ([] if to_file else ["edited"])
    assert not (workspace.directory / "original.txt").exists()
    if to_file:
        assert target.read_text() == "edited"
    else:
        assert not target.exists()
    proposal = next(
        message
        for message in model.inputs[-1]
        if isinstance(message, AIMessage) and message.tool_calls
    )
    assert proposal.tool_calls == [{**original, "type": "tool_call"}]
    result = next(
        message
        for message in model.inputs[-1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "reviewed"
    )
    assert result.status == "success"
    assert f"Executed instead: {edited['name']}" in result.text
    graph = await tracer.query(runtime.thread_identity("thread"))
    execution_nodes = [node for node in graph.nodes if node.source_id == "reviewed"]
    assert len(execution_nodes) == 1
    execution = execution_nodes[0]
    assert execution.id == proposal_node.id
    assert execution.name == edited["name"]
    assert execution.request == (
        {"file_path": str(target), "content": "edited"}
        if to_file
        else {"message": "edited"}
    )
    assert execution.status == "succeeded"


@pytest.mark.parametrize("renamed_first", [False, True])
async def test_edited_tool_names_and_paths_share_parallel_mutation_guard(
    tmp_path: Path, renamed_first: bool
) -> None:
    @tool
    async def approved_note(message: str) -> str:
        """Return the approved message."""
        raise AssertionError("the reviewed replacement must run")

    workspace = NativeWorkspace(tmp_path / "project")
    target = workspace.directory / "report.txt"
    custom = {"id": "renamed", "name": "approved_note", "args": {"message": "note"}}
    file = {
        "id": "file",
        "name": "write_file",
        "args": {"file_path": "unused.txt", "content": "original"},
    }
    proposals = [custom, file] if renamed_first else [file, custom]
    edits = [
        {
            "type": "edit",
            "edited_action": {
                "name": "write_file",
                "args": {
                    "file_path": "report.txt" if index == 0 else str(target),
                    "content": call["id"],
                },
            },
        }
        for index, call in enumerate(proposals)
    ]
    model = RecordingModel(
        responses=[
            AIMessage(content="", tool_calls=proposals),
            AIMessage(content="done"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .build(
            model=model,
            backend=workspace,
            tools=[approved_note],
            interrupt_on={"write_file": True, "approved_note": True},
        )
    )
    await consume(
        runtime.open_run(
            thread_id="thread",
            run_id="first",
            input={"messages": [{"role": "user", "content": "record"}]},
        )
    )
    await consume(
        runtime.open_run(
            thread_id="thread",
            run_id="edited",
            input=Command(resume={"decisions": edits}),
        )
    )
    assert target.read_text() == proposals[0]["id"]
    assert not (workspace.directory / "unused.txt").exists()
    proposal = next(
        message
        for message in model.inputs[-1]
        if isinstance(message, AIMessage) and message.tool_calls
    )
    assert proposal.tool_calls == [{**call, "type": "tool_call"} for call in proposals]
    results = {
        message.tool_call_id: message
        for message in model.inputs[-1]
        if isinstance(message, ToolMessage)
    }
    assert results[proposals[0]["id"]].status == "success"
    assert results[proposals[1]["id"]].status == "error"
    assert "parallel file mutations" in results[proposals[1]["id"]].text


@pytest.mark.parametrize(
    "name,args",
    [
        ("ls", {"path": "."}),
        ("read_file", {"file_path": "file.txt"}),
        (
            "edit_file",
            {"file_path": "file.txt", "old_string": "text", "new_string": "edited"},
        ),
        ("delete", {"file_path": "file.txt"}),
        ("glob", {"pattern": "*.txt"}),
        ("grep", {"pattern": "text"}),
    ],
)
async def test_file_operations_and_omitted_search_roots_use_workspace_paths(
    tmp_path: Path, name: str, args: dict
) -> None:
    workspace = NativeWorkspace(tmp_path / "project")
    (workspace.directory / "file.txt").write_text("text")
    tracer = Tracer()
    runtime = (
        TinkerFin()
        .with_namespace("paths")
        .with_observer(tracer)
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[{"id": "read", "name": name, "args": args}],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            backend=workspace,
        )
    )
    await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="read",
            messages=[{"id": "user", "role": "user", "content": "read"}],
        )
    )
    graph = await tracer.query(runtime.thread_identity("thread"))
    execution = next(node for node in graph.nodes if node.name == name)
    argument = "file_path" if name in {"read_file", "edit_file", "delete"} else "path"
    expected = (
        workspace.directory / "file.txt"
        if argument == "file_path"
        else workspace.directory
    )
    assert isinstance(execution.request, dict)
    assert execution.request[argument] == str(expected)
    assert workspace.resolutions[-1] == (
        str(expected),
        "write" if name in {"edit_file", "delete"} else "read",
    )


async def test_changed_canonical_target_is_rejected_after_approval(
    tmp_path: Path,
) -> None:
    workspace = NativeWorkspace(tmp_path / "project")
    target = workspace.directory / "report.txt"
    outside = tmp_path / "outside.txt"
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "write",
                                "name": "write_file",
                                "args": {
                                    "file_path": "report.txt",
                                    "content": "report",
                                },
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            backend=workspace,
            interrupt_on={"write_file": True},
        )
    )
    first = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="first",
            messages=[{"id": "user", "role": "user", "content": "write"}],
        )
    )
    target.symlink_to(outside)
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="changed",
        resume=decision(first[-1], {"type": "approve"}),
    )
    async with aclosing(stream):
        events = [event async for event in stream]
    assert stream.error is not None
    assert isinstance(events[-1], RunErrorEvent)
    assert not outside.exists()


async def test_resume_without_saved_file_binding_rejects_even_absolute_target(
    tmp_path: Path,
) -> None:
    class MissingBindingSaver(InMemorySaver):
        remove_binding = False

        async def aget_tuple(self, config):
            saved = await super().aget_tuple(config)
            if (
                saved is not None
                and self.remove_binding
                and "_tinkerfin_file_targets" in saved.checkpoint["channel_values"]
            ):
                return saved._replace(
                    checkpoint={
                        **saved.checkpoint,
                        "channel_values": {
                            **saved.checkpoint["channel_values"],
                            "_tinkerfin_file_targets": {},
                        },
                    }
                )
            return saved

    workspace = NativeWorkspace(tmp_path / "project")
    target = workspace.directory / "report.txt"
    saver = MissingBindingSaver()
    runtime = (
        TinkerFin(checkpointer=saver)
        .with_namespace("paths")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "write",
                                "name": "write_file",
                                "args": {"file_path": str(target), "content": "report"},
                            }
                        ],
                    )
                ]
            ),
            backend=workspace,
            interrupt_on={"write_file": True},
        )
    )
    first = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="first",
            messages=[{"id": "user", "role": "user", "content": "write"}],
        )
    )
    saver.remove_binding = True
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="missing",
        resume=decision(first[-1], {"type": "approve"}),
    )
    async with aclosing(stream):
        events = [event async for event in stream]
    assert stream.error is not None and "no saved target binding" in str(stream.error)
    assert isinstance(events[-1], RunErrorEvent)
    assert not target.exists()


async def test_parallel_aliases_keep_history_and_preserve_same_target_mutation_guard(
    tmp_path: Path,
) -> None:
    workspace = NativeWorkspace(tmp_path / "project")
    target = workspace.directory / "report.txt"
    model = RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "first",
                        "name": "write_file",
                        "args": {"file_path": "report.txt", "content": "first"},
                    },
                    {
                        "id": "second",
                        "name": "write_file",
                        "args": {"file_path": str(target), "content": "second"},
                    },
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = TinkerFin().with_namespace("paths").build(model=model, backend=workspace)
    await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="parallel",
            messages=[{"id": "user", "role": "user", "content": "write"}],
        )
    )
    assert target.read_text() == "first"
    proposal = next(
        message for message in model.inputs[-1] if isinstance(message, AIMessage)
    )
    assert [call["args"]["file_path"] for call in proposal.tool_calls] == [
        "report.txt",
        str(target),
    ]
    results = {
        message.tool_call_id: message
        for message in model.inputs[-1]
        if isinstance(message, ToolMessage)
    }
    assert results["first"].status == "success"
    assert (
        results["second"].status == "error"
        and "parallel file mutations" in results["second"].text
    )


async def test_filesystem_request_projection_preserves_tool_command_updates(
    tmp_path: Path,
) -> None:
    class WorkflowState(DeepAgentState):
        checkpoint_marker: str

    @tool
    async def record(runtime: ToolRuntime) -> Command:
        """Record a workflow value together with its tool result."""
        return Command(
            update={
                "checkpoint_marker": "retained",
                "messages": [
                    ToolMessage(content="recorded", tool_call_id=runtime.tool_call_id)
                ],
            }
        )

    workspace = NativeWorkspace(tmp_path / "project")
    runtime = (
        TinkerFin()
        .with_namespace("paths")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[{"id": "record", "name": "record", "args": {}}],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            backend=workspace,
            tools=[record],
            state_schema=WorkflowState,
        )
    )
    result = await runtime.ainvoke(
        thread_id="thread",
        run_id="record",
        input={"messages": [{"role": "user", "content": "record"}]},
    )
    assert result["checkpoint_marker"] == "retained"
    messages = result["messages"]
    assert isinstance(messages, list)
    assert any(
        isinstance(message, ToolMessage)
        and message.tool_call_id == "record"
        and message.text == "recorded"
        for message in messages
    )


async def test_virtual_backend_and_custom_file_tool_keep_their_path_contract() -> None:
    seen: list[str] = []

    @tool
    async def read_file(file_path: str) -> str:
        """Read a business identifier without filesystem interpretation."""
        seen.append(file_path)
        return file_path

    runtime = (
        TinkerFin()
        .with_namespace("virtual")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "custom",
                                "name": "read_file",
                                "args": {"file_path": "~/business/../id"},
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            backend=StateBackend(),
            tools=[read_file],
        )
    )
    await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="read",
            messages=[{"id": "user", "role": "user", "content": "read"}],
        )
    )
    assert seen == ["~/business/../id"]
    virtual = (
        TinkerFin()
        .with_namespace("virtual")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "write",
                                "name": "write_file",
                                "args": {
                                    "file_path": "relative.txt",
                                    "content": "virtual",
                                },
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            backend=StateBackend(),
        )
    )
    result = await virtual.ainvoke(
        thread_id="virtual",
        run_id="write",
        input={"messages": [{"role": "user", "content": "write"}]},
    )
    files = result["files"]
    assert isinstance(files, dict)
    assert "/relative.txt" in files


async def test_named_subagent_inherits_workspace_path_preparation(
    tmp_path: Path,
) -> None:
    workspace = NativeWorkspace(tmp_path / "project")
    child_model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "write",
                        "name": "write_file",
                        "args": {"file_path": "nested.txt", "content": "nested"},
                    }
                ],
            ),
            AIMessage(content="child done"),
        ]
    )
    parent_model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {"description": "write", "subagent_type": "writer"},
                    }
                ],
            ),
            AIMessage(content="parent done"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .build(
            model=parent_model,
            backend=workspace,
            subagents=[
                {
                    "name": "writer",
                    "description": "Write files",
                    "system_prompt": "Write the requested file.",
                    "model": child_model,
                }
            ],
        )
    )
    await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="nested",
            messages=[{"id": "user", "role": "user", "content": "write"}],
        )
    )
    assert (workspace.directory / "nested.txt").read_text() == "nested"
    assert workspace.resolutions == [
        ("nested.txt", "write"),
        (str(workspace.directory / "nested.txt"), "write"),
    ]


@pytest.mark.parametrize("action", ["reject", "cancel"])
async def test_rejected_or_abandoned_file_target_is_never_executed(
    tmp_path: Path, action: str
) -> None:
    workspace = NativeWorkspace(tmp_path / "project")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "write",
                                "name": "write_file",
                                "args": {
                                    "file_path": "report.txt",
                                    "content": "report",
                                },
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            backend=workspace,
            interrupt_on={"write_file": True},
        )
    )
    first = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="first",
            messages=[{"id": "user", "role": "user", "content": "write"}],
        )
    )
    await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="stop",
            resume=decision(
                first[-1],
                None if action == "cancel" else {"type": "reject", "message": "stop"},
            ),
        )
    )
    assert not (workspace.directory / "report.txt").exists()
    assert workspace.resolutions == [("report.txt", "write")]


@pytest.mark.parametrize("failure", ["invalid_input", "invalid_provider_output"])
async def test_failed_resolution_cannot_reach_approval_or_execution(
    tmp_path: Path, failure: str
) -> None:
    class FailedWorkspace(NativeWorkspace):
        async def resolve(
            self, path: str | None, *, operation: Literal["read", "write"]
        ) -> str:
            if failure == "invalid_input":
                raise ValueError("path cannot be resolved")
            return "/invalid/../target"

    workspace = FailedWorkspace(tmp_path / "project")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "write",
                                "name": "write_file",
                                "args": {
                                    "file_path": "report.txt",
                                    "content": "report",
                                },
                            }
                        ],
                    )
                ]
            ),
            backend=workspace,
            interrupt_on={"write_file": True},
        )
    )
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="failed",
        messages=[{"id": "user", "role": "user", "content": "write"}],
    )
    async with aclosing(stream):
        events = [event async for event in stream]
    assert stream.error is not None
    assert isinstance(events[-1], RunErrorEvent)
    assert not any(isinstance(event, RunFinishedEvent) for event in events)
    assert not (workspace.directory / "report.txt").exists()
    assert workspace.closed == ["failed"]


async def test_cancellation_joins_path_resolution_and_releases_workspace(
    tmp_path: Path,
) -> None:
    entered, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class WaitingWorkspace(NativeWorkspace):
        async def resolve(
            self, path: str | None, *, operation: Literal["read", "write"]
        ) -> str:
            entered.set()
            try:
                await release.wait()
                return await super().resolve(path, operation=operation)
            finally:
                stopped.set()

    workspace = WaitingWorkspace(tmp_path / "project")
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "write",
                                "name": "write_file",
                                "args": {
                                    "file_path": "report.txt",
                                    "content": "report",
                                },
                            }
                        ],
                    )
                ]
            ),
            backend=workspace,
        )
    )
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="cancelled",
        messages=[{"id": "user", "role": "user", "content": "write"}],
    )
    consuming = asyncio.create_task(consume(stream))
    try:
        await entered.wait()
        tail = await stream.abort()
        await asyncio.gather(consuming, return_exceptions=True)
        assert stopped.is_set()
        assert workspace.closed == ["cancelled"]
        assert not (workspace.directory / "report.txt").exists()
        assert (
            len(tail) == 1
            and isinstance(tail[0], RunErrorEvent)
            and tail[0].code == "cancelled"
        )
    finally:
        await stream.aclose()
        await asyncio.gather(consuming, return_exceptions=True)


async def test_runtime_offloads_tool_results_and_history_inside_workspace_artifacts(
    tmp_path: Path,
) -> None:
    from test_compaction import RecordingModel, history

    workspace = NativeWorkspace(tmp_path / "project")
    writes: list[str] = []

    class GuardedBackend(FilesystemBackend):
        async def awrite(self, file_path: str, content: str) -> WriteResult:
            assert Path(file_path).is_relative_to(workspace.directory / ".artifacts")
            writes.append(file_path)
            return await super().awrite(file_path, content)

    workspace.backend = CompositeBackend(
        default=GuardedBackend(root_dir=workspace.directory, virtual_mode=False),
        routes={},
        artifacts_root=str(workspace.directory / ".artifacts"),
    )

    @tool
    async def report() -> str:
        """Produce a report that must be retained outside the model context."""
        return "report " * 12000

    model = RecordingModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"id": "report", "name": "report", "args": {}}]
            ),
            AIMessage(content="done"),
            AIMessage(content="summary"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("paths")
        .build(
            model=model,
            backend=workspace,
            tools=[report],
            middleware=[
                SummarizationMiddleware(
                    model,
                    backend=workspace.backend,
                    trigger=("tokens", 10000),
                    keep=("messages", 1),
                )
            ],
        )
    )
    await runtime.ainvoke(
        thread_id="thread", run_id="report", input={"messages": history()}
    )
    result = await runtime.compact(thread_id="thread", run_id="compact")
    assert result.status == "compacted"
    assert any("/large_tool_results/" in path for path in writes)
    assert any("/conversation_history/" in path for path in writes)
    assert all(Path(path).is_file() for path in writes)
