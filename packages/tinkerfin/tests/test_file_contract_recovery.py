"""Approval and tool recovery preserve the proposal's declared file access."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from ag_ui.core import RunErrorEvent, RunFinishedEvent
from deepagents.backends.protocol import BackendProtocol
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import JsonValue
from test_prepared_workspace_tools import ProvidingWorkspace, RecordingModel, proposal
from test_workspace_file_paths import consume, decision

from tinkerfin import TinkerFin
from tinkerfin.errors import TinkerFinLifecycleError
from tinkerfin_contracts import PreparedWorkspace, RunIdentity, ToolDescriptor
from tinkerfin_native_stream import NativeRuntimeInterrupt


class ChangingWorkspace(ProvidingWorkspace):
    include_resolver = True

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[Path, BackendProtocol]]:
        async with super().prepare(identity) as prepared:
            yield (
                prepared
                if self.include_resolver
                else replace(prepared, file_path_resolver=None)
            )


def change_declaration(
    workspace: ChangingWorkspace,
    change: Literal["operation", "annotations", "resolver"],
) -> None:
    if change == "resolver":
        workspace.include_resolver = False
        return
    schema = deepcopy(workspace.descriptor.input_schema)
    if change == "operation":
        value: JsonValue = schema
        for key in (
            "$defs",
            "ReadFiles",
            "properties",
            "paths",
            "items",
            "x-tinkerfin-file-path",
        ):
            assert isinstance(value, dict)
            value = value[key]
        assert isinstance(value, dict)
        value["operation"] = "write"
    else:

        def remove_annotations(value: JsonValue) -> None:
            if isinstance(value, dict):
                value.pop("x-tinkerfin-file-path", None)
                for child in value.values():
                    remove_annotations(child)
            elif isinstance(value, list):
                for child in value:
                    remove_annotations(child)

        remove_annotations(schema)
    workspace.descriptor = ToolDescriptor(
        name=workspace.descriptor.name,
        description=workspace.descriptor.description,
        input_schema=schema,
    )


def absolute_proposal(workspace: ProvidingWorkspace) -> AIMessage:
    message = proposal()
    message.tool_calls[0]["args"] = {
        "action": {
            "kind": "read",
            "paths": [str(workspace.directory / "a.txt")],
        }
    }
    return message


@pytest.mark.parametrize(
    ("change", "resolution"),
    [
        ("operation", "approve"),
        ("operation", "edit"),
        ("operation", "reject"),
        ("operation", "cancel"),
        ("annotations", "approve"),
        ("resolver", "approve"),
    ],
)
async def test_pending_file_declaration_changes_cannot_authorize_execution(
    tmp_path: Path,
    change: Literal["operation", "annotations", "resolver"],
    resolution: str,
) -> None:
    workspace = ChangingWorkspace(tmp_path / "project")
    model = RecordingModel(
        responses=[absolute_proposal(workspace), AIMessage(content="done")]
    )
    saver = InMemorySaver()

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("review")
            .build(model=model, backend=workspace, interrupt_on={"inspect_paths": True})
        )

    initial = await consume(
        build().open_agui_run(
            thread_id="thread",
            run_id="propose",
            messages=[{"id": "user", "role": "user", "content": "inspect"}],
        )
    )
    terminal = initial[-1]
    assert isinstance(terminal, RunFinishedEvent)
    saved_resolutions = list(workspace.resolutions)
    change_declaration(workspace, change)
    if resolution == "edit":
        payload = {
            "type": "edit",
            "edited_action": {
                "name": "inspect_paths",
                "args": {"action": {"kind": "read", "paths": ["edited.txt"]}},
            },
        }
    elif resolution == "cancel":
        payload = None
    elif resolution == "reject":
        payload = {"type": "reject", "message": "Do not inspect."}
    else:
        payload = {"type": "approve"}
    stream = build().open_agui_run(
        thread_id="thread", run_id="decision", resume=decision(terminal, payload)
    )
    async with aclosing(stream):
        events = [event async for event in stream]
    assert sum(event.type.value == "RUN_STARTED" for event in events) == 1
    if resolution == "cancel":
        assert stream.error is None
        assert isinstance(events[-1], RunErrorEvent)
        assert events[-1].code == "resume_cancelled"
    else:
        assert isinstance(stream.error, TinkerFinLifecycleError)
        assert "file declarations changed" in str(stream.error)
        assert isinstance(events[-1], RunErrorEvent)
    assert sum(event.type.value == "RUN_ERROR" for event in events) == 1
    assert not any(isinstance(event, RunFinishedEvent) for event in events)
    assert workspace.effects == []
    assert workspace.resolutions == saved_resolutions
    assert workspace.closed == ["propose", "decision"]


@pytest.mark.parametrize("resolution", ["approve", "edit"])
@pytest.mark.parametrize("changed", [False, True])
async def test_approved_tool_rechecks_file_declarations_after_execution_pause(
    tmp_path: Path, resolution: str, changed: bool
) -> None:
    workspace = ChangingWorkspace(tmp_path / "project")
    model = RecordingModel(
        responses=[absolute_proposal(workspace), AIMessage(content="done")]
    )
    saver = InMemorySaver()

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("review")
            .build(model=model, backend=workspace, interrupt_on={"inspect_paths": True})
        )

    initial = await build().ainvoke(
        thread_id="thread",
        run_id="propose",
        input={"messages": [{"id": "user", "role": "user", "content": "inspect"}]},
    )
    interrupts = initial["__interrupt__"]
    assert isinstance(interrupts, list) and len(interrupts) == 1
    assert isinstance(interrupts[0], NativeRuntimeInterrupt)
    selected = (
        {
            "type": "edit",
            "edited_action": {
                "name": "inspect_paths",
                "args": {"action": {"kind": "read", "paths": ["edited.txt"]}},
            },
        }
        if resolution == "edit"
        else {"type": "approve"}
    )
    await build().ainvoke(
        thread_id="thread",
        run_id="approved",
        input=Command(resume={interrupts[0].id: {"decisions": [selected]}}),
        interrupt_before=["tools"],
    )
    assert workspace.effects == []
    saved_resolutions = list(workspace.resolutions)
    if changed:
        change_declaration(workspace, "operation")
        with pytest.raises(TinkerFinLifecycleError, match="file declarations changed"):
            await build().ainvoke(thread_id="thread", run_id="continued", input=None)
        assert workspace.effects == []
        assert workspace.resolutions == saved_resolutions
    else:
        await build().ainvoke(thread_id="thread", run_id="continued", input=None)
        name = "edited.txt" if resolution == "edit" else "a.txt"
        assert workspace.effects == [
            {"action": {"kind": "read", "paths": [str(workspace.directory / name)]}}
        ]
    assert workspace.closed == ["propose", "approved", "continued"]


async def test_pending_review_requires_saved_file_declaration_evidence(
    tmp_path: Path,
) -> None:
    class MissingDeclarationSaver(InMemorySaver):
        remove_declaration = False

        async def aget_tuple(self, config):
            saved = await super().aget_tuple(config)
            if saved is None or not self.remove_declaration:
                return saved
            checkpoint = deepcopy(saved.checkpoint)
            checkpoint["channel_values"].pop("_tinkerfin_file_contract", None)
            return saved._replace(checkpoint=checkpoint)

    workspace = ChangingWorkspace(tmp_path / "project")
    model = RecordingModel(responses=[absolute_proposal(workspace)])
    saver = MissingDeclarationSaver()
    runtime = (
        TinkerFin(checkpointer=saver)
        .with_namespace("review")
        .build(model=model, backend=workspace, interrupt_on={"inspect_paths": True})
    )
    initial = await consume(
        runtime.open_agui_run(
            thread_id="thread",
            run_id="propose",
            messages=[{"id": "user", "role": "user", "content": "inspect"}],
        )
    )
    terminal = initial[-1]
    assert isinstance(terminal, RunFinishedEvent)
    saver.remove_declaration = True
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="decision",
        resume=decision(terminal, {"type": "approve"}),
    )
    async with aclosing(stream):
        events = [event async for event in stream]
    assert isinstance(stream.error, TinkerFinLifecycleError)
    assert "file declarations changed" in str(stream.error)
    assert isinstance(events[-1], RunErrorEvent)
    assert workspace.effects == []


async def test_native_reviewed_edit_cannot_select_a_changed_file_tool(
    tmp_path: Path,
) -> None:
    effects: list[str] = []

    @tool
    async def review_report(note: str) -> str:
        """Record the report review note."""
        effects.append(note)
        return note

    workspace = ChangingWorkspace(tmp_path / "project")
    model = RecordingModel(
        responses=[
            AIMessage(
                id="proposal",
                content="",
                tool_calls=[
                    {
                        "id": "review",
                        "name": "review_report",
                        "args": {"note": "ready"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    saver = InMemorySaver()

    def build():
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("review")
            .build(
                model=model,
                backend=workspace,
                tools=[review_report],
                interrupt_on={"review_report": True},
            )
        )

    initial = await build().ainvoke(
        thread_id="thread",
        run_id="propose",
        input={"messages": [{"id": "user", "role": "user", "content": "review"}]},
    )
    interrupts = initial["__interrupt__"]
    assert isinstance(interrupts, list) and len(interrupts) == 1
    assert isinstance(interrupts[0], NativeRuntimeInterrupt)
    change_declaration(workspace, "operation")
    with pytest.raises(TinkerFinLifecycleError, match="file declarations changed"):
        await build().ainvoke(
            thread_id="thread",
            run_id="decision",
            input=Command(
                resume={
                    interrupts[0].id: {
                        "decisions": [
                            {
                                "type": "edit",
                                "edited_action": {
                                    "name": "inspect_paths",
                                    "args": {
                                        "action": {
                                            "kind": "read",
                                            "paths": ["edited.txt"],
                                        }
                                    },
                                },
                            }
                        ]
                    }
                }
            ),
        )
    assert workspace.effects == [] and effects == []
    assert workspace.resolutions == []
