"""Typed workspace preparation shared by runtimes and resource providers."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Generic, Protocol, TypeVar, runtime_checkable

from .identity import RunIdentity

WorkspaceT_co = TypeVar("WorkspaceT_co", covariant=True)
BackendT_co = TypeVar("BackendT_co", covariant=True)


@dataclass(frozen=True, slots=True)
class PreparedWorkspace(Generic[WorkspaceT_co, BackendT_co]):
    """Expose workspace access and the final filesystem backend for one Run.

    ``workspace`` is the capability borrowed by tools during managed execution.
    ``backend`` serves the agent's filesystem operations and may combine routes.
    Both are borrowed until the provider's context exits. Instructions describe
    path usage; they do not grant permissions or replace authorization.
    """

    workspace: WorkspaceT_co
    backend: BackendT_co
    filesystem_instructions: str | None = None
    tool_descriptions: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        """Keep instruction overrides independent of the provider's input mapping."""

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


__all__ = ["PreparedWorkspace", "Workspace"]
