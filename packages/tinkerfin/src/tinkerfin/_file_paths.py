"""Bind declared filesystem arguments before review and recheck them at execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import nullcontext
from copy import deepcopy
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict, cast

from deepagents.middleware.filesystem import (
    DELETE_TOOL_DESCRIPTION,
    FilesystemMiddleware,
)
from jsonschema import Draft202012Validator
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, create_model
from pydantic.fields import FieldInfo

from tinkerfin_contracts import (
    FilePath,
    InvalidToolArguments,
    ToolDescriptor,
    WorkspacePathResolver,
    resolve_tool_arguments,
)
from tinkerfin_contracts.workspace_tools import FILE_PATH_METADATA

from ._agent_spec import ToolDefinition
from ._tool_proposals import pending_tool_calls
from ._workspace_operations import WorkspaceOperations
from ._workspace_tools import tool_descriptor
from .errors import TinkerFinLifecycleError

if TYPE_CHECKING:
    from ._named_workspaces import NamedWorkspaces

_FS_TOOL_PATH_ARGS: dict[
    str,
    tuple[
        Literal["read", "write"],
        Literal["path", "file_path"],
        Literal["exact", "bulk"],
        str | None,
    ],
] = {
    "ls": ("read", "path", "bulk", None),
    "read_file": ("read", "file_path", "exact", None),
    "write_file": ("write", "file_path", "exact", None),
    "edit_file": ("write", "file_path", "exact", None),
    "delete": ("write", "file_path", "bulk", None),
    "glob": ("read", "path", "bulk", "pattern"),
    "grep": ("read", "path", "bulk", None),
}
FILE_TARGETS_CHANNEL = "_tinkerfin_file_targets"
FILE_CONTRACT_CHANNEL = "_tinkerfin_file_contract"
_JSON = TypeAdapter(dict[str, JsonValue])


class _FileTarget(BaseModel):
    """Retain a complete proposal and its canonical arguments in private state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    message_id: str
    tool_name: str
    requested: dict[str, JsonValue]
    resolved: dict[str, JsonValue]


class _FilePathState(AgentState):
    _tinkerfin_file_targets: Annotated[
        dict[str, dict[str, JsonValue]], PrivateStateAttr
    ]


class FileContractState(AgentState):
    """Retain the file declarations presented with the current model proposal."""

    _tinkerfin_file_contract: Annotated[str, PrivateStateAttr]
    _tinkerfin_named_workspaces: Annotated[dict[str, JsonValue], PrivateStateAttr]


def file_contract_digest(
    paths: _WorkspaceFilePaths | None, state: Mapping[str, object] | None = None
) -> str:
    """Distinguish a complete file capability set from absent path resolution."""
    return hashlib.sha256(b"null").hexdigest() if paths is None else paths.digest(state)


def validate_file_contract(
    state: Mapping[str, object], paths: _WorkspaceFilePaths | None
) -> str:
    """Refuse changed or missing proposal evidence before review or execution."""
    current = file_contract_digest(paths, state)
    if state.get(FILE_CONTRACT_CHANNEL) != current:
        raise TinkerFinLifecycleError(
            "workspace file declarations changed after proposal; "
            "submit the operation again"
        )
    return current


_TARGETS = TypeAdapter(dict[str, _FileTarget])


class _PathSchemaFields(TypedDict, total=False):
    path: tuple[type[Any] | None, FieldInfo]
    file_path: tuple[type[Any] | None, FieldInfo]


