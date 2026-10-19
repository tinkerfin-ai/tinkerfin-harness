"""Prepared capabilities participate in the cloud Agent's existing lifecycle."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import pytest
from ag_ui.core import RunFinishedEvent
from deepagents.backends.protocol import BackendProtocol
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field, JsonValue
from test_compaction import RecordingModel
from test_workspace_file_paths import (
    NativeWorkspace,
    ToolSchemaModel,
    consume,
    decision,
)

from tinkerfin import SkillSource, TinkerFin, WorkspaceSkills
from tinkerfin.subagents import SubAgent
from tinkerfin_contracts import (
    FilePath,
    PreparedWorkspace,
    RunIdentity,
    ToolDescriptor,
    ToolExecutionScope,
    ToolResult,
    WorkspaceTool,
    current_workspace_operation,
    emit_workspace_output,
    next_workspace_operation,
    workspace_operation,
)


class ReadFiles(BaseModel):
    kind: Literal["read"]
    paths: list[Annotated[str, FilePath("read")]]


class WriteFile(BaseModel):
    kind: Literal["write"]
    path: Annotated[str, FilePath("write")]


class FileAction(BaseModel):
    action: Annotated[ReadFiles | WriteFile, Field(discriminator="kind")]


class ProvidingWorkspace(NativeWorkspace):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.effects: list[dict[str, JsonValue]] = []
        self.origins: list[ToolExecutionScope] = []
        self.calls: list[ToolExecutionScope] = []
        self.sources: tuple[SkillSource, ...] = ()
        self.workspace_scope_id = "authorized-project"
        self.descriptor = ToolDescriptor(
            name="inspect_paths",
            description="Inspect the selected filesystem inputs.",
            input_schema=FileAction.model_json_schema(),
        )

    async def resolve(
        self, path: str | None, *, operation: Literal["read", "write"]
    ) -> str:
        self.origins.append(current_workspace_operation())
        return await super().resolve(path, operation=operation)

    async def invoke(self, arguments: Mapping[str, JsonValue]) -> ToolResult:
        self.effects.append(dict(arguments))
        self.origins.append(current_workspace_operation())
        self.calls.extend(next_workspace_operation() for _ in range(3))
        with workspace_operation("report-progress"):
            await emit_workspace_output({"phase": "inspected"})
        return ToolResult(value={"accepted": True})

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[Path, BackendProtocol]]:
        try:
            yield PreparedWorkspace(
                self.directory,
                self.backend,
                file_path_resolver=self.resolve,
                tools=(WorkspaceTool(self.descriptor, self.invoke),),
                skill_sources=self.sources,
                workspace_scope_id=self.workspace_scope_id,
            )
        finally:
            self.closed.append(identity.run_id)


async def test_patterned_file_arguments_use_the_reviewed_target_at_execution(
    tmp_path: Path,
) -> None:
    class Input(BaseModel):
        files: dict[
            Annotated[str, Field(pattern="^file_")],
            Annotated[str, FilePath("read")],
        ]

    workspace = ProvidingWorkspace(tmp_path / "project")
    workspace.descriptor = ToolDescriptor(
        name="inspect_paths",
        description="Inspect selected files.",
        input_schema=Input.model_json_schema(),
    )
    requested = {"files": {"file_report": "report.md"}}
    model = RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "inspect",
                        "name": "inspect_paths",
                        "args": requested,
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("workspace")
        .build(model=model, backend=workspace, interrupt_on={"inspect_paths": True})
    )
    events = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="proposal",
            messages=[{"id": "user", "role": "user", "content": "inspect"}],
        )
    )
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    metadata = terminal.outcome.interrupts[0].metadata
    assert metadata is not None
    target = str(workspace.directory / "report.md")
    assert metadata["deepagents"]["originalArgs"] == {"files": {"file_report": target}}
    assert workspace.effects == []
    await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="approved",
            resume=decision(terminal, {"type": "approve"}),
        )
    )
    assert workspace.effects == [{"files": {"file_report": target}}]
    assert workspace.resolutions == [("report.md", "read"), (target, "read")]
    assert requested == {"files": {"file_report": "report.md"}}


@pytest.mark.parametrize("failure", ["constraint", "changed_access"])
async def test_invalid_resolved_arguments_never_execute_the_raw_proposal(
    tmp_path: Path,
    failure: str,
) -> None:
    read: dict[str, JsonValue] = {
        "type": "string",
        "pattern": "^[^/]",
        "x-tinkerfin-file-path": {"operation": "read", "default_root": False},
    }
    path_schema: dict[str, JsonValue] = (
        read
        if failure == "constraint"
        else {
            "oneOf": [
                read,
                {
                    "type": "string",
                    "pattern": "^/",
                    "x-tinkerfin-file-path": {
                        "operation": "write",
                        "default_root": False,
                    },
                },
            ],
        }
    )
    workspace = ProvidingWorkspace(tmp_path / "project")
    workspace.descriptor = ToolDescriptor(
        name="inspect_paths",
        description="Inspect selected files.",
        input_schema={"type": "object", "properties": {"file": path_schema}},
    )
    runtime = (
        TinkerFin()
        .with_namespace("workspace")
        .build(
            model=RecordingModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "inspect",
                                "name": "inspect_paths",
                                "args": {"file": "report.md"},
                            }
                        ],
                    )
                ]
            ),
            backend=workspace,
        )
    )
    with pytest.raises(ValueError, match="resolved"):
        await runtime.ainvoke(
            thread_id="thread",
            run_id="proposal",
            input={"messages": [{"role": "user", "content": "inspect"}]},
        )
    assert workspace.effects == []
    assert workspace.resolutions == [("report.md", "read")]
    assert workspace.closed == ["proposal"]


@pytest.mark.parametrize(
    ("name", "reference"),
    [
        ("a b", "#/$defs/a%20b"),
        ("a/b~c", "#/$defs/a~1b~0c"),
        ("a%20b", "#/$defs/a%2520b"),
    ],
)
async def test_tool_schema_references_and_literal_defaults_reach_model_binding_intact(
    tmp_path: Path,
    name: str,
    reference: str,
) -> None:
    from jsonschema import Draft202012Validator

    workspace = ProvidingWorkspace(tmp_path / "project")
    literal: JsonValue = {"$ref": "https://example.test/literal"}
    workspace.descriptor = ToolDescriptor(
        name="inspect_paths",
        description="Inspect selected files.",
        input_schema={
            "type": "object",
            "$defs": {
                name: {
                    "type": "string",
                    "x-tinkerfin-file-path": {
                        "operation": "read",
                        "default_root": False,
                    },
                }
            },
            "properties": {
                "file": {"$ref": reference},
                "literal": {"type": "object", "default": literal},
            },
        },
    )
    model = ToolSchemaModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "inspect",
                        "name": "inspect_paths",
                        "args": {"file": "report.md"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = (
        TinkerFin().with_namespace("workspace").build(model=model, backend=workspace)
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="inspect",
        input={"messages": [{"role": "user", "content": "inspect"}]},
    )
    bound = model.bound_schemas["inspect_paths"]["parameters"]
    assert bound["properties"]["literal"]["default"] == literal
    assert bound["$defs"][name]["x-tinkerfin-file-path"]["operation"] == "read"
    assert Draft202012Validator(bound).is_valid({"file": "report.md"})
    assert workspace.effects == [{"file": str(workspace.directory / "report.md")}]


async def test_declared_model_tool_keeps_literal_reference_defaults_and_bound_paths(
    tmp_path: Path,
) -> None:
    class Input(BaseModel):
        file: Annotated[str, FilePath("read")]
        literal: dict[str, str] = {"$ref": "https://example.test/literal"}

    effects = []

    @tool(args_schema=Input)
    async def inspect_file(file: str, literal: dict[str, str]) -> str:
        """Return the selected file while retaining literal options."""
        effects.append((file, literal))
        return file

    workspace = NativeWorkspace(tmp_path / "project")
    literal = {"$ref": "https://example.test/literal"}
    model = ToolSchemaModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "inspect",
                        "name": "inspect_file",
                        "args": {"file": "report.md", "literal": literal},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = (
        TinkerFin()
        .with_namespace("workspace")
        .build(
            model=model,
            backend=workspace,
            tools=[inspect_file],
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="inspect",
        input={"messages": [{"role": "user", "content": "inspect"}]},
    )
    assert effects == [(str(workspace.directory / "report.md"), literal)]
    assert (
        model.bound_schemas["inspect_file"]["parameters"]["properties"]["literal"][
            "default"
        ]
        == literal
    )


def proposal() -> AIMessage:
    return AIMessage(
        id="proposal",
        content="",
        tool_calls=[
            {
                "id": "inspect",
                "name": "inspect_paths",
                "args": {"action": {"kind": "read", "paths": ["a.txt", "b.txt"]}},
            }
        ],
    )


@pytest.mark.parametrize("resolution", ["approve", "edit", "reject", "cancel"])
async def test_prepared_nested_paths_keep_origin_across_review_and_runtime_rebuild(
    tmp_path: Path, resolution: str
) -> None:
    workspace = ProvidingWorkspace(tmp_path / "project")
    model = RecordingModel(responses=[proposal(), AIMessage(content="done")])
    saver = InMemorySaver()

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("workspace")
            .build(model=model, backend=workspace, interrupt_on={"inspect_paths": True})
        )

    first = build()
    events = await consume(
        first.open_agui_run(
            thread_id="thread",
            run_id="propose",
            messages=[{"id": "user", "role": "user", "content": "inspect"}],
        )
    )
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    metadata = terminal.outcome.interrupts[0].metadata
    assert metadata is not None
    assert metadata["deepagents"]["originalArgs"] == {
        "action": {
            "kind": "read",
            "paths": [
                str(workspace.directory / "a.txt"),
                str(workspace.directory / "b.txt"),
            ],
        }
    }
    assert workspace.effects == []
    if resolution == "edit":
        payload = {
            "type": "edit",
            "edited_action": {
                "name": "inspect_paths",
                "args": {"action": {"kind": "write", "path": "edited.txt"}},
            },
        }
    elif resolution == "cancel":
        payload = None
    elif resolution == "reject":
        payload = {"type": "reject", "message": "Do not inspect."}
    else:
        payload = {"type": "approve"}
    second = build()
    resumed = await consume(
        second.open_agui_run(
            thread_id="thread", run_id="decision", resume=decision(terminal, payload)
        )
    )
    if resolution in {"reject", "cancel"}:
        assert workspace.effects == []
    else:
        expected = (
            {
                "action": {
                    "kind": "write",
                    "path": str(workspace.directory / "edited.txt"),
                }
            }
            if resolution == "edit"
            else metadata["deepagents"]["originalArgs"]
        )
        assert workspace.effects == [expected]
        assert {item.origin_identity.run_id for item in workspace.origins} == {
            "propose"
        }
        assert {item.invocation_id for item in workspace.origins} == {
            workspace.origins[0].invocation_id
        }
        assert workspace.origins[-1].identity.run_id == "decision"
        assert all(
            item.workspace_scope_id == "authorized-project"
            for item in workspace.origins
        )
        assert [item.steps[-1] for item in workspace.calls] == [
            "operation:1",
            "operation:2",
            "operation:3",
        ]
        assert any("inspected" in event.model_dump_json() for event in resumed)
        raw = next(
            item
            for item in model.inputs[-1]
            if isinstance(item, AIMessage) and item.id == "proposal"
        )
        assert raw.tool_calls[0]["args"] == proposal().tool_calls[0]["args"]
        result = next(
            item
            for item in model.inputs[-1]
            if isinstance(item, ToolMessage) and item.tool_call_id == "inspect"
        )
        assert str(result.content).endswith(json.dumps({"accepted": True}))
        assert result.status == "success"
    assert workspace.closed == ["propose", "decision"]


@pytest.mark.parametrize("role", ["specialist", "general-purpose"])
async def test_prepared_tools_and_real_skill_roots_follow_cloud_role_selections(
    tmp_path: Path, role: str
) -> None:
    workspace = ProvidingWorkspace(tmp_path / "project")
    directory = workspace.directory / "actual-cached-skills"
    for name in ("research", "summarize", "restricted"):
        path = directory / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} instructions\n---\nUse {name}.\n"
        )
    workspace.sources = (SkillSource(str(directory), names=("research", "summarize")),)
    task = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "delegate",
                "name": "task",
                "args": {"subagent_type": role, "description": "inspect"},
            }
        ],
    )
    subagents: list[SubAgent]
    if role == "specialist":
        parent = RecordingModel(responses=[task, AIMessage(content="done")])
        child = RecordingModel(responses=[proposal(), AIMessage(content="inspected")])
        subagents = [
            {
                "name": role,
                "description": "Research selected inputs.",
                "system_prompt": "Do the research.",
                "model": child,
                "skills": [WorkspaceSkills(names=("research", "restricted"))],
            }
        ]
    else:
        parent = child = RecordingModel(
            responses=[
                task,
                proposal(),
                AIMessage(content="inspected"),
                AIMessage(content="done"),
            ]
        )
        subagents = []
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("workspace")
        .build(model=parent, backend=workspace, subagents=subagents)
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "inspect"}]},
    )
    assert len(workspace.effects) == 1
    assert workspace.origins[-1].graph_namespace
    parent_system = str(parent.inputs[0][0].content)
    child_system = str(child.inputs[0 if role == "specialist" else 1][0].content)
    assert (
        "research instructions" in parent_system
        and "summarize instructions" in parent_system
    )
    assert "research instructions" in child_system
    assert ("summarize instructions" in child_system) is (role == "general-purpose")
    assert "restricted instructions" not in parent_system + child_system
    assert str(directory / "research" / "SKILL.md") in child_system


@pytest.mark.parametrize("name", ["read_file", "task", "compact_conversation"])
async def test_prepared_tools_cannot_shadow_framework_tools(
    tmp_path: Path, name: str
) -> None:
    workspace = ProvidingWorkspace(tmp_path / "project")
    workspace.descriptor = workspace.descriptor.model_copy(update={"name": name})
    model = RecordingModel(responses=[AIMessage(content="unused")])
    runtime = (
        TinkerFin().with_namespace("workspace").build(model=model, backend=workspace)
    )
    with pytest.raises(ValueError, match="conflicts with an agent tool"):
        await runtime.ainvoke(
            thread_id="thread",
            run_id="run",
            input={"messages": [{"role": "user", "content": "inspect"}]},
        )
    assert model.inputs == [] and workspace.effects == []


async def test_framework_file_io_keeps_saved_origin_and_archive_path_after_model_failure(
    tmp_path: Path,
) -> None:
    from deepagents.backends import CompositeBackend, FilesystemBackend
    from deepagents.backends.protocol import (
        EditResult,
        FileDownloadResponse,
        LsResult,
        WriteResult,
    )
    from deepagents.middleware.summarization import SummarizationMiddleware
    from test_compaction import history

    workspace = ProvidingWorkspace(tmp_path / "project")
    operations: list[tuple[str, str, ToolExecutionScope]] = []

    class RecordingBackend(FilesystemBackend):
        async def awrite(self, file_path: str, content: str) -> WriteResult:
            operations.append(("write", file_path, next_workspace_operation()))
            return await super().awrite(file_path, content)

        async def aedit(
            self,
            file_path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ) -> EditResult:
            operations.append(("edit", file_path, next_workspace_operation()))
            return await super().aedit(file_path, old_string, new_string, replace_all)

        async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
            operations.append(("download", str(paths), next_workspace_operation()))
            return await super().adownload_files(paths)

        async def als(self, path: str) -> LsResult:
            operations.append(("list", path, next_workspace_operation()))
            return await super().als(path)

    class FailingOnceModel(RecordingModel):
        fail_once: bool = True

        async def _agenerate(self, *args, **kwargs):
            if self.fail_once:
                self.fail_once = False
                raise ValueError("controlled model failure")
            return await super()._agenerate(*args, **kwargs)

    directory = workspace.directory / "skills"
    skill = directory / "research"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: research\ndescription: Research instructions\n---\nRead sources.\n"
    )
    memory = workspace.directory / "MEMORY.md"
    memory.write_text("Remember the project.")
    workspace.sources = (SkillSource(str(directory)),)
    workspace.backend = CompositeBackend(
        default=RecordingBackend(root_dir=workspace.directory, virtual_mode=False),
        routes={},
        artifacts_root=str(workspace.directory / ".artifacts"),
    )
    model = FailingOnceModel(responses=[AIMessage(content="done")])
    summary = RecordingModel(responses=[AIMessage(content="Saved requirements.")])
    saver = InMemorySaver()

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("workspace")
            .build(
                model=model,
                backend=workspace,
                memory=[str(memory)],
                middleware=[
                    SummarizationMiddleware(
                        summary,
                        backend=workspace.backend,
                        trigger=("tokens", 1),
                        keep=("messages", 1),
                    )
                ],
            )
        )

    with pytest.raises(ValueError, match="controlled model failure"):
        await build().ainvoke(
            thread_id="thread", run_id="original", input={"messages": history()}
        )
    before = [item for item in operations if item[0] == "write"]
    assert len(before) == 1 and before[0][2].purpose == "compaction"
    assert before[0][2].tool_call_id is None
    await build().ainvoke(thread_id="thread", run_id="retry", input=None)
    writes = [item for item in operations if item[0] in {"write", "edit"}]
    assert len(writes) == 2
    assert writes[0][1] == writes[1][1]
    assert writes[0][2].invocation_id == writes[1][2].invocation_id
    assert writes[0][2].steps == writes[1][2].steps
    assert writes[0][2].origin_identity == writes[1][2].origin_identity
    assert writes[1][2].identity.run_id == "retry"
    assert {item[2].purpose for item in operations} >= {
        "skills",
        "memory",
        "compaction",
    }
    assert workspace.closed == ["original", "retry"]


async def test_review_cannot_execute_after_workspace_authorization_scope_changes(
    tmp_path: Path,
) -> None:
    from contextlib import aclosing

    from tinkerfin import TinkerFinLifecycleError

    workspace = ProvidingWorkspace(tmp_path / "project")
    model = RecordingModel(responses=[proposal(), AIMessage(content="done")])
    saver = InMemorySaver()

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("workspace")
            .build(model=model, backend=workspace, interrupt_on={"inspect_paths": True})
        )

    first = await consume(
        build().open_agui_run(
            thread_id="thread",
            run_id="propose",
            messages=[{"id": "user", "role": "user", "content": "inspect"}],
        )
    )
    workspace.workspace_scope_id = "different-authorization"
    stream = build().open_agui_run(
        thread_id="thread",
        run_id="decision",
        resume=decision(first[-1], {"type": "approve"}),
    )
    async with aclosing(stream):
        _events = [event async for event in stream]
    assert isinstance(stream.error, TinkerFinLifecycleError)
    assert workspace.effects == []
    assert len(workspace.resolutions) == 2


async def test_invalid_prepared_tool_arguments_return_feedback_before_any_effect(
    tmp_path: Path,
) -> None:
    workspace = ProvidingWorkspace(tmp_path / "project")
    invalid = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "invalid",
                "name": "inspect_paths",
                "args": {"action": {"kind": "read", "paths": "not-a-list"}},
            }
        ],
    )
    model = RecordingModel(responses=[invalid, proposal(), AIMessage(content="done")])
    runtime = (
        TinkerFin().with_namespace("workspace").build(model=model, backend=workspace)
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "inspect"}]},
    )
    feedback = next(item for item in model.inputs[1] if isinstance(item, ToolMessage))
    assert feedback.tool_call_id == "invalid" and feedback.status == "error"
    assert str(feedback.content).startswith("Invalid tool arguments:")
    assert len(workspace.effects) == 1
    assert all(path != "not-a-list" for path, _operation in workspace.resolutions)


async def test_prepared_tools_cannot_replace_plan_controls(tmp_path: Path) -> None:
    workspace = ProvidingWorkspace(tmp_path / "project")
    workspace.descriptor = workspace.descriptor.model_copy(
        update={"name": "submit_plan"}
    )
    model = RecordingModel(responses=[AIMessage(content="unused")])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("workspace")
        .with_plan(enabled=True)
        .build(model=model, backend=workspace)
    )
    with pytest.raises(ValueError, match="conflicts with an agent tool"):
        await runtime.ainvoke(
            thread_id="thread",
            run_id="run",
            mode="plan",
            input={"messages": [{"role": "user", "content": "plan"}]},
        )
    assert model.inputs == [] and workspace.effects == []


@pytest.mark.parametrize("kind", ["image", "attachment"])
async def test_prepared_tool_media_reaches_model_and_attachment_events(
    tmp_path: Path, kind: str
) -> None:
    import base64

    from tinkerfin import AttachmentContent, AttachmentSupport
    from tinkerfin_contracts.media import Attachment

    picture = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQm8AAAAASUVORK5CYII="
    attachment = Attachment(
        id="saved-report", name="report.pdf", mime_type="application/pdf", size_bytes=14
    )
    blocks: list[JsonValue] = [{"type": "text", "text": "Captured output"}]
    blocks.append(
        {"type": "image", "base64": picture, "mime_type": "image/png"}
        if kind == "image"
        else attachment.content_block()
    )

    class MediaWorkspace(ProvidingWorkspace):
        async def invoke(self, arguments: Mapping[str, JsonValue]) -> ToolResult:
            return ToolResult(value=blocks)

    reads = []

    async def read_content(requested: Attachment) -> AttachmentContent:
        assert current_workspace_operation().purpose == "media"
        reads.append(requested)
        return AttachmentContent(b"%PDF-1.4\n%%EOF", "application/pdf")

    workspace = MediaWorkspace(tmp_path / "project")
    model = RecordingModel(
        responses=[proposal(), AIMessage(content="Observed output.")]
    )
    runtime = (
        TinkerFin()
        .with_namespace("workspace")
        .with_attachments(
            AttachmentSupport(
                read_content=read_content, supports_content=lambda _model, _mime: True
            )
        )
        .build(model=model, backend=workspace)
    )
    events = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            messages=[{"role": "user", "id": "user", "content": "inspect"}],
        )
    )
    input_blocks = [
        block
        for message in model.inputs[-1]
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, dict)
    ]
    if kind == "image":
        assert any(
            block.get("type") == "image" and block.get("base64") == picture
            for block in input_blocks
        )
        assert reads == []
    else:
        assert reads == [attachment]
        expected = base64.b64encode(b"%PDF-1.4\n%%EOF").decode()
        assert any(
            block.get("type") == "file" and block.get("base64") == expected
            for block in input_blocks
        )
        result = next(
            event for event in events if event.type.value == "TOOL_CALL_RESULT"
        )
        assert result.model_dump(mode="json")["attachments"] == [
            attachment.model_dump(mode="json")
        ]


async def test_new_turn_reads_completed_checkpoint_with_old_private_file_binding(
    tmp_path: Path,
) -> None:
    from copy import deepcopy

    workspace = NativeWorkspace(tmp_path / "project")
    first_model = RecordingModel(
        responses=[
            AIMessage(
                id="old-proposal",
                content="",
                tool_calls=[
                    {
                        "id": "old-write",
                        "name": "write_file",
                        "args": {"file_path": "old.txt", "content": "original"},
                    }
                ],
            ),
            AIMessage(id="old-answer", content="Stored."),
        ]
    )
    saver = InMemorySaver()
    await (
        TinkerFin(checkpointer=saver)
        .with_namespace("workspace")
        .build(model=first_model, backend=workspace)
        .ainvoke(
            thread_id="thread",
            run_id="old-run",
            input={
                "messages": [{"id": "old-user", "role": "user", "content": "write old"}]
            },
        )
    )
    stored = [item async for item in saver.alist(None)]
    head = max(
        (
            item
            for item in stored
            if item.config.get("configurable", {}).get("checkpoint_ns", "") == ""
        ),
        key=lambda item: item.checkpoint["id"],
    )
    assert not head.pending_writes
    checkpoint = deepcopy(head.checkpoint)
    origin_keys = {
        "_tinkerfin_tool_origins",
        "_tinkerfin_workspace_origin",
        "_tinkerfin_file_contract",
    }
    for name in origin_keys:
        checkpoint["channel_values"].pop(name, None)
        checkpoint["channel_versions"].pop(name, None)
    for node, seen in list(checkpoint["versions_seen"].items()):
        if node.startswith("ToolOriginsMiddleware."):
            del checkpoint["versions_seen"][node]
        else:
            for name in origin_keys:
                seen.pop(name, None)
    channel = "_tinkerfin_file_targets"
    checkpoint["channel_values"][channel] = {
        "old-write": {
            "message_id": "old-proposal",
            "tool_name": "write_file",
            "requested_path": "old.txt",
            "resolved_path": str(workspace.directory / "old.txt"),
        }
    }
    prior_version = checkpoint["channel_versions"].get(channel)
    assert isinstance(prior_version, str)
    version = saver.get_next_version(prior_version, None)
    checkpoint["channel_versions"][channel] = version
    assert head.parent_config is not None
    rewritten = await saver.aput(
        head.parent_config, checkpoint, head.metadata, {channel: version}
    )
    decoded = await saver.aget_tuple(rewritten)
    assert (
        decoded is not None
        and decoded.checkpoint["channel_values"][channel]
        == checkpoint["channel_values"][channel]
    )
    assert not origin_keys.intersection(decoded.checkpoint["channel_values"])
    old_resolutions = list(workspace.resolutions)
    current_model = RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "new-write",
                        "name": "write_file",
                        "args": {"file_path": "new.txt", "content": "current"},
                    }
                ],
            ),
            AIMessage(content="Continued."),
        ]
    )
    await (
        TinkerFin(checkpointer=saver)
        .with_namespace("workspace")
        .build(model=current_model, backend=workspace)
        .ainvoke(
            thread_id="thread",
            run_id="new-run",
            input={
                "messages": [{"id": "new-user", "role": "user", "content": "write new"}]
            },
        )
    )
    assert (workspace.directory / "old.txt").read_text() == "original"
    assert (workspace.directory / "new.txt").read_text() == "current"
    assert workspace.resolutions == [
        *old_resolutions,
        ("new.txt", "write"),
        (str(workspace.directory / "new.txt"), "write"),
    ]
    assert any(
        isinstance(item, AIMessage) and item.id == "old-proposal"
        for item in current_model.inputs[0]
    )
    assert any(
        isinstance(item, ToolMessage) and item.tool_call_id == "old-write"
        for item in current_model.inputs[0]
    )
