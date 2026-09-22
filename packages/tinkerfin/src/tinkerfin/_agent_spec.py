"""Trusted agent configuration shared by execution and planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Generic, TypeAlias, TypeVar, cast

from deepagents import AsyncSubAgent, CompiledSubAgent, DeepAgentState
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import AgentMiddleware, InterruptOnConfig
from langchain.agents.structured_output import ResponseFormat
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from langgraph.typing import ContextT

from tinkerfin_contracts import Workspace

from .media import AttachmentSupport
from .subagents import SubAgent
from .tools import _ToolRunScope

ToolDefinition: TypeAlias = BaseTool | Callable[..., Any] | dict[str, Any]
AgentMiddlewareType: TypeAlias = AgentMiddleware[Any, Any, Any]
SubagentDefinition: TypeAlias = SubAgent | CompiledSubAgent | AsyncSubAgent
CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)
AgentBackend: TypeAlias = BackendProtocol | Workspace[object, BackendProtocol] | None
ResponseFormatType: TypeAlias = ResponseFormat[Any] | type | dict[str, Any] | None
ValueT = TypeVar("ValueT")


def copy_configuration(value: ValueT) -> ValueT:
    """Copy declaration containers while borrowing models, tools, and resources."""
    if isinstance(value, dict):
        return cast(
            ValueT,
            {
                key: copy_configuration(item)
                for key, item in cast(Mapping[object, object], value).items()
            },
        )
    if isinstance(value, list):
        return cast(
            ValueT, [copy_configuration(item) for item in cast(Sequence[object], value)]
        )
    if isinstance(value, tuple):
        return cast(
            ValueT,
            tuple(copy_configuration(item) for item in cast(tuple[object, ...], value)),
        )
    if isinstance(value, set):
        return cast(ValueT, set(cast(set[object], value)))
    return value


@dataclass(frozen=True, slots=True)
class AgentSpec(Generic[ContextT]):
    """Describe one agent without opening resources or resolving a model."""

    model: str | BaseChatModel
    tools: Sequence[ToolDefinition] = ()
    system_prompt: str | SystemMessage | None = None
    middleware: Sequence[AgentMiddlewareType] = ()
    compaction_tool_enabled: bool = False
    subagents: Sequence[SubagentDefinition] = ()
    skills: Sequence[str] | None = None
    memory: Sequence[str] | None = None
    permissions: Sequence[FilesystemPermission] = ()
    backend: AgentBackend = None
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None
    response_format: ResponseFormatType = None
    state_schema: type[DeepAgentState] | None = None
    context_schema: type[ContextT] | None = None
    checkpointer: CheckpointSaver | None = None
    store: BaseStore | None = None
    name: str | None = None
    attachments: AttachmentSupport | None = None
    tool_scope: _ToolRunScope[object] | None = None

    def snapshot(self) -> AgentSpec[ContextT]:
        """Retain a fresh declaration without copying borrowed resources."""
        return replace(
            self,
            tools=tuple(copy_configuration(self.tools)),
            middleware=tuple(self.middleware),
            subagents=tuple(copy_configuration(self.subagents)),
            skills=None if self.skills is None else tuple(self.skills),
            memory=None if self.memory is None else tuple(self.memory),
            permissions=tuple(self.permissions),
            interrupt_on=copy_configuration(self.interrupt_on),
            response_format=copy_configuration(self.response_format),
        )
