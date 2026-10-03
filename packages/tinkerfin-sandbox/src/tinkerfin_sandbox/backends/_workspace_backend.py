"""Project all Deep Agents workspace operations into one isolated Run session."""

from __future__ import annotations

__all__ = ["_WorkspaceBackend"]

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextvars import ContextVar
from datetime import timedelta
from pathlib import PurePosixPath
from typing import TypeVar

from deepagents.backends.protocol import (
    ASYNC_GREP_TIMEOUT,
    FILE_NOT_FOUND,
    INVALID_PATH,
    PERMISSION_DENIED,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GrepResult,
)

from ..errors import (
    OpenSandboxHandleClosedError,
    OpenSandboxHandleOwnershipError,
)
from ..models import OpenSandboxRuntimeInfo
from ._bounded_read import validate_read_limits
from ._isolated import _uuid_text, _workspace_call, _WorkspaceConnection
from ._operations import RemoteOperations
from ._rooted_protocol import _build_rooted_command
from .handle import OpenSandboxHandle
from .rooted import RootedOpenSandboxBackend
from .sdk import _ROOTED_INTERNAL_COMMAND_ENV, OpenSandboxBackend

_ResultT = TypeVar("_ResultT")


async def _await_cleanup(task: asyncio.Task[_ResultT]) -> _ResultT:
    """Finish one owned cleanup despite repeated cancellation, then propagate it."""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except BaseException:  # noqa: BLE001 - consume the owned result below
            break
    if cancellation is not None:
        try:
            task.result()
        except BaseException as error:
            raise cancellation from error
        raise cancellation
    return task.result()


