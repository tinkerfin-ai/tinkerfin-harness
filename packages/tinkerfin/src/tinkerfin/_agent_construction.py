"""Assemble role-specific agents from explicit framework configuration."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeAlias, cast

from deepagents import AsyncSubAgent, CompiledSubAgent, DeepAgentState
from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.subagents import (
    GENERAL_PURPOSE_SUBAGENT,
    SubAgentMiddleware,
)
from deepagents.middleware.subagents import (
    SubAgent as NativeSubAgent,
)
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
    create_summarization_middleware,
)

# LangGraph Checkpointer omits BaseCheckpointSaver's version parameter; retain
# LangChain's complete overloads and constrain the actual saver in AgentSpec.
from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import (
    AgentState,
    InputAgentState,
    OutputAgentState,
)
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.typing import ContextT

from tinkerfin_contracts import PreparedWorkspace

from ._agent_spec import AgentMiddlewareType, AgentSpec, CheckpointSaver, ToolDefinition
from ._attachment_agents import _AttachmentMiddleware, preserve_media
from ._durable_delegation import prepare_delegation_retry
from ._file_paths import _WorkspaceFilePaths, _WorkspaceFilesystem
from ._hitl import create_tool_review
from ._middleware_resources import prepare_middleware_resources
from ._skills import SourceSkillsMiddleware, resolve_skill_sources
from ._state_schema import private_state_fields
from ._summarization import ObservedCompactionTool, observe_summarization
from ._tool_runtime import _ToolRuntimeMiddleware
from ._workspace_operations import (
    ToolOriginsMiddleware,
    WorkspaceMemory,
    WorkspaceOperations,
)
from ._workspace_tools import merge_workspace_tools
from .media import AttachmentSupport
from .skills import SkillSource
from .tools import _ToolRunScope

if TYPE_CHECKING:
    from ._named_workspaces import NamedWorkspaces

AgentGraph: TypeAlias = CompiledStateGraph[
    AgentState[Any], Any, InputAgentState, OutputAgentState[Any]
]


def resolve_model(model: str | BaseChatModel) -> BaseChatModel:
    """Resolve an explicit model without consulting a harness-profile registry."""
    if isinstance(model, BaseChatModel):
        return model
    resolved = init_chat_model(model)
    if not isinstance(resolved, BaseChatModel):
        raise TypeError("model must resolve to BaseChatModel")
    return resolved


def _merge_middleware(
    defaults: Sequence[AgentMiddlewareType], custom: Sequence[AgentMiddlewareType]
) -> list[AgentMiddlewareType]:
    # A named replacement retains the default slot. Additional behavior follows
    # the base stack, before memory and final attachment projection.
    result = list(defaults)
    positions = {item.name: index for index, item in enumerate(result)}
    for item in custom:
        if item.name in positions:
            result[positions[item.name]] = item
        else:
            positions[item.name] = len(result)
            result.append(item)
    return result


def _filesystem(
    backend: BackendProtocol,
    permissions: Sequence[FilesystemPermission],
    workspace: PreparedWorkspace[object, BackendProtocol] | None,
    tools: Sequence[ToolDefinition],
    operations: WorkspaceOperations | None,
    named: NamedWorkspaces | None = None,
) -> FilesystemMiddleware:
    resolver = None if workspace is None else workspace.file_path_resolver
    filesystem_type = (
        FilesystemMiddleware
        if resolver is None and operations is None
        else _WorkspaceFilesystem
    )
    filesystem = filesystem_type(
        backend=backend,
        _permissions=list(permissions),
        system_prompt=None if workspace is None else workspace.filesystem_instructions,
        custom_tool_descriptions=None
        if workspace is None
        else dict(workspace.tool_descriptions),
    )
    if isinstance(filesystem, _WorkspaceFilesystem):
        filesystem.operations = operations
        if resolver is not None or named is not None:
            filesystem.file_paths = _WorkspaceFilePaths(
                resolver, filesystem.tools, tools, operations, named
            )
    return filesystem


def _with_attachments(
    middleware: list[AgentMiddlewareType],
    attachments: AttachmentSupport | None,
    operations: WorkspaceOperations | None,
) -> list[AgentMiddlewareType]:
    support = attachments if attachments is not None else AttachmentSupport()
    return [
        *(
            preserve_media(
                item,
                support,
                protect_messages=isinstance(item, FilesystemMiddleware)
                or item.name == "FilesystemMiddleware",
            )
            if isinstance(
                item,
                (
                    FilesystemMiddleware,
                    SkillsMiddleware,
                    MemoryMiddleware,
                    SubAgentMiddleware,
                    AsyncSubAgentMiddleware,
                    SummarizationToolMiddleware,
                ),
            )
            or item.name == "FilesystemMiddleware"
            else item
            for item in middleware
        ),
        _AttachmentMiddleware(support, operations),
    ]


def _role_defaults(
    *,
    model: BaseChatModel,
    backend: BackendProtocol,
    skills: Sequence[SkillSource] | None,
    permissions: Sequence[FilesystemPermission],
    interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    workspace: PreparedWorkspace[object, BackendProtocol] | None,
    tools: Sequence[ToolDefinition],
    operations: WorkspaceOperations | None,
    named: NamedWorkspaces | None = None,
) -> list[AgentMiddlewareType]:
    """Share file access, context management, and approvals across Agent roles."""
    filesystem = _filesystem(backend, permissions, workspace, tools, operations, named)
    paths = (
        filesystem.file_paths if isinstance(filesystem, _WorkspaceFilesystem) else None
    )
    defaults: list[AgentMiddlewareType] = [
        filesystem,
        observe_summarization(
            create_summarization_middleware(model, backend), operations
        ),
        PatchToolCallsMiddleware(),
    ]
    review = create_tool_review(permissions, interrupt_on, paths)
    if review is not None:
        defaults.append(review)
    if paths is not None:
        defaults.append(paths)
    if operations is not None:
        defaults.append(ToolOriginsMiddleware(operations))
    if skills is not None:
        defaults.append(
            SourceSkillsMiddleware(
                backend=backend, sources=list(skills), operations=operations
            )
        )
    return defaults


def _child_stack(
    *,
    model: BaseChatModel,
    backend: BackendProtocol,
    skills: Sequence[SkillSource] | None,
    permissions: Sequence[FilesystemPermission],
    custom: Sequence[AgentMiddlewareType],
    interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    workspace: PreparedWorkspace[object, BackendProtocol] | None,
    attachments: AttachmentSupport | None,
    tool_scope: _ToolRunScope[object] | None,
    checkpointer: CheckpointSaver | None,
    tools: Sequence[ToolDefinition],
    inherit_slots_only: bool = False,
    named: NamedWorkspaces | None = None,
) -> list[AgentMiddlewareType]:
    operations = None if tool_scope is None else WorkspaceOperations(tool_scope, named)
    defaults = _role_defaults(
        model=model,
        backend=backend,
        skills=skills,
        permissions=permissions,
        interrupt_on=interrupt_on,
        workspace=workspace,
        tools=tools,
        operations=operations,
        named=named,
    )
    if inherit_slots_only:
        names = {item.name for item in defaults}
        custom = tuple(item for item in custom if item.name in names)
    return _with_attachments(
        [
            *prepare_delegation_retry(
                _merge_middleware(defaults, custom), checkpointer
            ),
            _ToolRuntimeMiddleware(
                tool_scope,
                next(
                    (
                        item
                        for item in defaults
                        if isinstance(item, _WorkspaceFilePaths)
                    ),
                    None,
                ),
                named,
            ),
        ],
        attachments,
        operations,
    )


def create_agent_graph(
    spec: AgentSpec[ContextT],
    *,
    workspace: PreparedWorkspace[object, BackendProtocol] | None = None,
) -> AgentGraph:
    """Build one graph using the framework's explicit role and resource rules.

    This synchronous assembly runs in the Runtime's bounded worker boundary.
    Models and resources remain borrowed. Automatic general-purpose delegation
    inherits tools and skills; declared agents inherit tools but not skills.
    Native SubAgentMiddleware retains dynamic response schemas and checkpoint
    inheritance. No model-dependent HarnessProfile changes the selected stack.
    """
    model = resolve_model(spec.model)
    backend = spec.backend
    if backend is None:
        backend = StateBackend()
    if not isinstance(backend, BackendProtocol):
        raise TypeError("workspace must be prepared before agent construction")
    operations = (
        None
        if spec.tool_scope is None
        else WorkspaceOperations(spec.tool_scope, spec.named_workspaces)
    )
    custom = prepare_middleware_resources(spec.middleware, operations)
    prepared_tools = () if workspace is None else workspace.tools
    prepared_skills = () if workspace is None else workspace.skill_sources
    tools = merge_workspace_tools(spec.tools, prepared_tools, custom)
    if spec.named_workspaces is not None:
        spec.named_workspaces.validate_tools(
            [
                *tools,
                *(
                    tool
                    for item in custom
                    if hasattr(item, "tools")
                    for tool in item.tools
                ),
            ]
        )
        tools.extend(spec.named_workspaces.discovery_tools())
    skills = resolve_skill_sources(spec.skills, prepared_skills, inherit=True)
    inline: list[NativeSubAgent | CompiledSubAgent] = []
    remote: list[AsyncSubAgent] = []
    for declaration in spec.subagents:
        if "graph_id" in declaration:
            remote.append(declaration)
        elif "runnable" in declaration:
            inline.append(declaration)
        else:
            child = declaration
            child_model = resolve_model(child.get("model", model))
            child_custom = prepare_middleware_resources(
                child.get("middleware", ()), operations
            )
            child_tools = merge_workspace_tools(
                child.get("tools", spec.tools), prepared_tools, child_custom
            )
            if spec.named_workspaces is not None:
                spec.named_workspaces.validate_tools(
                    [
                        *child_tools,
                        *(
                            tool
                            for item in child_custom
                            if hasattr(item, "tools")
                            for tool in item.tools
                        ),
                    ]
                )
                child_tools.extend(spec.named_workspaces.discovery_tools())
            child_stack = _child_stack(
                model=child_model,
                backend=backend,
                skills=resolve_skill_sources(
                    child.get("skills"), prepared_skills, inherit=False
                )
                or None,
                permissions=child.get("permissions", spec.permissions),
                custom=child_custom,
                interrupt_on=child.get("interrupt_on", spec.interrupt_on),
                workspace=workspace,
                attachments=spec.attachments,
                tool_scope=spec.tool_scope,
                checkpointer=spec.checkpointer,
                tools=child_tools,
                named=spec.named_workspaces,
            )
            native: NativeSubAgent = {
                "name": child["name"],
                "description": child["description"],
                "system_prompt": child["system_prompt"],
                "model": child_model,
                "tools": child_tools,
                "middleware": child_stack,
            }
            if "response_format" in child:
                native["response_format"] = child["response_format"]
            inline.append(native)
    if not any(child["name"] == GENERAL_PURPOSE_SUBAGENT["name"] for child in inline):
        inline.insert(
            0,
            {
                "name": GENERAL_PURPOSE_SUBAGENT["name"],
                "description": GENERAL_PURPOSE_SUBAGENT["description"],
                "system_prompt": GENERAL_PURPOSE_SUBAGENT["system_prompt"]
                if "system_prompt" in GENERAL_PURPOSE_SUBAGENT
                else "",
                "model": model,
                "tools": tools,
                "middleware": _child_stack(
                    model=model,
                    backend=backend,
                    skills=skills or None,
                    permissions=spec.permissions,
                    custom=custom,
                    interrupt_on=spec.interrupt_on,
                    workspace=workspace,
                    attachments=spec.attachments,
                    tool_scope=spec.tool_scope,
                    checkpointer=spec.checkpointer,
                    tools=tools,
                    inherit_slots_only=True,
                    named=spec.named_workspaces,
                ),
            },
        )
    delegation = SubAgentMiddleware(
        backend=backend, subagents=inline, state_schema=spec.state_schema
    )
    defaults: list[AgentMiddlewareType] = []
    filesystem = _filesystem(
        backend, spec.permissions, workspace, tools, operations, spec.named_workspaces
    )
    paths = (
        filesystem.file_paths if isinstance(filesystem, _WorkspaceFilesystem) else None
    )
    defaults.extend(
        [
            filesystem,
            delegation,
            observe_summarization(
                create_summarization_middleware(model, backend), operations
            ),
            PatchToolCallsMiddleware(),
        ]
    )
    review = create_tool_review(spec.permissions, spec.interrupt_on, paths)
    if review is not None:
        defaults.append(review)
    if paths is not None:
        defaults.append(paths)
    if operations is not None:
        defaults.append(ToolOriginsMiddleware(operations))
    if skills:
        defaults.append(
            SourceSkillsMiddleware(
                backend=backend, sources=skills, operations=operations
            )
        )
    if remote:
        defaults.append(AsyncSubAgentMiddleware(async_subagents=remote))
    middleware = prepare_delegation_retry(
        _merge_middleware(defaults, custom), spec.checkpointer
    )
    if spec.compaction_tool_enabled:
        summary = next(
            item for item in middleware if item.name == "SummarizationMiddleware"
        )
        if not isinstance(summary, SummarizationMiddleware):
            raise TypeError("the compaction tool requires Deep Agents summarization")
        if any(isinstance(item, SummarizationToolMiddleware) for item in middleware):
            raise ValueError("configure the compaction tool through one entry point")
        middleware.append(ObservedCompactionTool(summary))
    if spec.memory is not None:
        memory = WorkspaceMemory(backend=backend, sources=list(spec.memory))
        memory.operations = operations
        middleware.append(memory)
    middleware.append(
        _ToolRuntimeMiddleware(spec.tool_scope, paths, spec.named_workspaces)
    )
    middleware = _with_attachments(middleware, spec.attachments, operations)
    schemas = [
        spec.state_schema or DeepAgentState,
        *(item.state_schema for item in middleware),
    ]
    delegation.private_state_keys = private_state_fields(schemas)
    graph = create_agent(
        model=model,
        tools=tools,
        system_prompt=spec.system_prompt,
        middleware=middleware,
        response_format=spec.response_format,
        state_schema=spec.state_schema or DeepAgentState,
        context_schema=spec.context_schema,
        checkpointer=spec.checkpointer,
        store=spec.store,
        name=spec.name,
    )
    configuration: RunnableConfig = {
        "recursion_limit": 9999,
        "metadata": {"ls_integration": "tinkerfin", "lc_agent_name": spec.name},
    }
    if spec.checkpointer is not None:
        configuration["configurable"] = {"__pregel_durability": "sync"}
    return cast(AgentGraph, graph.with_config(configuration))
