"""Assemble role-specific agents from explicit framework configuration."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeAlias, cast

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

from ._agent_spec import AgentMiddlewareType, AgentSpec
from ._attachment_agents import _AttachmentMiddleware, preserve_media
from ._hitl import create_tool_review
from ._middleware_resources import prepare_middleware_resources
from ._state_schema import private_state_fields
from ._summarization import ObservedCompactionTool, observe_summarization
from ._tool_runtime import _ToolRuntimeMiddleware
from .media import AttachmentSupport
from .tools import _ToolRunScope

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
) -> AgentMiddlewareType:
    return FilesystemMiddleware(
        backend=backend,
        _permissions=list(permissions),
        system_prompt=None if workspace is None else workspace.filesystem_instructions,
        custom_tool_descriptions=None
        if workspace is None
        else dict(workspace.tool_descriptions),
    )


def _with_attachments(
    middleware: list[AgentMiddlewareType], attachments: AttachmentSupport | None
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
        _AttachmentMiddleware(support),
    ]


def _role_defaults(
    *,
    model: BaseChatModel,
    backend: BackendProtocol,
    skills: Sequence[str] | None,
    permissions: Sequence[FilesystemPermission],
    interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    workspace: PreparedWorkspace[object, BackendProtocol] | None,
) -> list[AgentMiddlewareType]:
    """Share file access, context management, and approvals across Agent roles."""
    defaults: list[AgentMiddlewareType] = [
        _filesystem(backend, permissions, workspace),
        observe_summarization(create_summarization_middleware(model, backend)),
        PatchToolCallsMiddleware(),
    ]
    review = create_tool_review(permissions, interrupt_on)
    if review is not None:
        defaults.append(review)
    if skills is not None:
        defaults.append(SkillsMiddleware(backend=backend, sources=list(skills)))
    return defaults


def _child_stack(
    *,
    model: BaseChatModel,
    backend: BackendProtocol,
    skills: Sequence[str] | None,
    permissions: Sequence[FilesystemPermission],
    custom: Sequence[AgentMiddlewareType],
    interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    workspace: PreparedWorkspace[object, BackendProtocol] | None,
    attachments: AttachmentSupport | None,
    tool_scope: _ToolRunScope[object] | None,
    inherit_slots_only: bool = False,
) -> list[AgentMiddlewareType]:
    defaults = _role_defaults(
        model=model,
        backend=backend,
        skills=skills,
        permissions=permissions,
        interrupt_on=interrupt_on,
        workspace=workspace,
    )
    if inherit_slots_only:
        names = {item.name for item in defaults}
        custom = tuple(item for item in custom if item.name in names)
    return _with_attachments(
        [*_merge_middleware(defaults, custom), _ToolRuntimeMiddleware(tool_scope)],
        attachments,
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
    custom = prepare_middleware_resources(spec.middleware)
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
            child_custom = prepare_middleware_resources(child.get("middleware", ()))
            child_stack = _child_stack(
                model=child_model,
                backend=backend,
                skills=child.get("skills") or None,
                permissions=child.get("permissions", spec.permissions),
                custom=child_custom,
                interrupt_on=child.get("interrupt_on", spec.interrupt_on),
                workspace=workspace,
                attachments=spec.attachments,
                tool_scope=spec.tool_scope,
            )
            native: NativeSubAgent = {
                "name": child["name"],
                "description": child["description"],
                "system_prompt": child["system_prompt"],
                "model": child_model,
                "tools": child.get("tools", spec.tools),
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
                "tools": spec.tools,
                "middleware": _child_stack(
                    model=model,
                    backend=backend,
                    skills=spec.skills,
                    permissions=spec.permissions,
                    custom=custom,
                    interrupt_on=spec.interrupt_on,
                    workspace=workspace,
                    attachments=spec.attachments,
                    tool_scope=spec.tool_scope,
                    inherit_slots_only=True,
                ),
            },
        )
    delegation = SubAgentMiddleware(
        backend=backend, subagents=inline, state_schema=spec.state_schema
    )
    defaults: list[AgentMiddlewareType] = []
    if spec.skills is not None:
        defaults.append(SkillsMiddleware(backend=backend, sources=list(spec.skills)))
    defaults.extend(
        [
            _filesystem(backend, spec.permissions, workspace),
            delegation,
            observe_summarization(create_summarization_middleware(model, backend)),
            PatchToolCallsMiddleware(),
        ]
    )
    review = create_tool_review(spec.permissions, spec.interrupt_on)
    if review is not None:
        defaults.append(review)
    if remote:
        defaults.append(AsyncSubAgentMiddleware(async_subagents=remote))
    middleware = _merge_middleware(defaults, custom)
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
        middleware.append(MemoryMiddleware(backend=backend, sources=list(spec.memory)))
    middleware.append(_ToolRuntimeMiddleware(spec.tool_scope))
    middleware = _with_attachments(middleware, spec.attachments)
    schemas = [
        spec.state_schema or DeepAgentState,
        *(item.state_schema for item in middleware),
    ]
    delegation.private_state_keys = private_state_fields(schemas)
    graph = create_agent(
        model=model,
        tools=spec.tools,
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
