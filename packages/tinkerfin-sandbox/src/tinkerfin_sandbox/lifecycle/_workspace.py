"""Project-bound isolated workspaces for Runtime and application file access."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import TYPE_CHECKING, Generic, TypeVar

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import BackendProtocol

from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_notifications import ResyncRequired

from ..backends.rooted import RootedOpenSandboxBackend
from ..directory_contents import WorkspaceDirectoryContents
from ..middleware.filesystem import (
    ROOTED_EXECUTE_TOOL_DESCRIPTION,
    ROOTED_FILESYSTEM_SYSTEM_PROMPT,
)
from ..workspace_files import WorkspaceDirectoryPage, WorkspaceFileInfo, WorkspaceText
from ._workspace_access import _ProjectCoordinator
from ._workspace_directories import synchronize
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
        self._directories: tuple[WorkspaceDirectoryContents, ...] = ()

    def with_directories(
        self, directories: Sequence[WorkspaceDirectoryContents]
    ) -> SandboxWorkspace[KeyT]:
        """Prepare managed inputs before admitting each Run.

        Files remain project-local and writable. Unchanged declarations preserve
        local edits. A changed declaration raises Busy while another Run uses
        the project, and raises FileChanged instead of overwriting local edits.
        The framework owns publication, admission and failed-preparation cleanup.

        Args:
            directories: Complete managed inputs at nonoverlapping virtual paths.

        Returns:
            A lazy copy of this project declaration, borrowing the same manager.

        Raises:
            TypeError: An entry is not a WorkspaceDirectoryContents declaration.
            ValueError: Directory paths overlap.
        """
        values = tuple(directories)
        if any(not isinstance(item, WorkspaceDirectoryContents) for item in values):
            raise TypeError("directories must contain WorkspaceDirectoryContents")
        paths = [item.path.rstrip("/") for item in values]
        if any(
            a == b or a.startswith(b + "/") or b.startswith(a + "/")
            for i, a in enumerate(paths)
            for b in paths[i + 1 :]
        ):
            raise ValueError("managed directory declarations must not overlap")
        result = copy(self)
        result._directories = values
        return result

    async def synchronize_directories(
        self, directories: Sequence[WorkspaceDirectoryContents]
    ) -> bool:
        """Publish explicit declarations in an existing project without a Run.

        Returns whether any directory changed. No sandbox is created. Busy and
        local-change conflicts are observable; this method never waits or retries
        on behalf of the host and does not publish partly uploaded directories.
        Each directory is published independently; an error in a later declaration
        does not undo earlier publications.

        Args:
            directories: Complete managed inputs at nonoverlapping virtual paths.

        Returns:
            Whether at least one directory declaration changed.

        Raises:
            TypeError: A declaration has an unsupported type.
            ValueError: Directory paths overlap.
            OpenSandboxError: The existing workspace is unavailable, busy, has
                conflicting local edits, or publication or cleanup fails.
        """
        prepared = self.with_directories(directories)
        changed = False
        for item in prepared._directories:
            changed = await synchronize(self._project, item, create=False) or changed
        return changed

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[RootedOpenSandboxBackend, None]:
        """Borrow isolated file and command access outside a Runtime Run.

        The context stops its processes and network activity before returning and
        keeps project files for later use. It does not resume a paused parent.
        Concurrent contexts for the same project share files without transactional
        write guarantees. Other projects cannot access this project's data.
        After the owner binding is removed and its former parent is confirmed
        deleted, a later open creates a new parent without reviving old contexts.

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

    async def list_directory(
        self, path: str = "/", *, limit: int = 200, cursor: str | None = None
    ) -> WorkspaceDirectoryPage:
        """List an existing directory without creating or resuming any resource.

        Args:
            path: Absolute virtual project path; parent traversal is rejected.
            limit: Maximum entries per page, from one through 200.
            cursor: Opaque continuation from the same directory observation.

        Returns:
            Directories first, then other entries, ordered by exact name.

        Raises:
            ValueError: Path, limit, or cursor is invalid.
            FileNotFoundError: The directory does not exist.
            NotADirectoryError: The selected entry is not a directory.
            OpenSandboxError: Workspace access, cursor continuity or cleanup fails.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 through 200")
        if cursor is not None and len(cursor) > 8192:
            raise ValueError("cursor exceeds its size limit")
        result = await self._project.query_files(
            "list", path, {"limit": limit, "cursor": cursor}
        )
        assert result.kind == "directory"
        return WorkspaceDirectoryPage(
            result.path,
            tuple(item.public() for item in result.entries),
            result.next_cursor,
        )

    async def get_file_info(self, path: str) -> WorkspaceFileInfo:
        """Read existing entry metadata without following symbolic links.

        Args:
            path: Absolute virtual project path; parent traversal is rejected.

        Returns:
            Entry metadata with an opaque change token and UTC modification time.

        Raises:
            ValueError: The path is invalid.
            FileNotFoundError: The selected entry does not exist.
            OpenSandboxError: Workspace access or resource cleanup fails.
        """
        result = await self._project.query_files("stat", path, {})
        assert result.kind == "info"
        return result.file.public()

    async def read_text(
        self, path: str, *, max_bytes: int, max_lines: int = 200
    ) -> WorkspaceText:
        """Read a bounded UTF-8 prefix without opening an execution environment.

        Args:
            path: Absolute virtual path of an existing regular file.
            max_bytes: Maximum source bytes, from one through one MiB.
            max_lines: Maximum source lines, from one through 10,000.

        Returns:
            Text and metadata from one file descriptor, with explicit truncation.

        Raises:
            ValueError: The path or limits are invalid.
            FileNotFoundError: The selected file does not exist.
            OpenSandboxError: Entry is not UTF-8 text, changes during the read,
                workspace access fails, or owned resource cleanup fails.
        """
        if type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024:
            raise ValueError("max_bytes must be an integer from 1 through one MiB")
        if type(max_lines) is not int or not 1 <= max_lines <= 10000:
            raise ValueError("max_lines must be an integer from 1 through 10,000")
        result = await self._project.query_files(
            "text", path, {"max_bytes": max_bytes, "max_lines": max_lines}
        )
        assert result.kind == "text"
        return WorkspaceText(result.file.public(), result.text, result.truncated)

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
        for item in self._directories:
            await synchronize(self._project, item, create=True)
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
