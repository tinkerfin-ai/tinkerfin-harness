"""Execute registered tools with a real, revocable workspace capability."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated, Generic, TypeVar, get_args, get_origin

from deepagents.backends.protocol import BackendProtocol
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolArg, ToolException
from langchain_core.tools.base import get_all_basemodel_annotations
from pydantic import BaseModel, JsonValue, TypeAdapter

from tinkerfin_contracts import (
    PreparedWorkspace,
    SkillSource,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    Workspace,
    WorkspaceTool,
    borrow_tool_context,
    current_workspace_operation,
    resolve_tool_arguments,
)

from .errors import RemoteError, RemoteErrorCode
from .models import WorkspaceRequest

WorkspaceT = TypeVar("WorkspaceT")
ToolContextArg = Annotated[ToolContext[WorkspaceT], InjectedToolArg]
_json: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_schema = TypeAdapter(dict[str, JsonValue])


class _Tool:
    def __init__(self, tool: BaseTool) -> None:
        if tool.response_format != "content":
            raise TypeError("remote tools return content, not arbitrary artifacts")
        self.tool = tool.model_copy(
            update={"handle_tool_error": False, "handle_validation_error": False}
        )
        self.error_handler = tool.handle_tool_error
        self.contexts: list[str] = []
        for name, annotation in get_all_basemodel_annotations(
            tool.get_input_schema()
        ).items():
            args = get_args(annotation)
            base = args[0] if get_origin(annotation) is Annotated else annotation
            metadata = args[1:] if get_origin(annotation) is Annotated else ()
            injected = any(
                item is InjectedToolArg
                or isinstance(item, InjectedToolArg)
                or (isinstance(item, type) and issubclass(item, InjectedToolArg))
                for item in metadata
            )
            if base is ToolContext or get_origin(base) is ToolContext:
                if not injected:
                    raise TypeError("ToolContext must use ToolContextArg injection")
                self.contexts.append(name)
            elif injected or getattr(base, "__module__", "").startswith("langgraph"):
                raise TypeError(
                    "remote tools accept ToolContextArg, not Graph capabilities"
                )
        selected = tool.tool_call_schema
        if isinstance(selected, dict):
            schema = selected
        elif issubclass(selected, BaseModel):
            schema = selected.model_json_schema()
        else:
            raise TypeError("remote tools require current Pydantic argument schemas")
        self.descriptor = ToolDescriptor(
            name=tool.name,
            description=tool.description,
            input_schema=_schema.validate_python(schema, strict=True),
        )
        properties = self.descriptor.input_schema.get("properties", {})
        if not isinstance(properties, dict):
            raise TypeError("registered tool requires an object argument schema")
        if set(get_all_basemodel_annotations(tool.get_input_schema())).difference(
            properties, self.contexts
        ):
            raise TypeError("remote tools accept only ToolContextArg injections")

    def bind(
        self, prepared: PreparedWorkspace[WorkspaceT, BackendProtocol]
    ) -> WorkspaceTool:
        async def invoke(arguments: Mapping[str, JsonValue]) -> ToolResult:
            scope = current_workspace_operation()
            if scope.workspace_scope_id != prepared.workspace_scope_id:
                raise RemoteError(
                    RemoteErrorCode.REJECTED, "Tool belongs to another workspace"
                )
            if any(name in arguments for name in self.contexts):
                raise RemoteError(
                    RemoteErrorCode.REJECTED,
                    "Tool arguments cannot inject capabilities",
                )
            if prepared.file_path_resolver is not None:
                resolved = await resolve_tool_arguments(
                    self.descriptor,
                    arguments,
                    prepared.file_path_resolver,
                )
                if resolved != dict(arguments):
                    raise RemoteError(
                        RemoteErrorCode.REJECTED,
                        "Approved tool paths changed before execution",
                    )
            with borrow_tool_context(prepared.workspace, scope) as context:
                values: dict[str, object] = dict(arguments)
                values.update({name: context for name in self.contexts})
                try:
                    result: object = await self.tool.ainvoke(values)
                except ToolException as error:
                    handler = self.error_handler
                    if not handler:
                        raise
                    message = (
                        handler(error)
                        if callable(handler)
                        else handler
                        if isinstance(handler, str)
                        else str(error)
                    )
                    return ToolResult(
                        value=_json.validate_python(message, strict=True), is_error=True
                    )
            if isinstance(result, ToolMessage):
                if result.artifact is not None:
                    raise RemoteError(
                        RemoteErrorCode.PROTOCOL,
                        "Remote tools return JSON content, not artifacts",
                    )
                return ToolResult(
                    value=_json.validate_python(result.content, strict=True),
                    is_error=result.status == "error",
                )
            return ToolResult(value=_json.validate_python(result, strict=True))

        return WorkspaceTool(self.descriptor, invoke)


class ToolExecutor(Generic[WorkspaceT]):
    """Prepare standalone tools against a host-owned workspace manager.

    Args:
        workspace: Already-open workspace manager. The executor owns its prepare
            borrow, never the manager or the authorized directory.
        tools: Registered LangChain tools. Inject only ToolContextArg; Graph state,
            Runtime, Store, commands and arbitrary Python results are unsupported.
        skill_sources: Host-authorized skill directories visible through the backend.

    Construction performs no I/O. Pass this executor through ToolWorker's
    open_workspace context; the worker binds execution identity and owns all
    prepared tool borrows.
    """

    def __init__(
        self,
        *,
        workspace: Workspace[WorkspaceT, BackendProtocol],
        tools: Sequence[BaseTool] = (),
        skill_sources: Sequence[SkillSource] = (),
    ) -> None:
        """Capture declarations without opening or taking ownership of the manager."""
        self._workspace = workspace
        self._tools = tuple(_Tool(tool) for tool in tools)
        names = [tool.descriptor.name for tool in self._tools]
        if len(names) != len(set(names)):
            raise ValueError("registered tool names must be unique")
        self._skills = tuple(skill_sources)

    @asynccontextmanager
    async def _prepare(
        self,
        request: WorkspaceRequest,
    ) -> AsyncGenerator[PreparedWorkspace[WorkspaceT, BackendProtocol]]:
        """Own one resource borrow and revoke every tool when it ends."""
        async with self._workspace.prepare(request.identity) as original:
            names = {entry.descriptor.name for entry in original.tools}
            if names.intersection(tool.descriptor.name for tool in self._tools):
                raise ValueError(
                    "registered tools conflict with prepared workspace tools"
                )
            prepared = replace(
                original,
                workspace_scope_id=request.workspace_scope_id,
                skill_sources=(*original.skill_sources, *self._skills),
            )
            active = True
            tools: list[WorkspaceTool] = []
            for entry in (
                *prepared.tools,
                *(tool.bind(prepared) for tool in self._tools),
            ):

                async def invoke(
                    arguments: Mapping[str, JsonValue],
                    *,
                    entry: WorkspaceTool = entry,
                ) -> ToolResult:
                    if not active:
                        raise RemoteError(
                            RemoteErrorCode.CLOSED, "Workspace tool borrow has ended"
                        )
                    scope = current_workspace_operation()
                    if (
                        scope.identity != request.identity
                        or scope.workspace_scope_id != request.workspace_scope_id
                    ):
                        raise RemoteError(
                            RemoteErrorCode.REJECTED,
                            "Tool execution crossed its prepared resource borrow",
                        )
                    return await entry.invoke(arguments)

                tools.append(WorkspaceTool(entry.descriptor, invoke))
            try:
                yield replace(prepared, tools=tuple(tools))
            finally:
                active = False


__all__ = ["ToolContextArg", "ToolExecutor"]
