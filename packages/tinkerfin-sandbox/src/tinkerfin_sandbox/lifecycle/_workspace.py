"""Project-bound isolated workspaces for Runtime and application file access."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Generic, TypeVar

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import BackendProtocol

from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_notifications import ResyncRequired

from ..backends.rooted import RootedOpenSandboxBackend
from ..middleware.filesystem import (
    ROOTED_EXECUTE_TOOL_DESCRIPTION,
    ROOTED_FILESYSTEM_SYSTEM_PROMPT,
)
from ._workspace_access import _ProjectCoordinator
from ._workspace_watch import WorkspaceChange

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

KeyT = TypeVar("KeyT")


class SandboxWorkspace(Generic[KeyT]):
    """Share one project's files while isolating each Run's processes and network.

    Create this declaration with ``sandboxes.workspace(key, workspace_key=...)``.
    The owner selects a physical sandbox; the project selects its files, HOME,
    caches and dependencies. Runtime namespaces do not change either selection.
    Files live as long as the physical sandbox. Processes belong to one borrowed
    Run and are stopped when its context ends, including cancellation and errors.
    The manager remains borrowed and must outlive all workspace use.
    """

    def __init__(
        self,
        manager: OpenSandboxManager[KeyT],
        key: KeyT,
        *,
        workspace_key: str,
        routes: Mapping[str, BackendProtocol] | None = None,
    ) -> None:
        """Bind the physical owner and project without performing remote I/O.

        Args:
            manager: Borrowed sandbox lifecycle manager.
            key: Application identity selecting the physical sandbox owner.
            workspace_key: Nonempty opaque project identity, not a filesystem path.
            routes: Other borrowed backends used only for Runtime file-tool routing.

        Raises:
            TypeError: An identity has an unsupported type.
            ValueError: An identity is invalid.
        """
        self._project = _ProjectCoordinator(manager, key, workspace_key)
        self._routes = dict(routes or {})

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[RootedOpenSandboxBackend, None]:
        """Borrow isolated file and command access outside a Runtime Run.

        The context stops its processes and network activity before returning and
        keeps project files for later use. It does not resume a paused parent.
        Concurrent contexts for the same project share files without transactional
        write guarantees. Other projects cannot access this project's data.

        Yields:
            Project-rooted asynchronous file and command operations.

        Raises:
            OpenSandboxError: Admission, execution setup or cleanup fails. An
                uncertain cleanup retains ownership for a later ``delete``.
        """
        async with self._project.open() as workspace:
            yield workspace

    async def delete(self) -> None:
        """Stop the project's Runs and delete its files, HOME and dependencies.

        This affects open contexts in every worker, but never destroys the parent
        sandbox or another project. Successful deletion invalidates old contexts;
        the next ``open`` starts an empty project. An absent project is a no-op.
        Cancellation waits for owned cleanup. If termination cannot be confirmed,
        data is preserved and new access remains blocked until deletion succeeds.

        Raises:
            OpenSandboxError: The parent is unavailable, paused, or cleanup cannot
                be confirmed. Retry deletion after resolving the reported failure.
        """
        await self._project.delete()

    @asynccontextmanager
    async def watch(
        self,
    ) -> AsyncGenerator[AsyncIterator[WorkspaceChange | ResyncRequired], None]:
        """Subscribe to advisory file-root changes in an existing running project.

        Enter before reading the initial directory or file state. Changes can be
        coalesced; a ``ResyncRequired`` requires another authoritative read. The
        watch creates no Sandbox, resumes none, and runs no initialization. Pausing,
        deleting, replacing, or losing the selected instance emits a disconnected
        resync and ends the stream. Enter a new watch after making it available.
        Exiting or cancelling the context releases this subscription without
        waiting for other consumers. Each iterator supports one concurrent reader.

        Yields:
            Root-wide hints without file content, paths, or internal identities.

        Raises:
            OpenSandboxError: The manager, existing project, change collector, or
                notification service cannot admit the subscription, or owned
                watch resources cannot be closed. A source disconnection itself
                is reported through the iterator's resync and end.
        """
        async with self._project.watch() as changes:
            yield changes

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncGenerator[
        PreparedWorkspace[RootedOpenSandboxBackend, BackendProtocol], None
    ]:
        """Provide one isolated Run to a Runtime and release it after Graph activity.

        Args:
            identity: Runtime execution identity. Its namespace scopes logical
                persistence and does not select a different physical sandbox.

        Yields:
            The project workspace, routed backend and file-tool instructions.

        Raises:
            TypeError: ``identity`` is not a ``RunIdentity``.
            OpenSandboxError: Workspace admission or cleanup fails.
        """

        if not isinstance(identity, RunIdentity):
            raise TypeError("workspace preparation requires a RunIdentity")
        async with self.open() as workspace:
            backend = (
                CompositeBackend(default=workspace, routes=dict(self._routes))
                if self._routes
                else workspace
            )
            yield PreparedWorkspace(
                workspace=workspace,
                backend=backend,
                filesystem_instructions=ROOTED_FILESYSTEM_SYSTEM_PROMPT,
                tool_descriptions={"execute": ROOTED_EXECUTE_TOOL_DESCRIPTION},
            )