def _has_paths(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return FILE_PATH_METADATA in value or any(
            _has_paths(item) for item in value.values()
        )
    return isinstance(value, list) and any(_has_paths(item) for item in value)


def annotate_filesystem_tools(tools: Sequence[BaseTool]) -> None:
    """Describe native path arguments before any tool names are qualified."""
    for tool in tools:
        schema = tool.args_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise TypeError("workspace file tools require a model input schema")
        operation, argument, _scope, _pattern = _FS_TOOL_PATH_ARGS[tool.name]
        path_field = deepcopy(schema.model_fields[argument])
        default_root = tool.name in {"glob", "grep"}
        path_field.metadata.append(FilePath(operation, default_root=default_root))
        path_field.description = (
            "Path to the target file or directory. Follow the workspace filesystem "
            "instructions for supported path forms and the default root."
        )
        if default_root:
            path_field.description += (
                " Omit to use the workspace's default search root."
            )
        fields: _PathSchemaFields = {}
        fields[argument] = (path_field.annotation, path_field)
        tool.args_schema = create_model(
            schema.__name__, __base__=schema, __doc__=schema.__doc__, **fields
        )
        if (
            tool.name == "delete"
            and tool.description == DELETE_TOOL_DESCRIPTION.strip()
        ):
            tool.description = tool.description.replace(
                "given absolute path", "given path"
            )


class _WorkspaceFilePaths(AgentMiddleware):
    """Keep raw model history and exact approved execution arguments separate.

    LangChain 1.4.2 invokes after-model hooks in reverse declaration order.
    Construction places origin capture after this hook and this hook after review.
    Execution rechecks canonical values against the current filesystem and rejects
    a changed target before invoking the tool.
    """

    state_schema = _FilePathState

    def __init__(
        self,
        resolver: WorkspacePathResolver | None,
        tools: Sequence[BaseTool],
        declared: Sequence[ToolDefinition] = (),
        operations: WorkspaceOperations | None = None,
        named: NamedWorkspaces | None = None,
    ) -> None:
        self._resolver = resolver
        self._named = named
        self._operations = operations
        self._file_tools = {
            tool.name: tool
            for tool in tools
            if resolver is not None and tool.name in _FS_TOOL_PATH_ARGS
        }
        annotate_filesystem_tools(tuple(self._file_tools.values()))
        self._descriptors = {
            descriptor.name: descriptor
            for definition in (
                *self._file_tools.values(),
                *(declared if resolver is not None else ()),
            )
            for descriptor in (tool_descriptor(definition),)
            if descriptor is not None and _has_paths(descriptor.input_schema)
        }
        # Bind the entire role's file declarations so reviewer edits cannot select
        # a different tool whose access changed while the original call waited.
        # The model hook saves this evidence before path preparation and review;
        # tool execution rechecks it even after approval has already completed.
        self.contract_digest = hashlib.sha256(
            json.dumps(
                {
                    name: descriptor.model_dump(mode="json")
                    for name, descriptor in self._descriptors.items()
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def digest(self, state: Mapping[str, object] | None) -> str:
        if self._named is None:
            return self.contract_digest
        from ._named_workspaces import NAMED_WORKSPACES_CHANNEL

        return hashlib.sha256(
            json.dumps(
                [
                    self.contract_digest,
                    {} if state is None else state.get(NAMED_WORKSPACES_CHANNEL, {}),
                ],
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()

    async def restore(
        self, state: Mapping[str, object], call: ToolCall | None = None
    ) -> None:
        if self._named is not None:
            await self._named.restore(state, call)

    def descriptor(
        self, name: str, state: Mapping[str, object] | None
    ) -> ToolDescriptor | None:
        declared = self._descriptors.get(name)
        if declared is not None or self._named is None or state is None:
            return declared
        descriptor = self._named.descriptor(name, state)
        return (
            descriptor
            if descriptor is not None
            and _has_paths(descriptor.input_schema)
            and self._named.resolver(name) is not None
            else None
        )

    async def resolve_call(
        self,
        call: ToolCall,
        *,
        state: Mapping[str, object] | None = None,
        recheck: bool = False,
        step: str = "prepare-paths",
    ) -> ToolCall:
        descriptor = self.descriptor(call["name"], state)
        if descriptor is None:
            return call
        if state is not None:
            await self.restore(state, call)
        resolver = (
            None if self._named is None else self._named.resolver(call["name"])
        ) or self._resolver
        if resolver is None:
            return call
        if self._operations is not None and state is None:
            raise TinkerFinLifecycleError("path resolution requires the saved proposal")
        with (
            nullcontext()
            if self._operations is None or state is None
            else self._operations.tool(call, state, purpose="paths", step=step)
        ):
            try:
                resolved = await resolve_tool_arguments(
                    descriptor, _JSON.validate_python(call["args"]), resolver
                )
            except InvalidToolArguments:
                # Native ToolNode owns invalid argument feedback; no path or effect
                # is accessed when the declared schema rejects the input.
                return call
        if recheck and resolved != call["args"]:
            raise TinkerFinLifecycleError(
                "file target changed after preparation; submit the operation again"
            )
        return {**call, "args": resolved}

    def prepared_call(self, call: ToolCall, state: Mapping[str, object]) -> ToolCall:
        descriptor = self.descriptor(call["name"], state)
        if descriptor is None:
            return call
        tool_id = call.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            raise TinkerFinLifecycleError(
                "file operation has no stable proposal identity"
            )
        target = _TARGETS.validate_python(state.get(FILE_TARGETS_CHANNEL, {})).get(
            tool_id
        )
        if target is None:
            if not Draft202012Validator(descriptor.input_schema).is_valid(call["args"]):  # pyright: ignore[reportUnknownMemberType]
                return call
            raise TinkerFinLifecycleError("file operation has no saved target binding")
        messages = state.get("messages")
        if not isinstance(messages, list) or not any(
            isinstance(message, AIMessage)
            and message.id == target.message_id
            and any(
                candidate["id"] == tool_id
                and candidate["name"] == target.tool_name
                and candidate["args"] == target.requested
                for candidate in message.tool_calls
            )
            for message in cast(list[object], messages)
        ):
            raise TinkerFinLifecycleError(
                "file target has no matching original proposal"
            )
        if target.tool_name != call["name"] or call["args"] not in (
            target.requested,
            target.resolved,
        ):
            raise TinkerFinLifecycleError("file target changed after preparation")
        return {**call, "args": deepcopy(target.resolved)}

    def filesystem_request(self, request: ToolCallRequest) -> ToolCallRequest:
        """Project effective built-in paths only for native parallel mutation guards."""
        edited = request.state.get("hitl_edited_tool_calls", {})
        selected = edited.get(request.tool_call["id"])
        if self._file_tools.get(request.tool_call["name"]) is not request.tool and (
            selected is None or selected["name"] not in self._file_tools
        ):
            return request

        def effective(call: ToolCall) -> ToolCall:
            action = edited.get(call["id"])
            return (
                self.prepared_call(call, request.state)
                if action is None
                else {
                    **call,
                    "name": action["name"],
                    "args": action["args"],
                }
            )

        message = next(
            (
                item
                for item in reversed(request.state["messages"])
                if isinstance(item, AIMessage)
            ),
            None,
        )
        if message is None:
            raise TinkerFinLifecycleError("file operation has no original proposal")
        projected = message.model_copy(
            update={
                "tool_calls": [
                    effective(call)
                    for call in pending_tool_calls(request.state["messages"])
                ]
            },
            deep=True,
        )
        state = {
            **request.state,
            "messages": [
                projected if item is message else item
                for item in request.state["messages"]
            ],
        }
        return request.override(tool_call=effective(request.tool_call), state=state)

    async def prepare_execution(self, request: ToolCallRequest) -> ToolCallRequest:
        if self.descriptor(request.tool_call["name"], request.state) is None:
            return request
        edited = request.state.get("hitl_edited_tool_calls", {}).get(
            request.tool_call["id"]
        )
        if edited is not None:
            if edited != {
                "name": request.tool_call["name"],
                "args": request.tool_call["args"],
            }:
                raise TinkerFinLifecycleError(
                    "file operation changed after its reviewed edit"
                )
            effective = request.tool_call
        else:
            effective = self.prepared_call(request.tool_call, request.state)
        return request.override(
            tool_call=await self.resolve_call(
                effective, state=request.state, recheck=True, step="recheck-paths"
            )
        )

    def after_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object] | None:
        raise NotImplementedError("Workspace file paths require asynchronous execution")

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
        targets: dict[str, dict[str, JsonValue]] = {}
        if message is not None:
            for call in pending_tool_calls(state["messages"]):
                descriptor = self.descriptor(call["name"], state)
                if descriptor is None or not Draft202012Validator(
                    descriptor.input_schema
                ).is_valid(call["args"]):  # pyright: ignore[reportUnknownMemberType]
                    continue
                effective = await self.resolve_call(call, state=state)
                if not message.id or not call["id"]:
                    raise TinkerFinLifecycleError(
                        "file proposals require stable message and tool identities"
                    )
                target = _FileTarget(
                    message_id=message.id,
                    tool_name=call["name"],
                    requested=call["args"],
                    resolved=effective["args"],
                )
                targets[call["id"]] = _JSON.validate_python(
                    target.model_dump(mode="json")
                )
        return {FILE_TARGETS_CHANNEL: targets}


class _WorkspaceFilesystem(FilesystemMiddleware):
    """Preserve native guards and scope both execution and result offloading."""

    file_paths: _WorkspaceFilePaths | None = None
    operations: WorkspaceOperations | None = None

    @property
    def name(self) -> str:
        return "FilesystemMiddleware"

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        with (
            nullcontext()
            if self.operations is None
            else self.operations.framework("media", "filesystem-model", request.state)
        ):
            return await super().awrap_model_call(request, handler)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if self.file_paths is not None:
            await self.file_paths.restore(request.state)
        projected = (
            request
            if self.file_paths is None
            else self.file_paths.filesystem_request(request)
        )

        async def continue_original(
            prepared: ToolCallRequest,
        ) -> ToolMessage | Command[Any]:
            return await handler(
                prepared.override(
                    tool_call=request.tool_call
                    if prepared.tool_call == projected.tool_call
                    and prepared.tool is projected.tool
                    else prepared.tool_call,
                    state={**prepared.state, "messages": request.state["messages"]},
                )
            )

        with (
            nullcontext()
            if self.operations is None
            else self.operations.tool(
                request.tool_call,
                request.state,
                step="filesystem-result",
                default_workspace=True,
            )
        ):
            # Deep Agents 0.7.19 leaves the Command node type unparameterized.
            return await super().awrap_tool_call(projected, continue_original)  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]


__all__ = ["_WorkspaceFilePaths", "_WorkspaceFilesystem"]
