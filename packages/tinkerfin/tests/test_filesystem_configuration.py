"""File permissions and workspace ownership survive custom role configuration."""

from collections.abc import Sequence
from typing import Literal

import pytest
from deepagents.backends import StateBackend
from deepagents.backends.protocol import FileData
from deepagents.backends.utils import create_file_data
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.runtime import Runtime
from test_agent_construction import delegate
from test_plan_mode import _FakeModel
from test_runtime_workspace import _Workspace

from tinkerfin import TinkerFin
from tinkerfin._agent_spec import AgentMiddlewareType, ToolDefinition
from tinkerfin.subagents import SubAgent

_DENY = [FilesystemPermission(["read"], ["/secret"], "deny")]


class FileInput(InputAgentState):
    files: dict[str, FileData]


class RenamedFilesystem(FilesystemMiddleware):
    """Use the public subclass name instead of replacing the default slot."""


class NamedReplacement(AgentMiddleware):
    @property
    def name(self) -> str:
        return "FilesystemMiddleware"


async def read_file(file_path: str) -> str:
    """Read a business reference using a conflicting filesystem tool name."""
    return "business reference"


class FileTools(AgentMiddleware):
    tools = [tool(read_file)]


class ModelHook(AgentMiddleware):
    def __init__(self) -> None:
        self.calls = 0

    def before_model(self, state: AgentState, runtime: Runtime) -> None:
        self.calls += 1


def _conflict(
    kind: str,
) -> tuple[Sequence[AgentMiddlewareType], Sequence[ToolDefinition]]:
    if kind == "filesystem":
        return [FilesystemMiddleware(backend=StateBackend())], []
    if kind == "subclass":
        return [RenamedFilesystem(backend=StateBackend())], []
    if kind == "named_replacement":
        return [NamedReplacement()], []
    if kind == "middleware_tool":
        return [FileTools()], []
    if kind == "callable":
        return [], [read_file]
    return [], [tool(read_file)]


@pytest.mark.parametrize(
    "kind",
    [
        "filesystem",
        "subclass",
        "named_replacement",
        "middleware_tool",
        "callable",
        "tool",
    ],
)
@pytest.mark.parametrize("role", ["main", "inherited_child", "explicit_child"])
@pytest.mark.parametrize("workspace_owned", [False, True])
def test_conflicting_filesystem_configuration_is_rejected_before_resource_use(
    kind: str, role: str, workspace_owned: bool
) -> None:
    middleware, tools = _conflict(kind)
    workspace = _Workspace()
    model = _FakeModel(responses=[AIMessage(content="unused")])
    child: SubAgent = {
        "name": "reader",
        "description": "Read a reference",
        "system_prompt": "Read",
        "middleware": middleware,
        "tools": tools,
    }
    if role == "explicit_child":
        child["permissions"] = _DENY
    match = (
        "Workspace owns its filesystem" if workspace_owned else "filesystem permissions"
    )
    with pytest.raises(ValueError, match=match):
        TinkerFin().with_namespace("test").build(
            model=model,
            backend=workspace if workspace_owned else StateBackend(),
            permissions=_DENY if role != "explicit_child" else [],
            middleware=middleware if role == "main" else [],
            tools=tools if role == "main" else [],
            subagents=[] if role == "main" else [child],
        )
    assert not model.model_inputs and not model.bound_tool_names
    assert not workspace.opened


@pytest.mark.parametrize("kind", ["subclass", "middleware_tool", "tool", "callable"])
def test_child_permissions_validate_inherited_tools(kind: str) -> None:
    middleware, tools = _conflict(kind)
    child: SubAgent = {
        "name": "reader",
        "description": "Read",
        "system_prompt": "Read",
        "permissions": _DENY,
    }
    # Main middleware is not inherited by declared agents. Only its tools are
    # irrelevant here; child-local middleware is validated against child rules.
    if middleware:
        child["middleware"] = middleware
    with pytest.raises(ValueError, match="filesystem permissions"):
        TinkerFin().with_namespace("test").build(
            model=_FakeModel(responses=[AIMessage(content="unused")]),
            tools=tools,
            subagents=[child],
        )


@pytest.mark.parametrize("role", ["main", "general-purpose", "reader"])
@pytest.mark.parametrize("allow_custom", [False, True])
async def test_file_access_obeys_each_roles_effective_permissions(
    role: str, allow_custom: bool
) -> None:
    read = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "args": {"file_path": "/secret"}, "id": "read"}
        ],
    )
    responses: list[BaseMessage] = [read, AIMessage(content="finished")]
    if role != "main":
        responses = [delegate(role), *responses, AIMessage(content="parent finished")]
    model = _FakeModel(responses=responses)
    custom = [FilesystemMiddleware(backend=StateBackend())] if allow_custom else []
    permissions = [] if allow_custom else _DENY
    subagents: list[SubAgent] = []
    if role == "reader":
        subagents = [
            {
                "name": "reader",
                "description": "Read",
                "system_prompt": "Read",
                "middleware": custom,
            }
        ]
        if allow_custom:
            # Explicitly empty child rules replace inherited restrictions.
            subagents[0]["permissions"] = []
    runtime = (
        TinkerFin()
        .with_namespace("test")
        .build(
            model=model,
            backend=StateBackend(),
            permissions=_DENY if role == "reader" else permissions,
            middleware=[] if role == "reader" else custom,
            subagents=subagents,
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input=FileInput(
            messages=[{"role": "user", "content": "Read the reference"}],
            files={"/secret": create_file_data("fixture-secret")},
        ),
    )
    replies = [
        str(message.content)
        for messages in model.model_inputs
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id == "read"
    ]
    assert replies
    assert all(("fixture-secret" in reply) is allow_custom for reply in replies)
    if not allow_custom:
        assert all("permission denied" in reply for reply in replies)


@pytest.mark.parametrize("custom_filesystem", [False, True])
async def test_custom_model_hooks_remain_usable(custom_filesystem: bool) -> None:
    hook = ModelHook()
    middleware: list[AgentMiddlewareType] = [hook]
    if custom_filesystem:
        middleware.append(FilesystemMiddleware(backend=StateBackend()))
    model = _FakeModel(responses=[AIMessage(content="done")])
    runtime = (
        TinkerFin()
        .with_namespace("test")
        .build(
            model=model,
            permissions=[] if custom_filesystem else _DENY,
            middleware=middleware,
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "Read the reference"}]},
    )
    assert hook.calls == 1


@pytest.mark.parametrize("role", ["main", "child"])
@pytest.mark.parametrize("kind", ["filesystem", "subclass", "middleware_tool", "tool"])
def test_workspace_keeps_filesystem_ownership_without_permissions(
    role: Literal["main", "child"], kind: str
) -> None:
    middleware, tools = _conflict(kind)
    workspace = _Workspace()
    with pytest.raises(ValueError, match="Workspace owns its filesystem"):
        TinkerFin().with_namespace("test").build(
            model=_FakeModel(responses=[AIMessage(content="unused")]),
            backend=workspace,
            middleware=middleware if role == "main" else [],
            tools=tools if role == "main" else [],
            subagents=[
                {
                    "name": "reader",
                    "description": "Read",
                    "system_prompt": "Read",
                    "middleware": middleware,
                    "tools": tools,
                    "permissions": [],
                }
            ]
            if role == "child"
            else [],
        )
    assert not workspace.opened
