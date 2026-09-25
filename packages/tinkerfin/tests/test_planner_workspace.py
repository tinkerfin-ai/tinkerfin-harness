"""Planner tools borrow the workspace and preserve its allow, deny, and review rules."""

from pathlib import Path
from typing import Literal

import pytest
from deepagents import FilesystemPermission
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.utils import create_file_data
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from test_plan_mode import _FakeModel, _parts, _planner, _root_interrupts
from test_runtime_workspace import _Workspace

from tinkerfin import TinkerFin


@pytest.mark.parametrize("mode", ["allow", "deny", "interrupt"])
@pytest.mark.parametrize("name", ["read_file", "ls", "glob", "grep"])
async def test_planner_workspace_filters_protected_reads(
    mode: Literal["allow", "deny", "interrupt"], name: str
) -> None:
    store = InMemoryStore()
    for filename in ("public", "secret"):
        await store.aput(
            ("dGVzdA", "planning"),
            f"/{filename}.txt",
            dict(create_file_data(f"{filename} information")),
        )
    workspace = _Workspace(
        CompositeBackend(
            default=StateBackend(),
            routes={"/memory/": StoreBackend(namespace=lambda _: ("planning",))},
        )
    )
    arguments: dict[str, str] = {
        "read_file": {"file_path": "/memory/secret.txt"},
        "ls": {"path": "/memory/"},
        "glob": {"pattern": "*.txt", "path": "/memory/"},
        "grep": {"pattern": "information", "path": "/memory/"},
    }[name]
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": arguments, "id": "inspect"},
                    {
                        "name": "read_file",
                        "args": {"file_path": "/memory/public.txt"},
                        "id": "read-public",
                    },
                ],
            ),
            _planner(),
        ]
    )
    runtime = (
        TinkerFin(store=store, checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            backend=workspace,
            permissions=[
                FilesystemPermission(["read"], ["/memory/public.txt"], "allow"),
                FilesystemPermission(["read"], ["/memory/**"], mode),
            ],
        )
    )
    parts = await _parts(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="planner-permissions",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    if mode == "interrupt":
        pending = _root_interrupts(parts)
        assert len(pending) == 1
        assert pending[0].value["action_requests"][0]["name"] == name
        parts.extend(
            await _parts(
                runtime,
                Command(resume={"decisions": [{"type": "approve"}]}),
                run_id="planner-permissions-review",
                config={"configurable": {"thread_id": "plan-thread"}},
                mode="plan",
            )
        )
    replies: dict[str, str] = {}
    for part in parts:
        if part["type"] != "messages":
            continue
        payload = part["data"]
        if isinstance(payload, tuple) and isinstance(payload[0], ToolMessage):
            message = payload[0]
            replies[message.tool_call_id] = str(message.content)
    assert "public information" in replies["read-public"]
    protected = "secret information" if name == "read_file" else "secret.txt"
    assert (protected in replies["inspect"]) is (mode != "deny")
    assert workspace.closed == workspace.opened
    assert len(workspace.opened) == (2 if mode == "interrupt" else 1)


async def test_planner_tools_receive_the_prepared_workspace() -> None:
    from tinkerfin.tools import ToolRuntime

    seen: list[Path] = []

    @tool
    async def read_customer(runtime: ToolRuntime[None, Path]) -> str:
        """Read customer information for planning."""
        seen.append(runtime.workspace)
        return "customer information"

    workspace = _Workspace()
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_customer", "args": {}, "id": "read-customer"}
                ],
            ),
            _planner(),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, backend=workspace, tools=[read_customer])
    )
    await _parts(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="planner-workspace-tools",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    assert seen == [Path("/files/test")]
    assert model.bound_tool_names and all(
        "read_customer" in names for names in model.bound_tool_names
    )
    assert workspace.closed == workspace.opened
