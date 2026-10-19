"""Portable tool declarations and borrowed execution capabilities."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    GetJsonSchemaHandler,
    JsonValue,
    field_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from ._tool_schema import FILE_PATH_METADATA, validate_tool_schema
from .identity import RunIdentity

WorkspaceT = TypeVar("WorkspaceT")
OperationPurpose = Literal[
    "tool", "paths", "skills", "memory", "media", "compaction", "prepare"
]


@dataclass(frozen=True, slots=True)
class FilePath:
    """Declare a tool argument resolved by its authorized workspace before review.

    Place this annotation on strings, including list items, dictionary values,
    and nested model fields. Resolution does not grant access; execution rechecks
    the same target. Schema constraints must accept both the proposed value and
    its canonical absolute path without changing the selected access declaration.

    Args:
        operation: Required filesystem access for this argument.
        default_root: Resolve an omitted or null value to the workspace root.
    """

    operation: Literal["read", "write"]
    default_root: bool = False

    def __post_init__(self) -> None:
        """Reject declarations that cannot describe one filesystem operation."""
        if self.operation not in {"read", "write"}:
            raise ValueError("file path operation must be read or write")

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """Retain the declaration in the tool's exported input schema."""
        result = handler(schema)
        result[FILE_PATH_METADATA] = {
            "operation": self.operation,
            "default_root": self.default_root,
        }
        return result


class ToolDescriptor(BaseModel):
    """Describe model-visible arguments using a self-contained JSON Schema.

    Schemas may reference their own definitions with local JSON-pointer ``$ref``
    values in one schema document. References are stored in canonical URI-fragment
    form. External references, nested resource IDs,
    ``$dynamicRef``, and ``$recursiveRef`` are rejected before validation can
    retrieve other schemas.

    File paths support object properties, matching ``patternProperties``,
    unmatched ``additionalProperties``, array items, and ``allOf`` / ``anyOf`` /
    ``oneOf`` branches. Declarations in other conditional or applicator positions
    are rejected, including through references. Ordinary schemas without file
    path annotations retain those JSON Schema features.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    description: str
    input_schema: dict[str, JsonValue]

    @field_validator("name")
    @classmethod
    def canonical_name(cls, value: str) -> str:
        """Keep one unambiguous registered tool name."""
        if not value or value != value.strip() or "\0" in value:
            raise ValueError("tool name must be nonempty canonical text")
        return value

    @field_validator("input_schema")
    @classmethod
    def portable_schema(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Validate finite, self-contained declarations before transport or use."""
        json.dumps(value, allow_nan=False)
        return validate_tool_schema(value)


class ToolResult(BaseModel):
    """Return finite JSON and a tool error flag; the caller owns correlation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: JsonValue
    is_error: bool = False

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: JsonValue) -> JsonValue:
        """Reject non-finite numbers instead of changing the tool result."""
        json.dumps(value, allow_nan=False)
        return value


@dataclass(frozen=True, slots=True)
class WorkspaceTool:
    """Borrow one declared workspace capability during managed execution.

    Args:
        descriptor: Model-visible schema, including declared filesystem arguments.
        invoke: Execute validated effective arguments under the current operation.
            The workspace owns this callback and its resources until preparation
            exits. Return a ToolResult, never a Graph command or state update.
    """

    descriptor: ToolDescriptor
    invoke: Callable[[Mapping[str, JsonValue]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolExecutionScope:
    """Identify trusted work independently from its current delivery session.

    The framework derives origin and invocation from durable execution evidence.
    Providers must not derive them from model arguments or socket arrival order.
    Current identity may change across approval; origin, invocation and semantic
    steps continue to identify the same operation. Workspace scope binds the
    provider's authorization and survives separate Run resource borrows.
    """

    identity: RunIdentity
    origin_identity: RunIdentity
    workspace_scope_id: str
    invocation_id: str
    graph_namespace: tuple[str, ...]
    purpose: OperationPurpose
    tool_call_id: str | None = None
    steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject scopes whose stable origin changes the canonical thread."""
        if self.identity.thread != self.origin_identity.thread:
            raise ValueError("operation origin belongs to another thread")
        if any(
            not value or value != value.strip() or "\0" in value
            for value in (self.workspace_scope_id, self.invocation_id, *self.steps)
        ):
            raise ValueError("operation identities must be nonempty canonical text")


class _OperationBorrow:
    def __init__(
        self,
        scope: ToolExecutionScope,
        emit_output: Callable[[JsonValue], Awaitable[None]] | None,
    ) -> None:
        self.scope = scope
        self.emit_output = emit_output
        self.parent = _operation.get()
        self.active = True
        self.ordinal = 0

    def is_active(self) -> bool:
        return self.active and (self.parent is None or self.parent.is_active())


_operation: ContextVar[_OperationBorrow | None] = ContextVar(
    "tinkerfin_workspace_operation", default=None
)


def current_workspace_operation() -> ToolExecutionScope:
    """Read the active framework-authenticated operation at a provider boundary.

    Raises:
        RuntimeError: No operation is bound or its owner has already returned.
    """
    borrowed = _operation.get()
    if borrowed is None or not borrowed.is_active():
        raise RuntimeError("workspace access requires an active managed operation")
    return borrowed.scope


