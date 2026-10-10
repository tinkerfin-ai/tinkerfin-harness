"""File permissions and workspace ownership survive custom role configuration."""

from collections.abc import Sequence

import pytest
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from test_plan_mode import _FakeModel
from test_runtime_workspace import _Workspace

from tinkerfin import TinkerFin
from tinkerfin._agent_spec import AgentMiddlewareType, ToolDefinition
from tinkerfin.subagents import SubAgent

_DENY = [FilesystemPermission(["read"], ["/secret"], "deny")]


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


# Input normalization is shared across roles. Exercise all forms at the main
# boundary and permission inheritance/overrides with a representative conflict.
@pytest.mark.parametrize(
    ("kind", "role", "workspace_owned"),
    [
        (kind, "main", workspace_owned)
        for kind in (
            "filesystem",
            "subclass",
            "named_replacement",
            "middleware_tool",
            "callable",
            "tool",
        )
        for workspace_owned in (False, True)
    ]
    + [
        ("subclass", role, workspace_owned)
        for role in ("inherited_child", "explicit_child")
        for workspace_owned in (False, True)
    ],
)
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
