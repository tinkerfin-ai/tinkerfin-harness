"""Deep Agents virtual-root view over a managed OpenSandbox handle."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, NoReturn, TypeVar

from deepagents.backends.protocol import (
    ASYNC_GREP_TIMEOUT,
    INVALID_PATH,
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
from deepagents.backends.utils import normalize_read_bounds

from ..models import OpenSandboxRuntimeInfo, _normalize_workspace_root
from . import _rooted_projection, _rooted_transfer
from ._rooted_projection import _ROOTED_EDIT_INLINE_MAX_BYTES, _MappedPath
from ._rooted_protocol import (
    _build_rooted_command,
    _RootedCommand,
)
from ._rooted_transfer import _AsyncStartState
from .handle import OpenSandboxHandle
from .sdk import OpenSandboxBackend

_ResultT = TypeVar("_ResultT")


class RootedOpenSandboxBackend(BaseSandbox):
    """Project model-visible virtual paths into a fixed Sandbox workspace.

    The view owns no remote lifecycle. Native OpenSandbox remote I/O is asynchronous;
    synchronous protocol methods propagate the backend's explicit async-only error.
    Cancelled uploads await native transfer cleanup before returning control;
    writes that completed before cancellation are not rolled back.
    """

    def __init__(
        self,
        handle: OpenSandboxHandle,
        *,
        root: str = "/workspace",
    ) -> None:
        """Create a virtual-root view without taking remote lifecycle ownership.

        Args:
            handle: Stable Sandbox handle owned by ``OpenSandboxManager``.
            root: Absolute POSIX directory represented by the model-visible root.

        Raises:
            ValueError: ``root`` is not a safe non-root absolute POSIX path.
        """
        normalized_root = _normalize_workspace_root(root)
        if normalized_root is None:
            raise ValueError("root must be a safe non-root absolute POSIX path")
        self._handle = handle
        self._root = normalized_root
        self._root_error_pattern = re.compile(
            rf"(?<![\w./-]){re.escape(self._root)}"
            r"(?:/|(?=$|[\s'\".,:;)\]}]))"
        )
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def id(self) -> str:
        """Return the remote Sandbox ID currently held by the stable handle."""
        return self._handle.id

    @property
    def enable_capture_offload(  # pyright: ignore[reportIncompatibleVariableOverride]
        self,
    ) -> bool:
        """Return whether the current backend can offload captured output."""
        return self._handle.enable_capture_offload

    @property
    def is_closed(self) -> bool:
        """Return whether the lifecycle manager has closed the stable handle."""
        return self._handle.is_closed

    def close(self) -> None:
        """Reject closure that bypasses the lifecycle manager."""
        self._handle.close()

    def renew(self, timeout: timedelta) -> None:
        """Extend the current remote Sandbox lifetime through the stable handle."""

        self._handle.renew(timeout)

    async def arenew(self, timeout: timedelta) -> None:
        """Asynchronously extend the current remote Sandbox lifetime."""

        await self._handle.arenew(timeout)

    def get_runtime_info(self) -> OpenSandboxRuntimeInfo:
        """Return runtime details for the Sandbox currently held by the handle."""

        return self._handle.get_runtime_info()

    async def aget_runtime_info(self) -> OpenSandboxRuntimeInfo:
        """Asynchronously return runtime details for the current Sandbox."""

        return await self._handle.aget_runtime_info()

    def _start_async_task(
        self,
        operation: Callable[[OpenSandboxBackend], Awaitable[_ResultT]],
    ) -> tuple[asyncio.Task[_ResultT], _AsyncStartState]:
        """Pin a backend in an owned task until the remote call settles."""

        return _rooted_transfer._start_async_task(
            self,
            operation,
        )

    def _finish_async_task(self, task: asyncio.Task[Any]) -> None:
        """Retain a background task and consume errors after caller cancellation."""

        return _rooted_transfer._finish_async_task(
            self,
            task,
        )

    async def _run_async(
        self,
        operation: Callable[[OpenSandboxBackend], Awaitable[_ResultT]],
    ) -> _ResultT:
        """Forward cancellation and await native cleanup before releasing the caller."""

        return await _rooted_transfer._run_async(
            self,
            operation,
        )

    @contextmanager
    def _lease_backend(self) -> Generator[OpenSandboxBackend]:
        """Pin the Handle backend for one synchronous protocol call."""

        return _rooted_transfer._lease_backend(
            self,
        )

    def _reject_sync_remote_io(self) -> NoReturn:
        """Reject sync paths before any remote filesystem operation."""

        return _rooted_transfer._reject_sync_remote_io(
            self,
        )

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a Shell command unchanged; the virtual root scopes file tools."""
        with self._lease_backend() as backend:
            return backend.execute(command, timeout=timeout)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a Shell command unchanged through the native async backend."""

        async def operation(backend: OpenSandboxBackend) -> ExecuteResponse:
            return await backend.aexecute(command, timeout=timeout)

        return await self._run_async(operation)

    def to_shell_path(self, file_path: str) -> str:
        """Convert a virtual file-tool path to a workspace-relative Shell path."""

        mapped = self._map_path(file_path)
        if mapped is None:
            raise ValueError("file_path must be a safe virtual path")
        return mapped.virtual.lstrip("/") or "."

    def _map_path(self, path: str) -> _MappedPath | None:
        """Normalize a caller path into virtual and physical Sandbox paths."""

        return _rooted_projection._map_path(
            self,
            path,
        )

    @staticmethod
    def _display_input(path: str) -> str:
        """Escape NUL bytes while preserving other model input for diagnostics."""

        return _rooted_projection._display_input(
            path,
        )

    def _invalid_path_error(self, path: str) -> str:
        """Return a stable error that reveals no physical root or symlink target."""

        return _rooted_projection._invalid_path_error(
            self,
            path,
        )

    @staticmethod
    def _read_as_binary(path: str) -> bool:
        """Match Deep Agents' non-text extension classification."""

        return _rooted_projection._read_as_binary(
            path,
        )

    def _project_read_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> ReadResult:
        """Translate one validated helper envelope into ``ReadResult``."""

        return _rooted_projection._project_read_response(
            self,
            requested=requested,
            response=response,
            request=request,
        )

    def _project_edit_response(
        self,
        *,
        requested: _MappedPath,
        old_string: str,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> EditResult:
        """Translate one validated helper envelope into ``EditResult``."""

        return _rooted_projection._project_edit_response(
            self,
            requested=requested,
            old_string=old_string,
            response=response,
            request=request,
        )

    @staticmethod
    def _edit_staging_paths() -> tuple[str, str]:
        """Allocate unguessable Sandbox paths for an oversized edit payload."""

        return _rooted_projection._edit_staging_paths()

    @staticmethod
    def _edit_upload_error(
        *,
        requested: _MappedPath,
        responses: list[FileUploadResponse],
    ) -> EditResult | None:
        """Project staged edit upload failures without starting the target edit."""

        return _rooted_projection._edit_upload_error(
            requested=requested,
            responses=responses,
        )

    @staticmethod
    def _edit_cleanup_command(old_path: str, new_path: str) -> str:
        """Build best-effort cleanup for generated staging paths only."""

        return _rooted_projection._edit_cleanup_command(
            old_path,
            new_path,
        )

    def _cleanup_edit_staging(
        self,
        backend: OpenSandboxBackend,
        *,
        old_path: str,
        new_path: str,
    ) -> None:
        """Best-effort cleanup without replaying the target edit."""

        return _rooted_projection._cleanup_edit_staging(
            self,
            backend,
            old_path=old_path,
            new_path=new_path,
        )

    async def _acleanup_edit_staging(
        self,
        backend: OpenSandboxBackend,
        *,
        old_path: str,
        new_path: str,
    ) -> None:
        """Asynchronously clean generated staging paths without replaying edit."""

        return await _rooted_projection._acleanup_edit_staging(
            self,
            backend,
            old_path=old_path,
            new_path=new_path,
        )

    def _project_delete_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> DeleteResult:
        """Translate one validated helper envelope into ``DeleteResult``."""

        return _rooted_projection._project_delete_response(
            self,
            requested=requested,
            response=response,
            request=request,
        )

    def _project_list_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> LsResult:
        """Translate one validated helper envelope into ``LsResult``."""

        return _rooted_projection._project_list_response(
            self,
            requested=requested,
            response=response,
            request=request,
        )

    def _project_glob_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> GlobResult:
        """Translate one validated helper envelope into ``GlobResult``."""

        return _rooted_projection._project_glob_response(
            self,
            requested=requested,
            response=response,
            request=request,
        )

    def _project_grep_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> GrepResult:
        """Translate one validated helper envelope into ``GrepResult``."""

        return _rooted_projection._project_grep_response(
            self,
            requested=requested,
            response=response,
            request=request,
        )

    def _restore_error(self, error: str | None) -> str | None:
        """Replace physical workspace prefixes in errors with the virtual root."""

        return _rooted_projection._restore_error(
            self,
            error,
        )

    @staticmethod
    def _is_safe_path_pattern(pattern: str) -> bool:
        """Return whether a glob pattern avoids traversal and NUL bytes."""

        return _rooted_projection._is_safe_path_pattern(
            pattern,
        )

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read a text or binary preview within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return ReadResult(error=self._invalid_path_error(file_path))
        offset, limit = normalize_read_bounds(offset, limit)
        request = _build_rooted_command(
            root=self._root,
            operation="read",
            arguments={
                "path": mapped.virtual,
                "offset": offset,
                "limit": limit,
                "binary": self._read_as_binary(mapped.virtual),
            },
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_read_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Asynchronously read a file within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return ReadResult(error=self._invalid_path_error(file_path))
        offset, limit = normalize_read_bounds(offset, limit)
        request = _build_rooted_command(
            root=self._root,
            operation="read",
            arguments={
                "path": mapped.virtual,
                "offset": offset,
                "limit": limit,
                "binary": self._read_as_binary(mapped.virtual),
            },
        )

        async def operation(backend: OpenSandboxBackend) -> ReadResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_read_response(
                requested=mapped,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def write(self, file_path: str, content: str) -> WriteResult:
        """Reject synchronous remote writes; use :meth:`awrite`."""
        del content
        mapped = self._map_path(file_path)
        if mapped is None:
            return WriteResult(error=self._invalid_path_error(file_path))
        self._reject_sync_remote_io()

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Asynchronously write a text file within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return WriteResult(error=self._invalid_path_error(file_path))

        async def operation(backend: OpenSandboxBackend) -> WriteResult:
            result = await backend._aupload_rooted_file(
                root=self._root,
                path=mapped.virtual,
                content=content.encode("utf-8"),
            )
            if result.error == INVALID_PATH:
                return WriteResult(error=self._invalid_path_error(file_path))
            if result.error is not None:
                return WriteResult(
                    error=(
                        f"Failed to write file '{mapped.virtual}': "
                        f"{self._restore_error(result.error)}"
                    )
                )
            return WriteResult(path=mapped.requested)

        return await self._run_async(operation)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Apply an exact text replacement within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return EditResult(error=self._invalid_path_error(file_path))
        payload_size = len(old_string.encode("utf-8")) + len(new_string.encode("utf-8"))
        if payload_size <= _ROOTED_EDIT_INLINE_MAX_BYTES:
            request = _build_rooted_command(
                root=self._root,
                operation="edit",
                arguments={
                    "path": mapped.virtual,
                    "old": old_string,
                    "new": new_string,
                    "replace_all": replace_all,
                },
            )
            with self._lease_backend() as backend, backend._rooted_file_operation():
                response = backend.execute(request.command)
            return self._project_edit_response(
                requested=mapped,
                old_string=old_string,
                response=response,
                request=request,
            )

        old_path, new_path = self._edit_staging_paths()
        request = _build_rooted_command(
            root=self._root,
            operation="edit",
            arguments={
                "path": mapped.virtual,
                "old_path": old_path,
                "new_path": new_path,
                "replace_all": replace_all,
            },
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            try:
                upload_responses = backend.upload_files(
                    [
                        (old_path, old_string.encode("utf-8")),
                        (new_path, new_string.encode("utf-8")),
                    ]
                )
                upload_error = self._edit_upload_error(
                    requested=mapped,
                    responses=upload_responses,
                )
                if upload_error is not None:
                    return upload_error
                response = backend.execute(request.command)
            finally:
                self._cleanup_edit_staging(
                    backend,
                    old_path=old_path,
                    new_path=new_path,
                )
        return self._project_edit_response(
            requested=mapped,
            old_string=old_string,
            response=response,
            request=request,
        )

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Asynchronously edit a text file within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return EditResult(error=self._invalid_path_error(file_path))
        payload_size = len(old_string.encode("utf-8")) + len(new_string.encode("utf-8"))
        if payload_size <= _ROOTED_EDIT_INLINE_MAX_BYTES:
            request = _build_rooted_command(
                root=self._root,
                operation="edit",
                arguments={
                    "path": mapped.virtual,
                    "old": old_string,
                    "new": new_string,
                    "replace_all": replace_all,
                },
            )

            async def inline_operation(backend: OpenSandboxBackend) -> EditResult:
                with backend._rooted_file_operation():
                    response = await backend.aexecute(request.command)
                return self._project_edit_response(
                    requested=mapped,
                    old_string=old_string,
                    response=response,
                    request=request,
                )

            return await self._run_async(inline_operation)

        old_path, new_path = self._edit_staging_paths()
        request = _build_rooted_command(
            root=self._root,
            operation="edit",
            arguments={
                "path": mapped.virtual,
                "old_path": old_path,
                "new_path": new_path,
                "replace_all": replace_all,
            },
        )

        async def operation(backend: OpenSandboxBackend) -> EditResult:
            with backend._rooted_file_operation():
                try:
                    upload_responses = await backend.aupload_files(
                        [
                            (old_path, old_string.encode("utf-8")),
                            (new_path, new_string.encode("utf-8")),
                        ]
                    )
                    upload_error = self._edit_upload_error(
                        requested=mapped,
                        responses=upload_responses,
                    )
                    if upload_error is not None:
                        return upload_error
                    response = await backend.aexecute(request.command)
                finally:
                    await self._acleanup_edit_staging(
                        backend,
                        old_path=old_path,
                        new_path=new_path,
                    )
            return self._project_edit_response(
                requested=mapped,
                old_string=old_string,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a path within the virtual root but never the root itself."""
        mapped = self._map_path(file_path)
        if mapped is None or mapped.virtual == "/":
            return DeleteResult(error=self._invalid_path_error(file_path))
        request = _build_rooted_command(
            root=self._root,
            operation="delete",
            arguments={"path": mapped.virtual},
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_delete_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def adelete(self, file_path: str) -> DeleteResult:
        """Asynchronously delete a path but never the virtual root itself."""
        mapped = self._map_path(file_path)
        if mapped is None or mapped.virtual == "/":
            return DeleteResult(error=self._invalid_path_error(file_path))
        request = _build_rooted_command(
            root=self._root,
            operation="delete",
            arguments={"path": mapped.virtual},
        )

        async def operation(backend: OpenSandboxBackend) -> DeleteResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_delete_response(
                requested=mapped,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def ls(self, path: str) -> LsResult:
        """List a virtual directory and restore model-visible entry paths."""
        mapped = self._map_path(path)
        if mapped is None:
            return LsResult(error=self._invalid_path_error(path), entries=None)
        request = _build_rooted_command(
            root=self._root,
            operation="list",
            arguments={"path": mapped.virtual},
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_list_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def als(self, path: str) -> LsResult:
        """Asynchronously list a virtual directory and restore entry paths."""
        mapped = self._map_path(path)
        if mapped is None:
            return LsResult(error=self._invalid_path_error(path), entries=None)
        request = _build_rooted_command(
            root=self._root,
            operation="list",
            arguments={"path": mapped.virtual},
        )

        async def operation(backend: OpenSandboxBackend) -> LsResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_list_response(
                requested=mapped,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Run a glob within a virtual search root and restore matched paths."""
        search_input = path or "/"
        mapped = self._map_path(search_input)
        if mapped is None or not self._is_safe_path_pattern(pattern):
            return GlobResult(
                error=self._invalid_path_error(search_input),
                matches=None,
            )
        request = _build_rooted_command(
            root=self._root,
            operation="glob",
            arguments={"path": mapped.virtual, "pattern": pattern},
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_glob_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Asynchronously run a glob within a virtual search root."""
        search_input = path or "/"
        mapped = self._map_path(search_input)
        if mapped is None or not self._is_safe_path_pattern(pattern):
            return GlobResult(
                error=self._invalid_path_error(search_input),
                matches=None,
            )
        request = _build_rooted_command(
            root=self._root,
            operation="glob",
            arguments={"path": mapped.virtual, "pattern": pattern},
        )

        async def operation(backend: OpenSandboxBackend) -> GlobResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_glob_response(
                requested=mapped,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Search text within a virtual root and restore matched paths."""
        search_input = path or "/"
        mapped = self._map_path(search_input)
        if mapped is None or (
            glob is not None and not self._is_safe_path_pattern(glob)
        ):
            return GrepResult(
                error=self._invalid_path_error(search_input),
                matches=None,
            )
        request = _build_rooted_command(
            root=self._root,
            operation="grep",
            arguments={
                "path": mapped.virtual,
                "pattern": pattern,
                "glob": glob,
                "max_count": max_count,
            },
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_grep_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Asynchronously search text within a virtual root."""
        search_input = path or "/"
        mapped = self._map_path(search_input)
        if mapped is None or (
            glob is not None and not self._is_safe_path_pattern(glob)
        ):
            return GrepResult(
                error=self._invalid_path_error(search_input),
                matches=None,
            )
        request = _build_rooted_command(
            root=self._root,
            operation="grep",
            arguments={
                "path": mapped.virtual,
                "pattern": pattern,
                "glob": glob,
                "max_count": max_count,
            },
        )

        async def operation(backend: OpenSandboxBackend) -> GrepResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_grep_response(
                requested=mapped,
                response=response,
                request=request,
            )

        task, state = self._start_async_task(operation)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=ASYNC_GREP_TIMEOUT,
            )
        except TimeoutError:
            if not state.has_started:
                task.cancel()
            return GrepResult(
                error=(
                    f"Error: grep timed out after {ASYNC_GREP_TIMEOUT}s. "
                    "Try a more specific pattern or a narrower path."
                )
            )
        except asyncio.CancelledError:
            if not state.has_started:
                task.cancel()
            raise

    def execute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        """Reject synchronous remote capture; use :meth:`aexecute_with_offload`."""

        return _rooted_transfer.execute_with_offload(
            self,
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
        """Asynchronously map a capture path or execute without offload."""

        return await _rooted_transfer.aexecute_with_offload(
            self,
            command,
            capture_path,
            max_inline_bytes=max_inline_bytes,
            max_capture_bytes=max_capture_bytes,
            timeout=timeout,
        )

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Reject valid sync downloads while returning local invalid-only batches."""

        return _rooted_transfer.download_files(
            self,
            paths,
        )

    async def aread_bytes(
        self, path: str, *, max_bytes: int, timeout: float = 30
    ) -> bytes:
        """Read a complete workspace binary file within an explicit byte limit.

        Args:
            path: Virtual file path within this view's workspace root.
            max_bytes: Maximum accepted size in bytes; zero permits only empty files.
            timeout: Read deadline in seconds, greater than zero and at most 290.
                Response and helper cleanup may each take up to five additional seconds.

        Returns:
            Complete bytes. No partial content is returned on overflow or failure.

        Raises:
            ValueError: The virtual path or limits are invalid.
            FileNotFoundError: The workspace file does not exist.
            PermissionError: The file is not readable.
            IsADirectoryError: The target is not a regular file.
            OpenSandboxFileTooLargeError: The content exceeds ``max_bytes``.
            OpenSandboxBackendError: The read or descriptor cleanup failed.
            OpenSandboxBackendTimeoutError: The read deadline expired.

        File descriptors enforce the workspace boundary even during path replacement.
        Cancellation stops the transfer and retains the Handle lease until response
        and descriptor cleanup finish. Memory retains bounded content, one transport
        chunk, and the final bytes copy.
        """
        mapped = self._map_path(path)
        if mapped is None:
            raise ValueError("path must be a valid workspace file path")
        async with self._handle._alease() as backend:
            return await backend._aread_rooted_bytes(
                root=self._root,
                path=mapped.virtual,
                max_bytes=max_bytes,
                timeout=timeout,
            )

    async def adownload_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Asynchronously download paths mapped through the virtual root."""

        return await _rooted_transfer.adownload_files(
            self,
            paths,
        )

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Reject valid sync uploads while returning local invalid-only batches."""

        return _rooted_transfer.upload_files(
            self,
            files,
        )

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Asynchronously upload paths mapped through the virtual root."""

        return await _rooted_transfer.aupload_files(
            self,
            files,
        )