class _IsolatedBackend(OpenSandboxBackend):
    """Reuse helper construction without ever retaining a parent Sandbox object.

    The workspace orchestrator owns the fixed parent lease. This adapter admits at
    most sixteen local I/O tasks and serializes foreground commands because execd
    uses one persistent bash process per session. Uncertain operations close local
    admission and require namespaced DELETE before cancellation or failure escapes.
    It cannot renew, destroy, query, or close the physical parent sandbox.
    """

    def __init__(
        self,
        connection: _WorkspaceConnection,
        *,
        session_id: str,
        session_namespace: str,
        stop_run: Callable[[], Coroutine[None, None, None]],
        environment: Mapping[str, str],
        default_timeout: int,
        enable_capture_offload: bool,
    ) -> None:
        if default_timeout < 0:
            raise ValueError("default_timeout must not be negative")
        self._connection = connection
        self._session_id = _uuid_text(session_id)
        self._session_namespace = _uuid_text(session_namespace)
        self._stop_run = stop_run
        self._default_timeout = default_timeout
        self._command_env = dict(environment)
        self._working_directory = "/workspace"
        self._is_rooted_file_operation = ContextVar(
            f"isolated_workspace_file_operation_{id(self)}", default=False
        )
        self.enable_capture_offload = enable_capture_offload
        self._remote_operations = RemoteOperations()
        self._command_lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(16)
        self._active: set[asyncio.Task[object]] = set()
        self._stop_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def id(self) -> str:
        """Return the pinned physical sandbox ID, even after parent replacement."""
        return self._connection.sandbox_id

    @property
    def is_closed(self) -> bool:
        """Return whether this Run has permanently closed operation admission."""
        return self._closed

    def _check_open(self) -> None:
        if self._closed:
            raise OpenSandboxHandleClosedError("Workspace Run is closed")

    async def _stop(self) -> None:
        self._closed = True
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_run(),
                name=f"tinkerfin-workspace-stop:{self._session_id}",
            )
        await _await_cleanup(self._stop_task)

    async def _perform(self, operation: Callable[[], Awaitable[_ResultT]]) -> _ResultT:
        self._check_open()
        async with self._capacity:
            self._check_open()

            async def run() -> _ResultT:
                try:
                    return await _workspace_call(operation())
                except (FileNotFoundError, PermissionError, IsADirectoryError):
                    raise
                except BaseException as primary:
                    try:
                        await self._stop()
                    except BaseException as cleanup_error:
                        raise primary from cleanup_error
                    raise

            task = asyncio.create_task(run(), name="tinkerfin-workspace-operation")
            self._active.add(task)
            try:
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError as cancellation:
                    task.cancel()
                    try:
                        await _await_cleanup(task)
                    except BaseException as error:
                        raise cancellation from error
                    raise
            finally:
                self._active.discard(task)

    async def aexecute(
        self, command: str, *, timeout: int | None = None
    ) -> ExecuteResponse:
        """Execute once in the fixed Run; timeout or cancellation ends that Run."""
        effective_timeout = self._default_timeout if timeout is None else timeout
        if effective_timeout < 0:
            raise ValueError("timeout must not be negative")
        environment = dict(self._command_env)
        if self._is_rooted_file_operation.get():
            environment.update(_ROOTED_INTERNAL_COMMAND_ENV)
        scoped_command = f"cd /workspace && {command}"

        async def execute() -> ExecuteResponse:
            async with self._command_lock:
                self._check_open()
                async with asyncio.timeout(effective_timeout or None):
                    return await self._connection.run(
                        self._session_id,
                        command=scoped_command,
                        environment=environment,
                        timeout=effective_timeout,
                    )

        return await self._perform(execute)

    @staticmethod
    def _native_path(path: str, *, root: str | None = None) -> str:
        if "\x00" in path or path.startswith("//") or ".." in PurePosixPath(path).parts:
            raise ValueError("path must remain within the workspace")
        if root is not None:
            if root != "/workspace":
                raise ValueError("workspace helper root must be /workspace")
            relative = path.lstrip("/")
        else:
            candidate = PurePosixPath(path)
            try:
                relative = str(candidate.relative_to("/workspace"))
            except ValueError as error:
                raise ValueError("path must be an absolute workspace path") from error
        if not relative or relative == ".":
            raise ValueError("path must identify a workspace file")
        return relative

    async def _aupload_rooted_file(
        self, *, root: str, path: str, content: bytes
    ) -> FileUploadResponse:
        """Send a virtual path only to the isolated session's descriptor-safe upload."""
        try:
            native = self._native_path(path, root=root)
        except ValueError:
            return FileUploadResponse(path=path, error=INVALID_PATH)
        try:
            await self._perform(
                lambda: self._connection.upload(self._session_id, native, content)
            )
        except PermissionError:
            return FileUploadResponse(path=path, error=PERMISSION_DENIED)
        return FileUploadResponse(path=path, error=None)

    async def _adownload_rooted_file(
        self, *, root: str, path: str
    ) -> FileDownloadResponse:
        """Download from the isolated file route without a parent /proc helper."""
        try:
            native = self._native_path(path, root=root)
        except ValueError:
            return FileDownloadResponse(path=path, content=None, error=INVALID_PATH)
        try:
            content = await self._perform(
                lambda: self._connection.download(self._session_id, native)
            )
        except (FileNotFoundError, PermissionError) as error:
            return FileDownloadResponse(
                path=path,
                content=None,
                error=FILE_NOT_FOUND
                if isinstance(error, FileNotFoundError)
                else PERMISSION_DENIED,
            )
        return FileDownloadResponse(path=path, content=content, error=None)

    async def _aread_rooted_bytes(
        self, *, root: str, path: str, max_bytes: int, timeout: float
    ) -> bytes:
        """Keep bounded download and its response cleanup inside the fixed Run."""
        validate_read_limits(max_bytes, timeout)
        native = self._native_path(path, root=root)

        async def read() -> bytes:
            async with asyncio.timeout(timeout):
                return await self._connection.download(
                    self._session_id, native, max_bytes=max_bytes
                )

        return await self._perform(read)

    async def aread_bytes(
        self, path: str, *, max_bytes: int, timeout: float = 30
    ) -> bytes:
        """Read a physical workspace path without exposing parent files."""
        native = self._native_path(path)
        return await self._aread_rooted_bytes(
            root="/workspace", path=native, max_bytes=max_bytes, timeout=timeout
        )

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """Upload helper staging paths under /workspace in the original batch order."""
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                native = self._native_path(path)
            except ValueError:
                responses.append(FileUploadResponse(path=path, error=INVALID_PATH))
                continue
            result = await self._aupload_rooted_file(
                root="/workspace", path=native, content=content
            )
            responses.append(FileUploadResponse(path=path, error=result.error))
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Reject paths outside /workspace before requesting native file access."""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                native = self._native_path(path)
            except ValueError:
                responses.append(
                    FileDownloadResponse(path=path, content=None, error=INVALID_PATH)
                )
                continue
            result = await self._adownload_rooted_file(root="/workspace", path=native)
            responses.append(
                FileDownloadResponse(
                    path=path, content=result.content, error=result.error
                )
            )
        return responses

    async def arenew(self, timeout: timedelta) -> None:
        """Reject physical sandbox lifecycle control through a workspace Run."""
        del timeout
        raise OpenSandboxHandleOwnershipError(
            "Workspace Runs do not own the parent sandbox"
        )

    async def akill(self) -> None:
        """Reject destruction of the borrowed physical sandbox."""
        raise OpenSandboxHandleOwnershipError(
            "Workspace Runs do not own the parent sandbox"
        )

    async def aget_runtime_info(self) -> OpenSandboxRuntimeInfo:
        """Reject parent lifecycle queries from this file and command view."""
        raise OpenSandboxHandleOwnershipError(
            "Workspace Runs do not own the parent sandbox"
        )

    async def aclose(self) -> None:
        """Confirm remote termination, then cancel and drain owned local I/O."""

        async def close() -> None:
            try:
                await self._stop()
            finally:
                pending = tuple(self._active)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        if self._close_task is None:
            self._close_task = asyncio.create_task(
                close(), name="tinkerfin-workspace-close"
            )
        await _await_cleanup(self._close_task)


class _WorkspaceBackend(RootedOpenSandboxBackend):
    """Reuse the virtual file protocol over a fixed, exclusively isolated adapter.

    The private Handle never receives a replacement and contains no parent backend.
    Thus inherited binary reads, transfer callbacks, helpers, and output capture all
    enter the same session. The orchestrator retains ownership of the parent lease
    and the HTTP connection; closing this view only terminates its isolated Run.
    """

    def __init__(
        self,
        connection: _WorkspaceConnection,
        *,
        session_id: str,
        session_namespace: str,
        stop_run: Callable[[], Coroutine[None, None, None]],
        environment: Mapping[str, str],
        default_timeout: int = 60,
        enable_capture_offload: bool = False,
    ) -> None:
        self._isolated = _IsolatedBackend(
            connection,
            session_id=session_id,
            session_namespace=session_namespace,
            stop_run=stop_run,
            environment=environment,
            default_timeout=default_timeout,
            enable_capture_offload=enable_capture_offload,
        )
        super().__init__(OpenSandboxHandle(self._isolated), root="/workspace")

    @property
    def is_closed(self) -> bool:
        """Return the fixed Run's operation-admission state."""
        return self._isolated.is_closed

    async def _run_async(
        self, operation: Callable[[OpenSandboxBackend], Awaitable[_ResultT]]
    ) -> _ResultT:
        return await _workspace_call(operation(self._isolated))

    @staticmethod
    def _edit_staging_paths() -> tuple[str, str]:
        prefix = f"/workspace/.tinkerfin-rooted-edit-{secrets.token_hex(16)}"
        return f"{prefix}-old", f"{prefix}-new"

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Search within the Run without detaching work when its deadline expires."""
        search_input = path or "/"
        mapped = self._map_path(search_input)
        if mapped is None or (
            glob is not None and not self._is_safe_path_pattern(glob)
        ):
            return GrepResult(
                error=self._invalid_path_error(search_input), matches=None
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
        try:
            async with asyncio.timeout(ASYNC_GREP_TIMEOUT):
                with self._isolated._rooted_file_operation():
                    response = await self._isolated.aexecute(request.command)
        except TimeoutError:
            termination = self._isolated._stop_task
            if termination is not None:
                termination.result()
            return GrepResult(
                error=f"Error: grep timed out after {ASYNC_GREP_TIMEOUT}s."
            )
        return self._project_grep_response(
            requested=mapped, response=response, request=request
        )

    async def aclose(self) -> None:
        """End the isolated Run and await all local operations, leaving its parent open."""
        await self._isolated.aclose()
