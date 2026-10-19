"""Typed workspace preparation shared by runtimes and resource providers."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Generic, Literal, Protocol, TypeVar, runtime_checkable

from .identity import RunIdentity
from .skills import SkillSource
from .workspace_tools import WorkspaceTool

WorkspaceT_co = TypeVar("WorkspaceT_co", covariant=True)
BackendT_co = TypeVar("BackendT_co", covariant=True)


class WorkspacePathResolver(Protocol):
    """Resolve file-tool paths inside the workspace's own filesystem boundary.

    The provider owns relative paths, home expansion, and symlink resolution.
    Resolving never grants access: the eventual file operation must enforce its
    current permissions. No target file may be created or modified to resolve it.
    """

    async def __call__(
        self, path: str | None, *, operation: Literal["read", "write"]
    ) -> str:
        """Return a canonical absolute POSIX path without traversal components.

        Args:
            path: Requested file or directory; ``None`` selects the workspace's
                default search directory for tools with an optional search root.
            operation: Intended file access, without implying authorization.

        Returns:
            The resolved path, including nonexistent targets for writes.

        Raises:
            ValueError: The path cannot be resolved to a supported target.
            asyncio.CancelledError: Resolution was cancelled; no work is detached.
        """
        ...


@runtime_checkable
class WorkspacePreparationFailure(Protocol):
    """Expose a safe preparation category across workspace provider exceptions.

    Providers retain their own exception family and diagnostic details. Runtime
    reports only this category when preparation fails; exception text stays private.
    """

    @property
    def workspace_failure(
        self,
    ) -> (
        Literal["busy", "file_conflict", "stopped", "paused", "unavailable", "timeout"]
        | None
    ):
        """Identify an actionable condition, or leave it unclassified."""
        ...


@dataclass(frozen=True, slots=True)
class PreparedWorkspace(Generic[WorkspaceT_co, BackendT_co]):
    """Expose workspace access and the final filesystem backend for one Run.

    ``workspace`` is the capability borrowed by tools during managed execution.
    ``backend`` serves the agent's filesystem operations and may combine routes.
    Both are borrowed until the provider's context exits. Instructions describe
    path usage; they do not grant permissions or replace authorization.
    A file-path resolver binds native path interpretation to this workspace for
    built-in file tools. Without it, the backend's virtual path contract applies.
    Tools and skill sources become available only after preparation. A provider
    may supply a stable workspace_scope_id for authorization that survives
    separate approval Run borrows; it must not identify a transient connection.
    """

    workspace: WorkspaceT_co
    backend: BackendT_co
    filesystem_instructions: str | None = None
    tool_descriptions: Mapping[str, str] = field(default_factory=dict[str, str])
    file_path_resolver: WorkspacePathResolver | None = None
    tools: tuple[WorkspaceTool, ...] = ()
    skill_sources: tuple[SkillSource, ...] = ()
    workspace_scope_id: str | None = None

    def __post_init__(self) -> None:
        """Keep instruction overrides independent of the provider's input mapping."""

        if self.workspace_scope_id is not None and (
            not isinstance(self.workspace_scope_id, str)
            or not self.workspace_scope_id
            or self.workspace_scope_id.strip() != self.workspace_scope_id
            or "\0" in self.workspace_scope_id
        ):
            raise ValueError("workspace_scope_id must be a canonical nonempty string")
        object.__setattr__(
            self, "tool_descriptions", MappingProxyType(dict(self.tool_descriptions))
        )


@runtime_checkable
class Workspace(Protocol[WorkspaceT_co, BackendT_co]):
    """Declare a workspace that the Runtime opens after admitting a Run.

    Creating the declaration performs no resource I/O. The returned context owns
    preparation and release, including failed or cancelled preparation. Exiting it
    releases this Run's borrow without implicitly closing shared services.
    """

    def prepare(
        self, identity: RunIdentity
    ) -> AbstractAsyncContextManager[PreparedWorkspace[WorkspaceT_co, BackendT_co]]:
        """Open workspace access for one complete Run identity.

        Args:
            identity: The Runtime's immutable namespace, thread, and Run identity.

        Returns:
            A context that prepares the workspace on entry and releases this Run's
            resources after the Graph has fully stopped.
        """

        ...


__all__ = [
    "PreparedWorkspace",
    "Workspace",
    "WorkspacePathResolver",
    "WorkspacePreparationFailure",
]
