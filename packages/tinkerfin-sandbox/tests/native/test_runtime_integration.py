"""Real Runtime execution and approval over a native local workspace."""

import shlex
from collections.abc import Callable, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest
from ag_ui.core.types import ResumeEntry
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import AgUiResumeRequest, TinkerFin
from tinkerfin_contracts import RunIdentity
from tinkerfin_sandbox import LocalAccess, LocalWorkspace


class ToolModel(FakeMessagesListChatModel):
    """Return deterministic provider messages while accepting real tool binding."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> "ToolModel":
        return self


@pytest.mark.parametrize(
    "path_kind", ["relative", "absolute", "home", "symlink_parent"]
)
async def test_native_workspace_survives_approval_and_is_released_between_runs(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch, path_kind: str
) -> None:
    project, user_home, outside = (
        tmp_path / name for name in ("project", "home", "outside")
    )
    for directory in (project, user_home, outside / "nested"):
        directory.mkdir(parents=True)
    (project / "link").symlink_to(outside / "nested", target_is_directory=True)
    monkeypatch.setenv("HOME", str(user_home))
    requested, expected = {
        "relative": ("report.txt", project / "report.txt"),
        "absolute": (str(outside / "report.txt"), outside / "report.txt"),
        "home": ("~/report.txt", user_home / "report.txt"),
        "symlink_parent": ("link/../report.txt", outside / "report.txt"),
    }[path_kind]
    model = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "file-call",
                        "name": "write_file",
                        "args": {
                            "file_path": requested,
                            "content": "approved result",
                        },
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    async with LocalWorkspace(
        project, sandbox_executable=sandbox, access=LocalAccess(filesystem="user")
    ) as workspace:
        runtime = (
            TinkerFin(checkpointer=InMemorySaver())
            .with_namespace("local-user")
            .build(
                model=model,
                backend=workspace,
                interrupt_on={"write_file": True},
            )
        )
        async with aclosing(
            runtime.open_agui_run(
                thread_id="thread",
                run_id="first",
                messages=[{"id": "user", "role": "user", "content": "Create a report"}],
            )
        ) as stream:
            first = [
                event.model_dump(mode="json", by_alias=True) async for event in stream
            ]
        assert first[0]["type"] == "RUN_STARTED"
        assert not expected.exists()
        terminal = [event for event in first if event["type"] == "RUN_FINISHED"]
        assert len(terminal) == 1 and terminal[0]["outcome"]["type"] == "interrupt"
        entries = tuple(
            ResumeEntry(
                interrupt_id=entry["id"], status="resolved", payload={"type": "approve"}
            )
            for entry in terminal[0]["outcome"]["interrupts"]
        )
        async with aclosing(
            runtime.open_agui_run(
                thread_id="thread",
                run_id="approved",
                parent_run_id="first",
                resume=AgUiResumeRequest(entries=entries),
            )
        ) as stream:
            resumed = [event.type.value async for event in stream]
        assert resumed.count("RUN_STARTED") == resumed.count("RUN_FINISHED") == 1
        assert "RUN_ERROR" not in resumed
    assert expected.read_text() == "approved result"


async def test_execution_offloads_to_persistent_native_project_files(
    tmp_path: Path, sandbox: Path
) -> None:
    # Deep Agents 0.7.19 evicts at 20,000 tokens with four characters per token.
    raw = "x" * 80_001
    model = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "oversized-output",
                        "name": "execute",
                        "args": {
                            "command": "python -c "
                            + shlex.quote("print('x' * 80001, end='')")
                        },
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        runtime = (
            TinkerFin().with_namespace("local-user").build(model, backend=workspace)
        )
        async with aclosing(
            runtime.open_agui_run(
                thread_id="offload",
                run_id="first",
                messages=[{"id": "user", "role": "user", "content": "Capture output"}],
            )
        ) as stream:
            events = [
                event.model_dump(mode="json", by_alias=True) async for event in stream
            ]
        assert not any(event["type"] == "RUN_ERROR" for event in events)
        assert sum(event["type"] == "RUN_FINISHED" for event in events) == 1
        artifacts = [path for path in tmp_path.rglob("*") if path.is_file()]
        assert len(artifacts) == 1
        artifact = artifacts[0]
        saved = artifact.read_bytes()
        assert saved.startswith(raw.encode())
        assert any(
            event["type"] == "TOOL_CALL_RESULT" and str(artifact) in event["content"]
            for event in events
        )
        async with workspace.prepare(
            RunIdentity(namespace="local-user", thread_id="offload", run_id="inspect")
        ) as prepared:
            (retrieved,) = await prepared.backend.adownload_files([str(artifact)])
            assert retrieved.error is None and retrieved.content == saved
    assert artifact.read_bytes() == saved
