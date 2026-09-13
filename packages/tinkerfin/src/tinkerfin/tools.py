"""Tool access to business context, run identity, and a borrowed workspace."""

from __future__ import annotations

from typing import Generic

from deepagents import DeepAgentState
from langchain.tools import ToolRuntime as LangChainToolRuntime
from typing_extensions import TypeVar

from tinkerfin_contracts import RunIdentity

from .errors import TinkerFinLifecycleError

ContextT = TypeVar("ContextT", default=None)
WorkspaceT = TypeVar("WorkspaceT", default=object)
StateT = TypeVar("StateT", default=DeepAgentState)


class _ToolRunScope(Generic[WorkspaceT]):
    """Revoke workspace access only after all graph and tool work has stopped."""

    def __init__(self, identity: RunIdentity, workspace: WorkspaceT | None) -> None:
        self.identity = identity
        self._workspace = workspace
        self._active = True

    @property
    def workspace(self) -> WorkspaceT:
        if not self._active:
            raise TinkerFinLifecycleError("The tool's run has closed")
        if self._workspace is None:
            raise TinkerFinLifecycleError("This run has no configured workspace")
        return self._workspace

    def close(self) -> None:
        self._active = False
        self._workspace = None


class ToolRuntime(
    LangChainToolRuntime[ContextT, StateT], Generic[ContextT, WorkspaceT, StateT]
):
    """Receive the executing tool's context, identity, and workspace automatically.

    Annotate a tool parameter with this type; the parameter is hidden from the
    model's tool schema. ``context`` is the original business context. ``state``
    defaults to DeepAgentState; other inherited tool runtime fields are retained.
    WorkspaceT must match the capability supplied by the configured Workspace.

    The workspace is borrowed for the managed run. Do not close it or retain it
    for background work. Compiled and remote subagents configure their own access.
    These properties require managed TinkerFin execution; constructing a runtime
    object directly or calling a standalone graph does not open a workspace.
    """

    # Retain the subclass's type parameters when Pydantic collects inherited
    # dataclass fields for a tool's argument model.
    state: StateT
    context: ContextT
    _scope: _ToolRunScope[WorkspaceT] | None = None

    @property
    def workspace(self) -> WorkspaceT:
        """Return the borrowed workspace while this run is active.

        Raises:
            TinkerFinLifecycleError: No workspace is configured, the run has
                closed, or the tool was invoked outside managed execution.
        """
        if self._scope is None:
            raise TinkerFinLifecycleError("Workspace access requires a managed run")
        return self._scope.workspace

    @property
    def identity(self) -> RunIdentity:
        """Return this managed run's immutable namespace, thread, and run identity.

        Raises:
            TinkerFinLifecycleError: The tool was invoked outside managed execution.
        """
        if self._scope is None:
            raise TinkerFinLifecycleError("Run identity requires a managed run")
        return self._scope.identity


__all__ = ["ToolRuntime"]
