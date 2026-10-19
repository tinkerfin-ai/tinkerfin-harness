"""Portable tool declarations retain path intent and revoke borrowed access."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

from tinkerfin_contracts import (
    FilePath,
    RunIdentity,
    ToolDescriptor,
    ToolExecutionScope,
    ToolResult,
    bind_workspace_operation,
    borrow_tool_context,
    current_workspace_operation,
    next_workspace_operation,
    resolve_tool_arguments,
    workspace_operation,
)


def scope() -> ToolExecutionScope:
    return ToolExecutionScope(
        identity=RunIdentity(namespace="owner", thread_id="thread", run_id="resume"),
        origin_identity=RunIdentity(
            namespace="owner", thread_id="thread", run_id="proposal"
        ),
        workspace_scope_id="authorized-workspace",
        invocation_id="original-call",
        graph_namespace=("tools:task",),
        purpose="tool",
        tool_call_id="call",
    )


def test_path_annotations_survive_nested_discriminated_tool_schemas() -> None:
    class Read(BaseModel):
        kind: Literal["read"]
        files: list[Annotated[str, FilePath("read")]]

    class Write(BaseModel):
        kind: Literal["write"]
        file: Annotated[str, FilePath("write")]

    class Input(BaseModel):
        actions: list[Annotated[Read | Write, Field(discriminator="kind")]]

    schema = Input.model_json_schema()
    assert schema["$defs"]["Read"]["properties"]["files"]["items"][
        "x-tinkerfin-file-path"
    ] == {"operation": "read", "default_root": False}
    assert schema["$defs"]["Write"]["properties"]["file"]["x-tinkerfin-file-path"] == {
        "operation": "write",
        "default_root": False,
    }
    parsed = Input.model_validate(
        {"actions": [{"kind": "write", "file": "report.txt"}]}
    ).actions[0]
    assert isinstance(parsed, Write) and parsed.file == "report.txt"


def test_provider_steps_replay_in_order_with_independent_named_branches() -> None:
    runs = []
    for _ in range(2):
        with bind_workspace_operation(scope()):
            sequential = [next_workspace_operation().steps for _ in range(3)]
            with workspace_operation("left"):
                left = next_workspace_operation().steps
            with workspace_operation("right"):
                right = next_workspace_operation().steps
            assert len(set((*sequential, left, right))) == 5
            runs.append((sequential, left, right))
    assert runs[0] == runs[1]


async def test_operation_scopes_are_independent_and_inherited_access_is_revoked() -> (
    None
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def escaped() -> None:
        assert current_workspace_operation().origin_identity.run_id == "proposal"
        entered.set()
        await release.wait()
        with pytest.raises(RuntimeError, match="active managed operation"):
            current_workspace_operation()

    with bind_workspace_operation(scope()):
        with workspace_operation("read-before"):
            before = current_workspace_operation()
        with workspace_operation("read-after"):
            after = current_workspace_operation()
        assert before.steps != after.steps
        assert before.invocation_id == after.invocation_id
        child = asyncio.create_task(escaped())
        await entered.wait()
    try:
        release.set()
        await child
    finally:
        if not child.done():
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)


async def test_tool_context_revokes_workspace_and_progress_without_graph_state() -> (
    None
):
    workspace = object()
    outputs = []

    async def emit(value):
        outputs.append(value)

    with borrow_tool_context(workspace, scope(), emit) as context:
        assert context.workspace is workspace
        assert context.identity.run_id == "resume"
        assert context.scope.origin_identity.run_id == "proposal"
        await context.emit_output({"progress": "ready"})
    assert outputs == [{"progress": "ready"}]
    with pytest.raises(RuntimeError, match="borrow has ended"):
        _ = context.workspace
    with pytest.raises(RuntimeError, match="borrow has ended"):
        await context.emit_output("late")


@pytest.mark.parametrize("value", [float("nan"), {"nested": float("inf")}, object()])
def test_tool_results_reject_nonportable_values(value: object) -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate({"value": value})


async def test_declared_paths_resolve_nested_variants_without_rewriting_the_request() -> (
    None
):
    class Read(BaseModel):
        kind: Literal["read"]
        files: list[Annotated[str, FilePath("read")]]

    class Write(BaseModel):
        kind: Literal["write"]
        file: Annotated[str, FilePath("write")]

    class Input(BaseModel):
        actions: list[Annotated[Read | Write, Field(discriminator="kind")]]

    descriptor = ToolDescriptor(
        name="batch", description="Handle files", input_schema=Input.model_json_schema()
    )
    arguments = {
        "actions": [
            {"kind": "write", "file": "report.txt"},
            {"kind": "read", "files": ["source.txt", "other.txt"]},
        ]
    }
    resolved = []

    async def resolver(path, *, operation):
        resolved.append((path, operation))
        return "/work/" + path

    effective = await resolve_tool_arguments(descriptor, arguments, resolver)
    assert arguments["actions"][0]["file"] == "report.txt"
    assert effective == {
        "actions": [
            {"kind": "write", "file": "/work/report.txt"},
            {"kind": "read", "files": ["/work/source.txt", "/work/other.txt"]},
        ]
    }
    assert resolved == [
        ("report.txt", "write"),
        ("source.txt", "read"),
        ("other.txt", "read"),
    ]


def test_tool_schema_reference_validation_keeps_literal_default_data() -> None:
    descriptor = ToolDescriptor(
        name="literal",
        description="Reference-shaped data.",
        input_schema={
            "type": "object",
            "properties": {
                "value": {
                    "type": "object",
                    "default": {"$ref": "https://example.test/data"},
                }
            },
        },
    )
    assert descriptor.input_schema["type"] == "object"


async def test_nested_context_inherits_output_and_owner_revokes_escaped_child() -> None:
    from tinkerfin_contracts import emit_workspace_output

    outputs = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def emit(value):
        outputs.append(value)

    async def child() -> None:
        with workspace_operation("branch"):
            with borrow_tool_context(object(), scope()) as context:
                await context.emit_output("context")
                await emit_workspace_output("provider")
                entered.set()
                await release.wait()
                with pytest.raises(RuntimeError, match="borrow has ended"):
                    _ = context.workspace
                with pytest.raises(RuntimeError, match="active managed operation"):
                    current_workspace_operation()
                with pytest.raises(RuntimeError, match="active managed operation"):
                    await emit_workspace_output("escaped")

    with bind_workspace_operation(scope(), emit_output=emit):
        task = asyncio.create_task(child())
        await entered.wait()
    try:
        release.set()
        await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert outputs == ["context", "provider"]


async def test_declared_default_file_targets_are_bound_without_mutating_input() -> None:
    class Output(BaseModel):
        path: Annotated[str, FilePath("write")] = "report.txt"

    class Input(BaseModel):
        output: Output = Output()
        unselected: str = "keep omitted"

    descriptor = ToolDescriptor(
        name="report",
        description="Write a report.",
        input_schema=Input.model_json_schema(),
    )
    requested = {}

    async def resolve(path, *, operation):
        assert path == "report.txt" and operation == "write"
        return "/workspace/report.txt"

    assert await resolve_tool_arguments(descriptor, requested, resolve) == {
        "output": {"path": "/workspace/report.txt"}
    }
    assert requested == {}


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_tool_descriptor_rejects_external_references_without_file_annotations(
    keyword: str,
) -> None:
    with pytest.raises(ValidationError, match="local"):
        ToolDescriptor(
            name="unsafe",
            description="No file annotations.",
            input_schema={
                "type": "object",
                "properties": {"value": {keyword: "https://example.test/value.json"}},
            },
        )


def test_local_schema_reference_cannot_hide_external_resolution_in_literal_data() -> (
    None
):
    with pytest.raises(ValidationError, match="local"):
        ToolDescriptor(
            name="indirect",
            description="References must remain local.",
            input_schema={
                "$ref": "#/examples/0",
                "examples": [{"$ref": "https://example.test/external"}],
            },
        )


def test_local_schema_references_do_not_change_resource_roots() -> None:
    with pytest.raises(ValidationError, match="one local reference document"):
        ToolDescriptor(
            name="resources",
            description="One local schema root.",
            input_schema={
                "$defs": {"child": {"$id": "child", "type": "string"}},
                "$ref": "#/$defs/child",
            },
        )
