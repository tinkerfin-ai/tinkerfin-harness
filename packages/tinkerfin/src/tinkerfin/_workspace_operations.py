"""Bind workspace I/O to its durable proposal or framework execution step."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Annotated, Any, cast

from deepagents.middleware.memory import (
    MemoryMiddleware,
    MemoryState,
    MemoryStateUpdate,
)
from deepagents.middleware.summarization import SUMMARIZATION_SESSION_ID_KEY
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage, ToolCall
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_config
from langgraph.runtime import ExecutionInfo, Runtime
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, field_validator

from tinkerfin_contracts import (
    RunIdentity,
    ToolExecutionScope,
    bind_workspace_operation,
)
from tinkerfin_contracts.workspace_tools import OperationPurpose

from ._delegation_journal import content_digest
from .errors import TinkerFinLifecycleError
from .tools import _ToolRunScope

TOOL_ORIGINS_CHANNEL = "_tinkerfin_tool_origins"
WORKSPACE_ORIGIN_CHANNEL = "_tinkerfin_workspace_origin"
_arguments = TypeAdapter(dict[str, JsonValue])


class _FrameworkOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identity: RunIdentity
    workspace_scope_id: str
    invocation_id: str
    graph_namespace: tuple[str, ...]

    @field_validator("graph_namespace", mode="before")
    @classmethod
    def _namespace(cls, value: object) -> object:
        return tuple(cast(list[object], value)) if isinstance(value, list) else value


class _ToolOrigin(_FrameworkOrigin):
    message_id: str
    tool_name: str
    arguments: dict[str, JsonValue]


class _ToolOriginState(AgentState):
    _tinkerfin_workspace_origin: Annotated[dict[str, JsonValue], PrivateStateAttr]
    _tinkerfin_tool_origins: Annotated[
        dict[str, dict[str, JsonValue]], PrivateStateAttr
    ]


_origins = TypeAdapter(dict[str, _ToolOrigin])


def _coordinates(execution: ExecutionInfo | None) -> tuple[str, str, str]:
    if execution is not None:
        return (
            execution.checkpoint_id,
            execution.checkpoint_ns.rpartition("|")[0],
            execution.task_id,
        )
    # Non-persistent native graphs still have a trusted node location. Their
    # current Run and proposal IDs bound invocation identity; they promise no
    # checkpoint recovery. No wall clock or random operation identity is used.
    metadata = get_config().get("metadata", {})
    namespace = metadata.get("langgraph_checkpoint_ns", "")
    node = metadata.get("langgraph_node", "")
    if not isinstance(namespace, str) or not isinstance(node, str):
        raise TinkerFinLifecycleError(
            "workspace operation has invalid graph coordinates"
        )
    return "", namespace.rpartition("|")[0], node


class WorkspaceOperations:
    """Use one Run borrow to issue stable tool and framework I/O scopes."""

    def __init__(self, owner: _ToolRunScope[object]) -> None:
        self._owner = owner

    @property
    def identity(self) -> RunIdentity:
        return self._owner.identity

    def _scope(
        self,
        *,
        origin: RunIdentity,
        invocation: str,
        namespace: tuple[str, ...],
        purpose: OperationPurpose,
        tool_call_id: str | None = None,
        steps: tuple[str, ...] = (),
    ) -> ToolExecutionScope:
        # Workspace access also checks that the owning Run has not released its
        # resources. Context inheritance alone cannot reopen a finished borrow.
        self._owner.ensure_active()
        return ToolExecutionScope(
            identity=self.identity,
            origin_identity=origin,
            workspace_scope_id=self._owner.workspace_scope_id,
            invocation_id=invocation,
            graph_namespace=namespace,
            purpose=purpose,
            tool_call_id=tool_call_id,
            steps=steps,
        )

    def tool(
        self,
        call: ToolCall,
        state: Mapping[str, object],
        *,
        purpose: OperationPurpose = "tool",
        step: str = "execute",
        emit_output: Callable[[JsonValue], Awaitable[None]] | None = None,
    ) -> AbstractContextManager[ToolExecutionScope]:
        """Use the saved proposal even when an approved edit changes its action."""
        identifier = call.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise TinkerFinLifecycleError("workspace tool has no invocation identity")
        origin = _origins.validate_python(state.get(TOOL_ORIGINS_CHANNEL, {})).get(
            identifier
        )
        if (
            origin is None
            or origin.identity.thread != self.identity.thread
            or origin.workspace_scope_id != self._owner.workspace_scope_id
        ):
            raise TinkerFinLifecycleError("workspace tool has no saved proposal origin")
        messages = state.get("messages")
        if not isinstance(messages, list) or not any(
            isinstance(message, AIMessage)
            and message.id == origin.message_id
            and any(
                item.get("id") == identifier
                and item["name"] == origin.tool_name
                and item["args"] == origin.arguments
                for item in message.tool_calls
            )
            for message in cast(list[object], messages)
        ):
            raise TinkerFinLifecycleError("workspace origin lost its model proposal")
        return bind_workspace_operation(
            self._scope(
                origin=origin.identity,
                invocation=origin.invocation_id,
                namespace=origin.graph_namespace,
                purpose=purpose,
                tool_call_id=identifier,
                steps=(step,),
            ),
            emit_output=emit_output,
        )

    def prepare_framework(
        self, execution: ExecutionInfo | None, state: Mapping[str, object]
    ) -> dict[str, object]:
        """Persist a real execution origin before its middleware can issue I/O."""
        checkpoint, namespace, task = _coordinates(execution)
        origin = _FrameworkOrigin(
            identity=self.identity,
            workspace_scope_id=self._owner.workspace_scope_id,
            invocation_id=content_digest(
                [self.identity.model_dump(mode="json"), checkpoint, namespace, task]
            ),
            graph_namespace=tuple(namespace.split("|")) if namespace else (),
        )
        update: dict[str, object] = {
            WORKSPACE_ORIGIN_CHANNEL: origin.model_dump(mode="json")
        }
        if state.get(SUMMARIZATION_SESSION_ID_KEY) is None:
            # Deep Agents otherwise chooses a new UUID before archiving, then
            # persists it only after model completion. Seed its existing private
            # channel first so replay cannot move an already attempted write.
            update[SUMMARIZATION_SESSION_ID_KEY] = "session_" + content_digest(
                [
                    self.identity.thread.model_dump(mode="json"),
                    self._owner.workspace_scope_id,
                    namespace,
                ]
            )
        return update

    def framework(
        self,
        purpose: OperationPurpose,
        key: str,
        state: Mapping[str, object],
    ) -> AbstractContextManager[ToolExecutionScope]:
        """Use the checkpointed middleware origin, including after Run recovery."""
        value = state.get(WORKSPACE_ORIGIN_CHANNEL)
        if value is None:
            raise TinkerFinLifecycleError(
                "workspace middleware has no saved execution origin"
            )
        origin = _FrameworkOrigin.model_validate(value)
        if (
            origin.identity.thread != self.identity.thread
            or origin.workspace_scope_id != self._owner.workspace_scope_id
        ):
            raise TinkerFinLifecycleError(
                "workspace middleware origin belongs to another thread"
            )
        return bind_workspace_operation(
            self._scope(
                origin=origin.identity,
                invocation=content_digest([origin.invocation_id, purpose, key]),
                namespace=origin.graph_namespace,
                purpose=purpose,
                steps=(key,),
            )
        )


class ToolOriginsMiddleware(AgentMiddleware):
    """Persist proposal ownership before any path preparation or approval."""

    state_schema = _ToolOriginState

    def __init__(self, operations: WorkspaceOperations) -> None:
        self.operations = operations

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object]:
        return self.operations.prepare_framework(runtime.execution_info, state)

    async def abefore_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object]:
        return self.operations.prepare_framework(runtime.execution_info, state)

    async def aafter_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object]:
        message = next(
            (
                item
                for item in reversed(state["messages"])
                if isinstance(item, AIMessage)
            ),
            None,
        )
        records: dict[str, dict[str, JsonValue]] = {}
        previous = _origins.validate_python(state.get(TOOL_ORIGINS_CHANNEL, {}))
        if message is not None and message.tool_calls:
            if not message.id:
                raise TinkerFinLifecycleError(
                    "workspace proposals require a message ID"
                )
            checkpoint, namespace, task = _coordinates(runtime.execution_info)
            framework_origin = _FrameworkOrigin.model_validate(
                state.get(WORKSPACE_ORIGIN_CHANNEL)
            )
            for call in message.tool_calls:
                identifier = call.get("id")
                if not isinstance(identifier, str) or not identifier:
                    raise TinkerFinLifecycleError(
                        "workspace proposals require tool IDs"
                    )
                arguments = _arguments.validate_python(call["args"])
                known = previous.get(identifier)
                if known is not None and known.message_id == message.id:
                    if known.tool_name != call["name"] or known.arguments != arguments:
                        raise TinkerFinLifecycleError(
                            "workspace proposal changed during replay"
                        )
                    origin = known
                else:
                    origin = _ToolOrigin(
                        identity=framework_origin.identity,
                        workspace_scope_id=framework_origin.workspace_scope_id,
                        invocation_id=content_digest(
                            [
                                framework_origin.invocation_id,
                                checkpoint,
                                namespace,
                                task,
                                message.id,
                                identifier,
                            ]
                        ),
                        graph_namespace=tuple(namespace.split("|"))
                        if namespace
                        else (),
                        message_id=message.id,
                        tool_name=call["name"],
                        arguments=arguments,
                    )
                records[identifier] = _arguments.validate_python(
                    origin.model_dump(mode="json")
                )
        return {TOOL_ORIGINS_CHANNEL: records}


class WorkspaceMemory(MemoryMiddleware):
    """Associate memory discovery with its actual middleware execution."""

    operations: WorkspaceOperations | None = None

    @property
    def name(self) -> str:
        return "MemoryMiddleware"

    async def abefore_agent(
        self, state: MemoryState, runtime: Runtime[Any], config: RunnableConfig
    ) -> MemoryStateUpdate | None:
        with (
            nullcontext()
            if self.operations is None
            else self.operations.framework("memory", "load-memory", state)
        ):
            return await super().abefore_agent(state, runtime, config)


__all__ = ["TOOL_ORIGINS_CHANNEL", "ToolOriginsMiddleware", "WorkspaceOperations"]
