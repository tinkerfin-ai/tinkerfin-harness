"""Validate declarations and borrow a workspace for one admitted run."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Generic, cast

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.typing import ContextT

from tinkerfin_contracts import PreparedWorkspace, RunIdentity, Workspace

from ._agent_spec import AgentMiddlewareType, AgentSpec, ToolDefinition
from ._hitl import _as_permissions
from ._middleware_resources import validate_middleware_resources
from ._run_resources import RunResources
from ._state_schema import validate_state_schema
from ._store import validate_store_backend
from ._store_backend import async_store_backend
from ._subagents import validate_subagent_resources

_DECLARED_AGENT_FIELDS = frozenset(
    {
        "name",
        "description",
        "system_prompt",
        "model",
        "tools",
        "middleware",
        "interrupt_on",
        "skills",
        "permissions",
        "response_format",
    }
)
_EXTERNAL_AGENT_FIELDS = frozenset({"name", "description", "runnable"})
_REMOTE_AGENT_FIELDS = frozenset({"name", "description", "graph_id", "url", "headers"})
_FILESYSTEM_TOOL_NAMES = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}
)
_REMOTE_TOOL_NAMES = frozenset(
    {
        "start_async_task",
        "check_async_task",
        "update_async_task",
        "cancel_async_task",
        "list_async_tasks",
    }
)


def _validate_middleware(middleware: Sequence[AgentMiddlewareType]) -> None:
    validate_middleware_resources(middleware)
    if any(
        isinstance(item, HumanInTheLoopMiddleware) or item.name == "ToolReview"
        for item in middleware
    ):
        raise ValueError(
            "configure tool review through interrupt_on, not custom HITL middleware"
        )
    if any(
        isinstance(item, (SubAgentMiddleware, AsyncSubAgentMiddleware))
        or item.name in {"SubAgentMiddleware", "AsyncSubAgentMiddleware"}
        for item in middleware
    ):
        raise ValueError(
            "configure delegation through subagents, not custom delegation middleware"
        )


def _validate_delegation_tools(
    tools: Sequence[ToolDefinition],
    middleware: Sequence[AgentMiddlewareType],
    *,
    has_remote_agents: bool,
) -> None:
    # Delegation must use the roles whose access and lifecycle were configured
    # by the Runtime. LangChain registers colliding tools by name, so replacing
    # task bypasses that whole role configuration even without replacing its middleware.
    reserved = {"task"} | (_REMOTE_TOOL_NAMES if has_remote_agents else set[str]())
    middleware_tools = (
        tool for item in middleware if hasattr(item, "tools") for tool in item.tools
    )
    for definition in (*tools, *middleware_tools):
        if isinstance(definition, BaseTool):
            name = definition.name
        elif isinstance(definition, dict):
            name = cast(Mapping[str, object], definition).get("name")
        else:
            name = getattr(definition, "__name__", None)
        if isinstance(name, str) and name in reserved:
            raise ValueError(
                f"tool {name!r} is owned by delegation; configure subagents instead"
            )


def _validate_filesystem_configuration(
    *,
    middleware: Sequence[AgentMiddlewareType],
    tools: Sequence[ToolDefinition],
    permissions: Sequence[FilesystemPermission],
    owns_workspace: bool,
) -> None:
    """Keep declared file access rules and workspace tools under one owner.

    Deep Agents FilesystemMiddleware enforces permissions inside its tools;
    LangChain create_agent registers later tools under the same name. Replacing
    either the middleware or a tool therefore bypasses those rules. Custom
    middleware has no public contract proving it preserves this ownership.
    See test_filesystem_configuration.py for public construction regressions.
    """
    if not owns_workspace and not permissions:
        return
    replaces_filesystem = any(
        isinstance(item, FilesystemMiddleware) or item.name == "FilesystemMiddleware"
        for item in middleware
    )
    middleware_tools = (
        tool for item in middleware if hasattr(item, "tools") for tool in item.tools
    )
    for definition in (*tools, *middleware_tools):
        if isinstance(definition, BaseTool):
            name = definition.name
        elif isinstance(definition, dict):
            name = cast(Mapping[str, object], definition).get("name")
        else:
            name = getattr(definition, "__name__", None)
        if isinstance(name, str) and name in _FILESYSTEM_TOOL_NAMES:
            replaces_filesystem = True
    if replaces_filesystem:
        if owns_workspace:
            raise ValueError(
                "a Workspace owns its filesystem middleware and tools; "
                "configure permissions instead"
            )
        raise ValueError(
            "filesystem permissions cannot be combined with custom filesystem "
            "middleware or tools that replace built-in filesystem tools"
        )


def validate_agent_spec(spec: AgentSpec[ContextT]) -> None:
    """Reject invalid configuration before model resolution or resource I/O."""
    if not isinstance(spec.model, (str, BaseChatModel)):
        raise TypeError("model must be a model identifier or BaseChatModel")
    if isinstance(spec.model, str) and not spec.model.strip():
        raise ValueError("model must not be blank")
    validate_state_schema(spec.state_schema, source="Agent state_schema")
    _as_permissions(spec.permissions)
    validate_store_backend(spec.backend)
    if spec.backend is not None and not isinstance(
        spec.backend, (BackendProtocol, Workspace)
    ):
        raise TypeError("backend must be a BackendProtocol or Workspace")
    if any(not isinstance(item, AgentMiddleware) for item in spec.middleware):
        raise TypeError("middleware must contain AgentMiddleware instances")
    _validate_middleware(spec.middleware)
    has_remote_agents = any(
        isinstance(child, Mapping) and "graph_id" in child for child in spec.subagents
    )
    _validate_delegation_tools(
        spec.tools, spec.middleware, has_remote_agents=has_remote_agents
    )
    owns_workspace = isinstance(spec.backend, Workspace)
    _validate_filesystem_configuration(
        middleware=spec.middleware,
        tools=spec.tools,
        permissions=spec.permissions,
        owns_workspace=owns_workspace,
    )
    validate_subagent_resources(spec.subagents)
    names: set[str] = set()
    for declaration in spec.subagents:
        if not isinstance(declaration, Mapping):
            raise TypeError("subagents must contain declarations")
        values = cast(Mapping[str, object], declaration)
        fields = (
            _EXTERNAL_AGENT_FIELDS
            if "runnable" in values
            else _REMOTE_AGENT_FIELDS
            if "graph_id" in values
            else _DECLARED_AGENT_FIELDS
        )
        unknown = values.keys() - fields
        if unknown:
            raise TypeError(
                f"unsupported subagent fields: {', '.join(sorted(unknown))}"
            )
        name = values.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("subagents require unique non-empty names")
        names.add(name)
        if "runnable" not in declaration and "graph_id" not in declaration:
            if not isinstance(values.get("system_prompt"), str):
                raise TypeError("declarative subagents require system_prompt")
            middleware = declaration.get("middleware", ())
            _validate_middleware(middleware)
            _validate_delegation_tools(
                declaration.get("tools", spec.tools),
                middleware,
                has_remote_agents=False,
            )
            permissions = _as_permissions(
                declaration.get("permissions", spec.permissions)
            )
            _validate_filesystem_configuration(
                middleware=middleware,
                tools=declaration.get("tools", spec.tools),
                permissions=permissions,
                owns_workspace=owns_workspace,
            )


@dataclass(frozen=True, slots=True)
class PreparedAgentSpec(Generic[ContextT]):
    """Retain prepared configuration and the borrowed workspace description."""

    spec: AgentSpec[ContextT]
    workspace: PreparedWorkspace[object, BackendProtocol] | None


async def prepare_agent_spec(
    spec: AgentSpec[ContextT], identity: RunIdentity, resources: RunResources
) -> PreparedAgentSpec[ContextT]:
    """Prepare a workspace only after admission and retain its run-owned borrow."""
    workspace: PreparedWorkspace[object, BackendProtocol] | None = None
    declaration = spec.backend
    if isinstance(declaration, Workspace):
        workspace = await resources.prepare_workspace(declaration, identity)
        if not isinstance(workspace, PreparedWorkspace):
            raise TypeError("Workspace.prepare must yield PreparedWorkspace")
        if not isinstance(workspace.backend, BackendProtocol):
            raise TypeError("the prepared backend must implement BackendProtocol")
        validate_store_backend(workspace.backend)
        workspace = replace(workspace, backend=async_store_backend(workspace.backend))
        spec = replace(spec, backend=workspace.backend)
    elif isinstance(declaration, BackendProtocol):
        spec = replace(spec, backend=async_store_backend(declaration))
    spec = replace(
        spec,
        tool_scope=resources.borrow_tools(
            identity, None if workspace is None else workspace.workspace
        ),
    )
    return PreparedAgentSpec(spec.snapshot(), workspace)
