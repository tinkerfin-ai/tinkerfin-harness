"""Custom behavior cannot replace the Runtime's declared delegation capability."""

import pytest
from deepagents.backends import StateBackend
from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware
from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from test_plan_mode import _FakeModel
from test_runtime_workspace import _Workspace

from tinkerfin import TinkerFin
from tinkerfin.subagents import SubAgent


class RenamedDelegation(SubAgentMiddleware):
    """Exercise native delegation with a different public middleware name."""


class RenamedRemoteDelegation(AsyncSubAgentMiddleware):
    """Exercise remote delegation with a different public middleware name."""


class NamedDelegation(AgentMiddleware):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name


# Both roles share middleware validation; filesystem configuration does not
# change it. Cover each kind/role and each boundary/role without their full product.
@pytest.mark.parametrize(
    ("kind", "role", "boundary"),
    [
        (kind, role, "plain")
        for kind in (
            "native",
            "subclass",
            "remote",
            "remote_subclass",
            "name",
            "remote_name",
        )
        for role in ("main", "child")
    ]
    + [
        ("subclass", role, boundary)
        for role in ("main", "child")
        for boundary in ("permissions", "workspace")
    ],
)
def test_delegation_middleware_conflicts_are_rejected_before_execution(
    kind: str, role: str, boundary: str
) -> None:
    model = _FakeModel(responses=[AIMessage(content="unused")])
    if kind in {"native", "subclass"}:
        middleware = (SubAgentMiddleware if kind == "native" else RenamedDelegation)(
            backend=StateBackend(),
            subagents=[
                {
                    "name": "reader",
                    "description": "Read a report",
                    "system_prompt": "Read a report",
                    "model": model,
                    "tools": [],
                }
            ],
        )
    elif kind in {"remote", "remote_subclass"}:
        middleware = (
            AsyncSubAgentMiddleware if kind == "remote" else RenamedRemoteDelegation
        )(
            async_subagents=[
                {"name": "remote", "description": "Read", "graph_id": "read"}
            ]
        )
    else:
        middleware = NamedDelegation(
            "SubAgentMiddleware" if kind == "name" else "AsyncSubAgentMiddleware"
        )
    workspace = _Workspace()
    child: SubAgent = {
        "name": "reader",
        "description": "Read a report",
        "system_prompt": "Read a report",
        "middleware": [middleware],
    }
    with pytest.raises(ValueError, match="configure delegation through subagents"):
        TinkerFin().with_namespace("scope").build(
            model=model,
            backend=workspace if boundary == "workspace" else StateBackend(),
            permissions=[FilesystemPermission(["read"], ["/secret"], "deny")]
            if boundary == "permissions"
            else [],
            middleware=[middleware] if role == "main" else [],
            subagents=[child] if role == "child" else [],
        )
    assert not model.model_inputs and not workspace.opened


@pytest.mark.parametrize("role", ["main", "child"])
@pytest.mark.parametrize("form", ["callable", "tool", "middleware", "dict"])
def test_task_tool_cannot_shadow_declared_delegation(role: str, form: str) -> None:
    async def task() -> str:
        """Run a custom delegation operation."""
        return "unexpected"

    class TaskTools(AgentMiddleware):
        tools = [tool(task)]

    tools = (
        [task]
        if form == "callable"
        else [tool(task)]
        if form == "tool"
        else [{"name": "task", "type": "function"}]
        if form == "dict"
        else []
    )
    middleware = [TaskTools()] if form == "middleware" else []
    child: SubAgent = {
        "name": "reader",
        "description": "Read",
        "system_prompt": "Read",
        "tools": tools,
        "middleware": middleware,
    }
    with pytest.raises(ValueError, match="owned by delegation"):
        TinkerFin().with_namespace("scope").build(
            model=_FakeModel(responses=[AIMessage(content="unused")]),
            tools=tools if role == "main" else [],
            middleware=middleware if role == "main" else [],
            subagents=[child] if role == "child" else [],
        )


@pytest.mark.parametrize(
    "name",
    [
        "start_async_task",
        "check_async_task",
        "update_async_task",
        "cancel_async_task",
        "list_async_tasks",
    ],
)
@pytest.mark.parametrize("remote_configured", [False, True])
def test_remote_tool_names_are_owned_only_when_remote_delegation_is_configured(
    name: str, remote_configured: bool
) -> None:
    @tool(name)
    async def operation() -> str:
        """Perform an application operation."""
        return "done"

    def build():
        return (
            TinkerFin()
            .with_namespace("scope")
            .build(
                model=_FakeModel(responses=[AIMessage(content="unused")]),
                tools=[operation],
                subagents=[
                    {"name": "remote", "description": "Read", "graph_id": "read"}
                ]
                if remote_configured
                else [],
            )
        )

    if remote_configured:
        with pytest.raises(ValueError, match="owned by delegation"):
            build()
    else:
        assert build()