async def emit_workspace_output(value: JsonValue) -> None:
    """Deliver progress through the current tool owner's output sink.

    Providers use this after receiving standalone tool output. The framework
    supplies correlation; the value cannot select a different tool or Run.

    Raises:
        RuntimeError: The operation has ended or has no tool output sink.
        ValueError: The value contains a non-finite JSON number.
    """
    current_workspace_operation()
    borrowed = _operation.get()
    assert borrowed is not None
    if borrowed.emit_output is None:
        raise RuntimeError("this workspace operation has no tool output sink")
    json.dumps(value, allow_nan=False)
    await borrowed.emit_output(value)


def next_workspace_operation() -> ToolExecutionScope:
    """Identify the next provider call in this execution's ordered steps.

    Re-entering the same verified scope restarts its sequence so durable providers
    can replay committed calls and reject changed inputs. Parallel branches must
    use distinct workspace_operation keys before issuing their provider calls.
    """
    scope = current_workspace_operation()
    borrowed = _operation.get()
    assert borrowed is not None
    borrowed.ordinal += 1
    return replace(scope, steps=(*scope.steps, f"operation:{borrowed.ordinal}"))


@contextmanager
def bind_workspace_operation(
    scope: ToolExecutionScope,
    *,
    emit_output: Callable[[JsonValue], Awaitable[None]] | None = None,
) -> Generator[ToolExecutionScope]:
    """Bind a verified operation while its framework owner awaits all work.

    This boundary is for runtime and provider implementations. Application tool
    authors use workspace_operation for named child steps, without constructing
    checkpoint identities. Leaving the boundary revokes inherited task access.
    """
    borrowed = _OperationBorrow(scope, emit_output)
    token = _operation.set(borrowed)
    try:
        yield scope
    finally:
        borrowed.active = False
        _operation.reset(token)


@contextmanager
def workspace_operation(key: str) -> Generator[ToolExecutionScope]:
    """Name an independently replayable step within the current operation.

    Use distinct semantic keys for repeated or parallel work. The same key must
    denote the same step after restart; elapsed time and scheduling are not keys.
    """
    if not key or key != key.strip() or "\0" in key:
        raise ValueError("operation step must be nonempty canonical text")
    scope = current_workspace_operation()
    borrowed = _operation.get()
    assert borrowed is not None
    with bind_workspace_operation(
        replace(scope, steps=(*scope.steps, f"step:{key}")),
        emit_output=borrowed.emit_output,
    ) as child:
        yield child


class ToolContext(Generic[WorkspaceT]):
    """Borrow actual workspace access and progress output for a standalone tool.

    Executors provide this capability; it contains no Agent state, model, Graph
    configuration, or Store. Do not retain it or spawn work past the tool's return.
    """

    def __init__(
        self,
        workspace: WorkspaceT,
        scope: ToolExecutionScope,
        emit_output: Callable[[JsonValue], Awaitable[None]],
    ) -> None:
        """Bind resources already prepared and owned by the executor."""
        self._workspace = workspace
        self.scope = scope
        self._emit_output = emit_output
        self._active = True
        self._borrow: _OperationBorrow | None = None

    def _require_active(self) -> None:
        if (
            not self._active
            or self._borrow is not None
            and not self._borrow.is_active()
        ):
            raise RuntimeError("the tool's workspace borrow has ended")

    @property
    def workspace(self) -> WorkspaceT:
        """Return the borrowed workspace while the executor owns this tool."""
        self._require_active()
        return self._workspace

    @property
    def identity(self) -> RunIdentity:
        """Return the current observing Run, distinct from the stable origin."""
        return self.scope.identity

    @property
    def workspace_scope_id(self) -> str:
        """Return the authorization scope shared across approval Run borrows."""
        return self.scope.workspace_scope_id

    @property
    def invocation_id(self) -> str:
        """Return the original logical invocation, stable across retries."""
        return self.scope.invocation_id

    async def emit_output(self, value: JsonValue) -> None:
        """Await progress delivery without producing a second result."""
        self._require_active()
        json.dumps(value, allow_nan=False)
        await self._emit_output(value)


@contextmanager
def borrow_tool_context(
    workspace: WorkspaceT,
    scope: ToolExecutionScope,
    emit_output: Callable[[JsonValue], Awaitable[None]] | None = None,
) -> Generator[ToolContext[WorkspaceT]]:
    """Give an executor-owned tool real resources and revoke access on exit.

    Args:
        workspace: Prepared capability whose lifecycle remains executor-owned.
        scope: Verified invocation identity supplied by the framework.
        emit_output: Awaitable output callback, or ``None`` to inherit the active
            operation's sink. Inheritance retains the original owner's lifetime.

    Yields:
        A context that exposes the workspace and progress only during this borrow.

    Raises:
        RuntimeError: Inheritance is requested outside a managed operation.
    """
    if emit_output is None:
        current_workspace_operation()
        parent = _operation.get()
        assert parent is not None

        async def inherited_output(value: JsonValue) -> None:
            if not parent.is_active():
                raise RuntimeError("the parent workspace operation has ended")
            if parent.emit_output is None:
                raise RuntimeError("this workspace operation has no tool output sink")
            await parent.emit_output(value)

        emit_output = inherited_output
    context = ToolContext(workspace, scope, emit_output)
    with bind_workspace_operation(scope, emit_output=emit_output):
        context._borrow = _operation.get()
        try:
            yield context
        finally:
            context._active = False


__all__ = [
    "FILE_PATH_METADATA",
    "FilePath",
    "OperationPurpose",
    "ToolContext",
    "ToolDescriptor",
    "ToolExecutionScope",
    "ToolResult",
    "WorkspaceTool",
    "bind_workspace_operation",
    "borrow_tool_context",
    "current_workspace_operation",
    "emit_workspace_output",
    "next_workspace_operation",
    "workspace_operation",
]
