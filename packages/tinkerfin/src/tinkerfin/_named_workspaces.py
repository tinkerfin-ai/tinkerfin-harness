"""Discover auxiliary resources without moving the agent's default filesystem."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, BaseMessage, ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from tinkerfin_contracts import (
    PreparedWorkspace,
    RunIdentity,
    ToolDescriptor,
    Workspace,
    WorkspacePathResolver,
)

from ._agent_spec import ToolDefinition
from ._file_paths import annotate_filesystem_tools
from ._store import validate_store_backend
from ._store_backend import async_store_backend
from ._tool_proposals import pending_tool_calls
from ._workspace_tools import _RESERVED, merge_workspace_tools, tool_descriptor
from .errors import TinkerFinLifecycleError
from .tools import _ToolRunScope

if TYPE_CHECKING:
    from ._file_paths import _WorkspaceFilePaths

NAMED_WORKSPACES_CHANNEL = "_tinkerfin_named_workspaces"
_json = TypeAdapter(dict[str, JsonValue])


class _WorkspaceDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: str
    tools: dict[str, ToolDescriptor]


_declarations = TypeAdapter(dict[str, _WorkspaceDeclaration])


def named_tool_names(state: Mapping[str, object]) -> set[str]:
    """Return only tool names retained in the current model declaration."""
    return {
        name
        for declaration in _declarations.validate_python(
            state.get(NAMED_WORKSPACES_CHANNEL, {})
        ).values()
        for name in declaration.tools
    }


def validate_workspace_names(
    workspaces: Mapping[str, Workspace[object, BackendProtocol]],
) -> None:
    """Reserve unambiguous tool prefixes before performing resource I/O."""
    for name, workspace in workspaces.items():
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) is None
        ):
            raise ValueError(
                "workspace names must be lowercase identifiers of at most 32 characters"
            )
        if any(other != name and name.startswith(other + "_") for other in workspaces):
            raise ValueError("workspace names must not have overlapping prefixes")
        if any(tool.startswith(name + "_") for tool in _RESERVED):
            raise ValueError("workspace name conflicts with a reserved tool prefix")
        if not isinstance(workspace, Workspace):
            raise TypeError("named workspaces must implement Workspace")


def _persistent_backend(backend: BackendProtocol) -> bool:
    # StateBackend writes into one shared graph channel. Auxiliary resources must
    # keep their files outside that channel so equal paths cannot alias each other.
    if isinstance(backend, StateBackend):
        return False
    if isinstance(backend, CompositeBackend):
        return _persistent_backend(backend.default) and all(
            _persistent_backend(route) for route in backend.routes.values()
        )
    return True


@dataclass(frozen=True, slots=True)
class _OpenedWorkspace:
    prepared: PreparedWorkspace[object, BackendProtocol]
    scope: _ToolRunScope[object]
    filesystem: FilesystemMiddleware
    tools: Mapping[str, BaseTool]
    declaration: _WorkspaceDeclaration


class _WorkspaceBorrow:
    """Enter and exit a provider in one owned task, even when tools run in parallel.

    The run owns this task from creation until close. Cancelling one caller does
    not abandon preparation; run settlement cancels unfinished preparation and
    awaits cleanup. The number of tasks is bounded by the configured workspaces.
    """

    def __init__(
        self,
        name: str,
        workspace: Workspace[object, BackendProtocol],
        identity: RunIdentity,
    ) -> None:
        self.ready: asyncio.Future[_OpenedWorkspace] = (
            asyncio.get_running_loop().create_future()
        )
        self.release = asyncio.Event()
        self.task = asyncio.create_task(
            self._own(name, workspace, identity), name=f"tinkerfin-workspace-{name}"
        )

    async def _own(
        self,
        name: str,
        workspace: Workspace[object, BackendProtocol],
        identity: RunIdentity,
    ) -> None:
        try:
            async with workspace.prepare(identity) as prepared:
                if not isinstance(prepared, PreparedWorkspace) or not isinstance(
                    prepared.backend, BackendProtocol
                ):
                    raise TypeError(
                        "Workspace.prepare must yield a PreparedWorkspace with a BackendProtocol"
                    )
                validate_store_backend(prepared.backend)
                if not _persistent_backend(prepared.backend):
                    raise ValueError(
                        "named workspaces require files outside graph state"
                    )
                prepared = replace(
                    prepared, backend=async_store_backend(prepared.backend)
                )
                scope = _ToolRunScope(
                    identity,
                    prepared.workspace,
                    prepared.workspace_scope_id
                    or f"{name}:{identity.namespace}:{identity.thread_id}",
                )
                try:
                    filesystem = FilesystemMiddleware(
                        backend=prepared.backend,
                        custom_tool_descriptions=prepared.tool_descriptions,
                        tool_token_limit_before_evict=None,
                    )
                    if prepared.file_path_resolver is not None:
                        annotate_filesystem_tools(
                            tuple(
                                tool
                                for tool in filesystem.tools
                                if tool.name != "execute"
                            )
                        )
                    provided = merge_workspace_tools((), prepared.tools, ())
                    tools: dict[str, BaseTool] = {}
                    descriptors: dict[str, ToolDescriptor] = {}
                    for definition in (*filesystem.tools, *provided):
                        if not isinstance(definition, BaseTool):
                            raise TypeError(
                                "prepared workspace tools must be executable tools"
                            )
                        public_name = f"{name}_{definition.name}"
                        if public_name == f"{name}_tools":
                            raise ValueError(
                                "workspace tool 'tools' conflicts with discovery"
                            )
                        tool = definition.model_copy(update={"name": public_name})
                        descriptor = tool_descriptor(tool)
                        assert descriptor is not None
                        tools[public_name] = tool
                        descriptors[public_name] = descriptor
                    self.ready.set_result(
                        _OpenedWorkspace(
                            prepared,
                            scope,
                            filesystem,
                            tools,
                            _WorkspaceDeclaration(
                                scope_id=scope.workspace_scope_id, tools=descriptors
                            ),
                        )
                    )
                    await self.release.wait()
                finally:
                    scope.close()
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)
                # Retrieval also covers cancellation before the first waiter starts.
                self.ready.exception()
            if (
                isinstance(error, asyncio.CancelledError)
                or self.ready.done()
                and self.ready.exception() is None
            ):
                raise

    async def close(self) -> None:
        cancelling_preparation = not self.ready.done()
        if cancelling_preparation:
            self.task.cancel()
        self.release.set()
        try:
            await self.task
        except asyncio.CancelledError:
            # Consume only the owned task's requested shutdown. Cancellation of
            # the closing caller still propagates and cannot become success.
            current = asyncio.current_task()
            if (
                not cancelling_preparation
                or current is not None
                and current.cancelling()
            ):
                raise


class _DiscoverWorkspace(BaseTool):
    registry: NamedWorkspaces = Field(exclude=True)
    workspace_name: str = Field(exclude=True)

    def _run(self, **kwargs: Any) -> str:
        raise NotImplementedError("Workspace discovery requires asynchronous execution")

    async def _arun(self, **kwargs: Any) -> str:
        opened = await self.registry.open(self.workspace_name)
        return json.dumps(
            {
                "workspace": self.workspace_name,
                "instructions": opened.prepared.filesystem_instructions,
                "tools": [
                    tool.model_dump(mode="json")
                    for tool in opened.declaration.tools.values()
                ],
                "skills": [
                    {"directory": source.directory, "names": source.names}
                    for source in opened.prepared.skill_sources
                ],
            },
            ensure_ascii=False,
        )


class NamedWorkspaces:
    """Own lazy resource borrows and validate declarations saved with proposals."""

    def __init__(
        self,
        workspaces: Mapping[str, Workspace[object, BackendProtocol]],
        identity: RunIdentity,
    ) -> None:
        self._workspaces = dict(workspaces)
        self._identity = identity
        self._borrows: dict[str, _WorkspaceBorrow] = {}
        self._closed = False

    def validate_tools(self, tools: Sequence[ToolDefinition]) -> None:
        for tool in tools:
            descriptor = tool_descriptor(tool)
            if descriptor is not None and any(
                descriptor.name.startswith(name + "_") for name in self._workspaces
            ):
                raise ValueError(
                    f"tool {descriptor.name!r} conflicts with a named workspace"
                )

    def discovery_tools(self) -> list[BaseTool]:
        return [
            _DiscoverWorkspace(
                name=f"{name}_tools",
                description=f"Discover tools, file instructions and skills for the {name} workspace. Call before using this resource.",
                args_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                registry=self,
                workspace_name=name,
            )
            for name in self._workspaces
        ]

    def resource_name(self, tool_name: str) -> str | None:
        return next(
            (
                name
                for name in self._workspaces
                if tool_name.startswith(name + "_") and tool_name != name + "_tools"
            ),
            None,
        )

    async def open(self, name: str) -> _OpenedWorkspace:
        if self._closed:
            raise TinkerFinLifecycleError("The tool's run has closed")
        borrow = self._borrows.get(name)
        if borrow is None:
            borrow = self._borrows[name] = _WorkspaceBorrow(
                name, self._workspaces[name], self._identity
            )
        return await asyncio.shield(borrow.ready)

    def _opened(self, name: str) -> _OpenedWorkspace:
        borrow = self._borrows.get(name)
        if borrow is None or not borrow.ready.done():
            raise TinkerFinLifecycleError(
                "workspace tools must be discovered before execution"
            )
        result = borrow.ready.result()
        result.scope.ensure_active()
        return result

    def scope(self, tool_name: str) -> _ToolRunScope[object] | None:
        name = self.resource_name(tool_name)
        return None if name is None else self._opened(name).scope

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            name: _json.validate_python(
                borrow.ready.result().declaration.model_dump(mode="json")
            )
            for name, borrow in self._borrows.items()
            if borrow.ready.done()
            and not borrow.ready.cancelled()
            and borrow.ready.exception() is None
        }

    def model_tools(self) -> list[BaseTool]:
        return [
            tool
            for name in self.snapshot()
            for tool in self._opened(name).tools.values()
        ]

    def descriptor(
        self, tool_name: str, state: Mapping[str, object]
    ) -> ToolDescriptor | None:
        name = self.resource_name(tool_name)
        if name is None:
            return None
        saved = _declarations.validate_python(
            state.get(NAMED_WORKSPACES_CHANNEL, {})
        ).get(name)
        return None if saved is None else saved.tools.get(tool_name)

    async def restore(
        self, state: Mapping[str, object], call: ToolCall | None = None
    ) -> None:
        """Reopen only resources actually used by the pending proposal, then compare.

        Discovery snapshots are saved by the model node, before review. A new
        model turn only sees resources opened in its own run; stale conversation
        history never forces an offline resource to open for a cloud-only task.
        """
        calls = (
            [call]
            if call is not None
            else pending_tool_calls(
                cast(Sequence[BaseMessage], state.get("messages", []))
            )
        )
        saved = _declarations.validate_python(state.get(NAMED_WORKSPACES_CHANNEL, {}))
        for proposal in calls:
            name = self.resource_name(proposal["name"])
            if name is None:
                continue
            declared = saved.get(name)
            if declared is None or proposal["name"] not in declared.tools:
                raise TinkerFinLifecycleError(
                    "workspace tool was not discovered for this proposal"
                )
            current = await self.open(name)
            if current.declaration != declared:
                raise TinkerFinLifecycleError(
                    "workspace authorization or tools changed after proposal; discover the resource again"
                )

    def resolver(self, tool_name: str) -> WorkspacePathResolver | None:
        name = self.resource_name(tool_name)
        return None if name is None else self._opened(name).prepared.file_path_resolver

    async def execute(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        paths: _WorkspaceFilePaths | None,
    ) -> ToolMessage | Command[Any]:
        name = self.resource_name(request.tool_call["name"])
        if name is None:
            return await handler(request)
        opened = self._opened(name)
        tool = opened.tools.get(request.tool_call["name"])
        if tool is None:
            raise TinkerFinLifecycleError("workspace tool is no longer available")
        public_request = request.override(tool=tool)

        def native_call(call: ToolCall) -> ToolCall:
            edited = request.state.get("hitl_edited_tool_calls", {}).get(call["id"])
            effective: ToolCall = (
                {**call, "name": edited["name"], "args": edited["args"]}
                if edited is not None
                else call
                if paths is None
                else paths.prepared_call(call, request.state)
            )
            return {**effective, "name": effective["name"][len(name) + 1 :]}

        # Deep Agents 0.7.19 guards conflicting file mutations using tool names
        # and canonical paths. Project only this resource into that native guard;
        # only the latest proposal owns saved path bindings. Historical calls
        # must remain unchanged when checking the current parallel batch.
        latest = next(
            (
                message
                for message in reversed(request.state["messages"])
                if isinstance(message, AIMessage)
            ),
            None,
        )
        projected = [
            message.model_copy(
                update={
                    "tool_calls": [
                        native_call(call)
                        for call in pending_tool_calls(request.state["messages"])
                        if self.resource_name(call["name"]) == name
                    ]
                }
            )
            if message is latest
            else message
            for message in request.state["messages"]
        ]
        native = public_request.override(
            tool_call=native_call(request.tool_call),
            state={**request.state, "messages": projected},
        )

        async def invoke(_request: ToolCallRequest) -> ToolMessage | Command[Any]:
            return await handler(public_request)

        result = cast(
            ToolMessage | Command[Any],
            await opened.filesystem.awrap_tool_call(native, invoke),  # pyright: ignore[reportUnknownMemberType]
        )
        if isinstance(result, ToolMessage):
            return result.model_copy(update={"name": request.tool_call["name"]})
        raw_update: object = result.update
        if isinstance(raw_update, dict):
            update = cast(dict[str, object], raw_update)
            messages = update.get("messages")
            if isinstance(messages, list):
                return replace(
                    result,
                    update={
                        **update,
                        "messages": [
                            item.model_copy(update={"name": request.tool_call["name"]})
                            if isinstance(item, ToolMessage)
                            else item
                            for item in cast(list[object], messages)
                        ],
                    },
                )
        return result

    async def aclose(self) -> None:
        self._closed = True
        results = await asyncio.gather(
            *(borrow.close() for borrow in self._borrows.values()),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup("Named workspace cleanup failed", failures)
