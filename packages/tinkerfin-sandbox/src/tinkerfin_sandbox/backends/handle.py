"""Stable Sandbox handle with safe backend replacement.

Application code can retain one ``OpenSandboxHandle`` while the manager replaces its
remote instance. Started calls finish on their leased backend before it is reclaimed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import (
    asynccontextmanager,
    contextmanager,
)
from datetime import timedelta
from threading import Condition

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteOffloadResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

from ..errors import (
    OpenSandboxBusyError,
    OpenSandboxHandleClosedError,
    OpenSandboxHandleOwnershipError,
    OpenSandboxLifecycleUncertainError,
    OpenSandboxPausedError,
)
from ..models import OpenSandboxRuntimeInfo
from ._operations import RemoteOperations
from ._rooted_protocol import _build_rooted_command, _parse_rooted_response
from .sdk import OpenSandboxBackend


class OpenSandboxHandle(BaseSandbox):
    """Preserve stable identity and in-flight calls across backend replacement.

    The condition protects only the backend pointer, closure state, and per-backend
    lease counts; it does not serialize unrelated operations. A replacement becomes
    visible immediately, while calls that already leased the old backend finish there
    before the manager disposes it.

    The handle owns no independent lifecycle. Callers must use the creating
    ``OpenSandboxManager`` to delete or close it so persisted bindings and remote
    resources remain consistent. It mirrors the synchronous ``BaseSandbox`` method
    shapes for protocol compatibility, but the native OpenSandbox backend supports
    remote I/O only through the corresponding asynchronous methods.
    """

    def __init__(self, backend: OpenSandboxBackend) -> None:
        """Initialize a stable handle with one manager-owned backend.

        Args:
            backend: Raw OpenSandbox connection whose lifecycle the manager owns.
        """
        self._condition = Condition()
        self._backend = backend
        self._active_calls: dict[int, int] = {}
        self._idle_waiters: dict[
            int,
            list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]],
        ] = {}
        self._closed = False
        self._blocked_reason: str | None = None
        self._remote_operations: dict[int, tuple[str, RemoteOperations]] = {}

    @property
    def id(self) -> str:
        """Return the current remote Sandbox ID, which can change on replacement."""
        with self._condition:
            return str(self._backend.id)

    @property
    def is_closed(self) -> bool:
        """Return whether the manager has stopped accepting new handle leases."""
        with self._condition:
            return self._closed

    @property
    def enable_capture_offload(  # pyright: ignore[reportIncompatibleVariableOverride]
        self,
    ) -> bool:
        """Return whether the current backend supports source-side output offload."""
        with self._condition:
            return bool(getattr(self._backend, "enable_capture_offload", False))

    @contextmanager
    def _lease(self) -> Generator[OpenSandboxBackend]:
        """Pin one backend for a call and register its in-flight lease."""
        backend = self._acquire_backend()
        try:
            yield backend
        finally:
            self._release_backend(backend)

    @asynccontextmanager
    async def _alease(self) -> AsyncGenerator[OpenSandboxBackend]:
        """Lease a backend without performing remote I/O during registration."""
        backend = self._acquire_backend()
        try:
            with self._condition:
                entry = self._remote_operations.get(id(backend))
                if entry is None:
                    # Native SDK initialization can leave remote work before this
                    # handle exists. Preserve that same execution ownership.
                    operations = (
                        backend._remote_operations
                        if isinstance(backend, OpenSandboxBackend)
                        else RemoteOperations()
                    )
                    entry = (backend.id, operations)
                    self._remote_operations[id(backend)] = entry
            with entry[1].activate():
                yield backend
        finally:
            self._release_backend(backend)

    def _acquire_backend(self) -> OpenSandboxBackend:
        """Atomically pin the current backend and increment its lease count."""
        with self._condition:
            if self._closed:
                raise OpenSandboxHandleClosedError("OpenSandbox handle is closed")
            if self._blocked_reason == "paused":
                raise OpenSandboxPausedError(
                    "Sandbox is paused; call resume before using it"
                )
            if self._blocked_reason in {"pausing", "resuming", "uncertain"}:
                raise OpenSandboxLifecycleUncertainError(
                    "Sandbox lifecycle transition is not yet confirmed"
                )
            if self._blocked_reason is not None:
                raise OpenSandboxBusyError(
                    "Sandbox is not accepting operations during lifecycle coordination"
                )
            backend = self._backend
            backend_key = id(backend)
            self._active_calls[backend_key] = self._active_calls.get(backend_key, 0) + 1
            return backend

    def _suspend_calls(self, reason: str) -> None:
        """Close local admission before acknowledging a shared pause request."""
        with self._condition:
            self._blocked_reason = reason

    def _allow_calls(self) -> None:
        """Reopen local admission only after authoritative lifecycle coordination."""
        with self._condition:
            self._blocked_reason = None

    def _accepts_calls(self) -> bool:
        """Report local readiness without issuing a data-plane operation."""
        with self._condition:
            return not self._closed and self._blocked_reason is None

    def _is_idle(self) -> bool:
        """Require local completion and confirmed remote settlement before pause."""
        with self._condition:
            if self._active_calls:
                return False
            if (
                isinstance(self._backend, OpenSandboxBackend)
                and not self._backend._remote_operations.is_idle
            ):
                return False
            return all(
                operations.is_idle
                for remote_id, operations in self._remote_operations.values()
                if remote_id == self._backend.id
            )

    def _release_backend(self, backend: OpenSandboxBackend) -> None:
        """Decrement a lease count and wake thread and event-loop waiters."""
        waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []
        with self._condition:
            backend_key = id(backend)
            remaining = self._active_calls[backend_key] - 1
            if remaining:
                self._active_calls[backend_key] = remaining
            else:
                self._active_calls.pop(backend_key, None)
                waiters = self._idle_waiters.pop(backend_key, [])
                self._condition.notify_all()
        for loop, waiter in waiters:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(self._complete_waiter, waiter)
            except RuntimeError:
                if loop.is_closed():
                    continue
                raise

    def _confirm_remote_resource(self, sandbox_id: str, identity: str) -> None:
        """Apply exact cleanup evidence to every retained connection for one instance.

        Reconnection does not erase unresolved work. A namespaced session DELETE
        can settle that same resource across old connection trackers without
        clearing unknown commands or obligations belonging to a replacement.
        """
        with self._condition:
            trackers = {
                operations
                for remote_id, operations in self._remote_operations.values()
                if remote_id == sandbox_id
            }
            if (
                isinstance(self._backend, OpenSandboxBackend)
                and self._backend.id == sandbox_id
            ):
                trackers.add(self._backend._remote_operations)
            for operations in trackers:
                operations.confirm_resource_stopped(identity)

    @staticmethod
    def _complete_waiter(waiter: asyncio.Future[None]) -> None:
        """Complete a live idle waiter after cancellation-safe cross-thread release."""
        if not waiter.done():
            waiter.set_result(None)

    def _replace_backend(
        self,
        backend: OpenSandboxBackend,
    ) -> OpenSandboxBackend:
        """Publish a replacement and return the old backend to manager cleanup."""
        with self._condition:
            if self._closed:
                raise OpenSandboxHandleClosedError("OpenSandbox handle is closed")
            old_backend = self._backend
            self._backend = backend
            return old_backend

    def _wait_until_idle(self, backend: OpenSandboxBackend) -> None:
        """Wait for one old backend to become idle without blocking its replacement."""
        with self._condition:
            backend_key = id(backend)
            while self._active_calls.get(backend_key, 0):
                self._condition.wait()

    async def _await_until_idle(self, backend: OpenSandboxBackend) -> None:
        """Wait asynchronously until every lease on one backend exits."""
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        backend_key = id(backend)
        entry = (loop, waiter)
        with self._condition:
            active = bool(self._active_calls.get(backend_key, 0))
            if active:
                self._idle_waiters.setdefault(backend_key, []).append(entry)
        try:
            if active:
                await waiter
            operations = self._remote_operations.get(backend_key)
            if operations is not None:
                await operations[1].wait()
                if backend is not self._backend and operations[1].is_idle:
                    self._remote_operations.pop(backend_key, None)
            elif isinstance(backend, OpenSandboxBackend):
                await backend._remote_operations.wait()
        except BaseException:
            with self._condition:
                waiters = self._idle_waiters.get(backend_key)
                if waiters is not None and entry in waiters:
                    waiters.remove(entry)
                    if not waiters:
                        self._idle_waiters.pop(backend_key, None)
            raise

    def _retire(self) -> OpenSandboxBackend:
        """Stop new leases and wait for current backend calls to finish."""
        with self._condition:
            if self._closed:
                return self._backend
            self._closed = True
            backend = self._backend
            backend_key = id(backend)
            while self._active_calls.get(backend_key, 0):
                self._condition.wait()
            return backend

    async def _aretire(self) -> OpenSandboxBackend:
        """Stop new leases and asynchronously await current backend calls."""
        with self._condition:
            self._closed = True
            backend = self._backend
        await self._await_until_idle(backend)
        return backend

    def close(self) -> None:
        """Reject closure that bypasses the owning manager.

        Raises:
            OpenSandboxHandleOwnershipError: Always raised because only the manager
                may close or delete this handle.
        """
        raise OpenSandboxHandleOwnershipError(
            "OpenSandboxHandle is owned by OpenSandboxManager; use manager.delete() "
            "or manager.aclose()"
        )

    def _close_from_manager(self) -> None:
        """Wait for calls and close the current local backend for the manager."""
        backend = self._retire()
        backend.close()

    async def _aclose_from_manager(self) -> None:
        """Await calls and close the current local backend for the manager."""
        backend = await self._aretire()
        await backend.aclose()

    def _reset_workspace_from_manager(self, root: str) -> None:
        """Clear a fixed workspace while retaining its directory and remote instance.

        The shared Rooted helper deletes direct children through a pinned directory
        descriptor and never follows workspace links. ``-I -S`` and the backend's
        internal command scope isolate helper imports from the writable workspace.

        Args:
            root: Sandbox workspace already validated by the configuration model.

        Raises:
            RuntimeError: The helper rejects the root or cannot complete cleanup.
        """
        request = _build_rooted_command(
            root=root,
            operation="reset",
            arguments={},
        )
        with self._lease() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        parsed = _parse_rooted_response(response, request=request)
        if parsed.status == "error" or parsed.operation != "reset":
            raise RuntimeError("OpenSandbox workspace reset command failed")

    async def _areset_workspace_from_manager(self, root: str) -> None:
        """Clear the fixed workspace through native async I/O and retain the instance."""
        request = _build_rooted_command(
            root=root,
            operation="reset",
            arguments={},
        )
        async with self._alease() as backend:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
        parsed = _parse_rooted_response(response, request=request)
        if parsed.status == "error" or parsed.operation != "reset":
            raise RuntimeError("OpenSandbox workspace reset command failed")

    def renew(self, timeout: timedelta) -> None:
        """Extend the current remote Sandbox lifetime under a stable lease.

        Args:
            timeout: Lifetime extension measured from the current time.
        """
        with self._lease() as backend:
            backend.renew(timeout)

    async def arenew(self, timeout: timedelta) -> None:
        """Asynchronously extend the current Sandbox lifetime under a lease."""
        async with self._alease() as backend:
            await backend.arenew(timeout)

    def get_runtime_info(self) -> OpenSandboxRuntimeInfo:
        """Return stable runtime details and data-plane health for the Sandbox."""
        with self._lease() as backend:
            return backend.get_runtime_info()

    async def aget_runtime_info(self) -> OpenSandboxRuntimeInfo:
        """Asynchronously return runtime details and data-plane health."""
        async with self._alease() as backend:
            return await backend.aget_runtime_info()

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Synchronously execute a command on the backend fixed by this lease.

        Args:
            command: Shell command text.
            timeout: Command timeout in seconds; ``None`` uses the backend default.

        Returns:
            The standard Deep Agents command response.
        """
        with self._lease() as backend:
            return backend.execute(command, timeout=timeout)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Asynchronously execute a command on the backend fixed by this lease.

        Args:
            command: Shell command text.
            timeout: Command timeout in seconds; ``None`` uses the backend default.

        Returns:
            The standard Deep Agents command response.
        """
        with self._lease() as backend:
            return await backend.aexecute(command, timeout=timeout)

    def execute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        """Synchronously execute and inline or offload output at configured limits.

        Args:
            command: Shell command text.
            capture_path: Sandbox path used for captured output.
            max_inline_bytes: Maximum number of bytes returned inline.
            max_capture_bytes: Optional maximum number of captured bytes retained.
            timeout: Command timeout in seconds.

        Returns:
            Inline output or capture-file metadata.
        """
        with self._lease() as backend:
            return backend.execute_with_offload(
                command,
                capture_path,
                max_inline_bytes=max_inline_bytes,
                max_capture_bytes=max_capture_bytes,
                timeout=timeout,
            )

    async def aexecute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        """Asynchronously execute and inline or offload output at configured limits.

        Args:
            command: Shell command text.
            capture_path: Sandbox path used for captured output.
            max_inline_bytes: Maximum number of bytes returned inline.
            max_capture_bytes: Optional maximum number of captured bytes retained.
            timeout: Command timeout in seconds.

        Returns:
            Inline output or capture-file metadata.
        """
        with self._lease() as backend:
            return await backend.aexecute_with_offload(
                command,
                capture_path,
                max_inline_bytes=max_inline_bytes,
                max_capture_bytes=max_capture_bytes,
                timeout=timeout,
            )

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Synchronously read a line window from a text file.

        Args:
            file_path: Absolute path inside the Sandbox.
            offset: Zero-based starting line offset.
            limit: Maximum number of lines to return.

        Returns:
            The standard Deep Agents read result.
        """
        with self._lease() as backend:
            return backend.read(file_path, offset, limit)

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Asynchronously read a line window from a text file.

        Args:
            file_path: Absolute path inside the Sandbox.
            offset: Zero-based starting line offset.
            limit: Maximum number of lines to return.

        Returns:
            The standard Deep Agents read result.
        """
        with self._lease() as backend:
            return await backend.aread(file_path, offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        """Synchronously replace a file with complete text content.

        Args:
            file_path: Absolute path inside the Sandbox.
            content: Complete replacement text.

        Returns:
            The standard Deep Agents write result.
        """
        with self._lease() as backend:
            return backend.write(file_path, content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Asynchronously replace a file with complete text content.

        Args:
            file_path: Absolute path inside the Sandbox.
            content: Complete replacement text.

        Returns:
            The standard Deep Agents write result.
        """
        with self._lease() as backend:
            return await backend.awrite(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Synchronously replace exact text within a file.

        Args:
            file_path: Absolute path inside the Sandbox.
            old_string: Exact source text to match.
            new_string: Replacement text.
            replace_all: Replace every match; otherwise require a unique match.

        Returns:
            The standard Deep Agents edit result.
        """
        with self._lease() as backend:
            return backend.edit(file_path, old_string, new_string, replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Asynchronously replace exact text within a file.

        Args:
            file_path: Absolute path inside the Sandbox.
            old_string: Exact source text to match.
            new_string: Replacement text.
            replace_all: Replace every match; otherwise require a unique match.

        Returns:
            The standard Deep Agents edit result.
        """
        with self._lease() as backend:
            return await backend.aedit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        """Synchronously delete one Sandbox file or directory.

        Args:
            file_path: Absolute path inside the Sandbox.

        Returns:
            The standard Deep Agents delete result.
        """
        with self._lease() as backend:
            return backend.delete(file_path)

    async def adelete(self, file_path: str) -> DeleteResult:
        """Asynchronously delete one Sandbox file or directory.

        Args:
            file_path: Absolute path inside the Sandbox.

        Returns:
            The standard Deep Agents delete result.
        """
        with self._lease() as backend:
            return await backend.adelete(file_path)

    def ls(self, path: str) -> LsResult:
        """Synchronously list one Sandbox directory.

        Args:
            path: Absolute directory path inside the Sandbox.

        Returns:
            The standard Deep Agents directory result.
        """
        with self._lease() as backend:
            return backend.ls(path)

    async def als(self, path: str) -> LsResult:
        """Asynchronously list one Sandbox directory.

        Args:
            path: Absolute directory path inside the Sandbox.

        Returns:
            The standard Deep Agents directory result.
        """
        with self._lease() as backend:
            return await backend.als(path)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Synchronously match paths inside the Sandbox.

        Args:
            pattern: Glob expression.
            path: Optional search root.

        Returns:
            The standard Deep Agents glob result.
        """
        with self._lease() as backend:
            return backend.glob(pattern, path)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Asynchronously match paths inside the Sandbox.

        Args:
            pattern: Glob expression.
            path: Optional search root.

        Returns:
            The standard Deep Agents glob result.
        """
        with self._lease() as backend:
            return await backend.aglob(pattern, path)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Synchronously search text inside the Sandbox.

        Args:
            pattern: Regular expression to search for.
            path: Optional search root.
            glob: Optional file-name filter.
            max_count: Maximum number of matches to return.

        Returns:
            The standard Deep Agents grep result.
        """
        with self._lease() as backend:
            return backend.grep(
                pattern,
                path,
                glob,
                max_count=max_count,
            )

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Asynchronously search text inside the Sandbox.

        Args:
            pattern: Regular expression to search for.
            path: Optional search root.
            glob: Optional file-name filter.
            max_count: Maximum number of matches to return.

        Returns:
            The standard Deep Agents grep result.
        """
        with self._lease() as backend:
            return await backend.agrep(
                pattern,
                path,
                glob,
                max_count=max_count,
            )

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Synchronously upload binary files while preserving per-file results.

        Args:
            files: ``(absolute Sandbox path, binary content)`` pairs.

        Returns:
            Per-file upload results in input order.
        """
        with self._lease() as backend:
            return backend.upload_files(files)

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Asynchronously upload files while preserving per-file results.

        Args:
            files: ``(absolute Sandbox path, binary content)`` pairs.

        Returns:
            Per-file upload results in input order.
        """
        with self._lease() as backend:
            return await backend.aupload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Synchronously download files while preserving per-file results.

        Args:
            paths: Absolute Sandbox file paths.

        Returns:
            Per-file download results in input order.
        """
        with self._lease() as backend:
            return backend.download_files(paths)

    async def aread_bytes(
        self, path: str, *, max_bytes: int, timeout: float = 30
    ) -> bytes:
        """Read a complete binary file under a stable backend lease.

        Args:
            path: Absolute Sandbox path; use a rooted view for workspace confinement.
            max_bytes: Maximum accepted file size in bytes.
            timeout: Read deadline in seconds, greater than zero and at most 290.

        Returns:
            Complete bytes, never a partial result.

        Raises:
            ValueError: The path or limits are invalid.
            OpenSandboxFileTooLargeError: The file exceeds the byte limit.
            OpenSandboxBackendError: The read failed or timed out.
            OSError: The file was not found or access was denied.

        Cancellation closes the response before releasing this lease.
        """
        async with self._alease() as backend:
            return await backend.aread_bytes(path, max_bytes=max_bytes, timeout=timeout)

    async def adownload_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Asynchronously download files while preserving per-file results.

        Args:
            paths: Absolute Sandbox file paths.

        Returns:
            Per-file download results in input order.
        """
        with self._lease() as backend:
            return await backend.adownload_files(paths)
