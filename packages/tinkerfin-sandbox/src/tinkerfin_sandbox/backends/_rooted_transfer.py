"""Rooted asynchronous lease ownership and bulk transfer operations."""

from __future__ import annotations

__all__ = [
    "_finish_async_task",
    "_lease_backend",
    "_reject_sync_remote_io",
    "_run_async",
    "_start_async_task",
]

import asyncio
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar

from deepagents.backends.protocol import (
    INVALID_PATH,
    ExecuteOffloadResult,
    FileDownloadResponse,
    FileUploadResponse,
)

from .sdk import OpenSandboxBackend

if TYPE_CHECKING:
    from .rooted import RootedOpenSandboxBackend

_ResultT = TypeVar("_ResultT")


@dataclass(slots=True)
class _AsyncStartState:
    """Track whether a native async operation has pinned a Handle lease."""

    has_started: bool = False


def _start_async_task(
    self: RootedOpenSandboxBackend,
    operation: Callable[[OpenSandboxBackend], Awaitable[_ResultT]],
) -> tuple[asyncio.Task[_ResultT], _AsyncStartState]:
    """Pin a backend in an owned task until the remote call settles."""
    state = _AsyncStartState()

    async def run() -> _ResultT:
        async with self._handle._alease() as backend:
            state.has_started = True
            return await operation(backend)

    task = asyncio.create_task(run())
    self._background_tasks.add(task)
    task.add_done_callback(self._finish_async_task)
    return task, state


def _finish_async_task(
    self: RootedOpenSandboxBackend,
    task: asyncio.Task[Any],
) -> None:
    """Retain a background task and consume errors after caller cancellation."""
    self._background_tasks.discard(task)
    if task.cancelled():
        return
    _ = task.exception()


async def _run_async(
    self: RootedOpenSandboxBackend,
    operation: Callable[[OpenSandboxBackend], Awaitable[_ResultT]],
) -> _ResultT:
    """Forward cancellation and await native cleanup before releasing the caller."""
    task, _ = self._start_async_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as error:
        cancellation = error

    # A caller may immediately remove or replace the files it was writing.
    # The native call must receive cancellation and finish its cleanup first;
    # retaining its lease alone would still permit a late write after removal.
    task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated caller cancellation must not interrupt native cleanup.
            continue
        except Exception:  # noqa: BLE001 - retrieve the owned task failure below
            break
    # Read the outcome outside the caller cancellation's except block so Python
    # does not replace the native exception's existing diagnostic context.
    try:
        task.result()
    except asyncio.CancelledError as native_cancellation:
        # SDK cleanup failures may be attached to its cancellation as notes.
        if native_cancellation is not cancellation:
            raise cancellation from native_cancellation
    except Exception as error:
        raise cancellation from error
    raise cancellation


def _lease_backend(
    self: RootedOpenSandboxBackend,
) -> Generator[OpenSandboxBackend]:
    """Pin the Handle backend for one synchronous protocol call."""
    with self._handle._lease() as backend:
        yield backend


def _reject_sync_remote_io(self: RootedOpenSandboxBackend) -> NoReturn:
    """Reject sync paths before any remote filesystem operation."""
    with self._lease_backend():
        OpenSandboxBackend._reject_sync()


def execute_with_offload(
    self: RootedOpenSandboxBackend,
    command: str,
    capture_path: str,
    *,
    max_inline_bytes: int,
    max_capture_bytes: int | None = None,
    timeout: int | None = None,
) -> ExecuteOffloadResult:
    """Reject synchronous remote capture; use :meth:`aexecute_with_offload`."""
    del command, capture_path, max_inline_bytes, max_capture_bytes, timeout
    self._reject_sync_remote_io()


async def aexecute_with_offload(
    self: RootedOpenSandboxBackend,
    command: str,
    capture_path: str,
    *,
    max_inline_bytes: int,
    max_capture_bytes: int | None = None,
    timeout: int | None = None,
) -> ExecuteOffloadResult:
    """Asynchronously map a capture path or execute without offload."""
    mapped = self._map_path(capture_path)

    async def operation(backend: OpenSandboxBackend) -> ExecuteOffloadResult:
        if mapped is not None:
            return await backend._aexecute_rooted_offload(
                root=self._root,
                command=command,
                capture_path=mapped.virtual,
                max_inline_bytes=max_inline_bytes,
                max_capture_bytes=max_capture_bytes,
                timeout=timeout,
            )
        response = await backend.aexecute(command, timeout=timeout)
        return ExecuteOffloadResult(offloaded=False, response=response)

    return await self._run_async(operation)


def download_files(
    self: RootedOpenSandboxBackend,
    paths: list[str],
) -> list[FileDownloadResponse]:
    """Reject valid sync downloads while returning local invalid-only batches."""
    mapped = [self._map_path(path) for path in paths]
    if any(item is not None for item in mapped):
        self._reject_sync_remote_io()
    return [
        FileDownloadResponse(
            path=path,
            content=None,
            error=INVALID_PATH,
        )
        for path in paths
    ]


async def adownload_files(
    self: RootedOpenSandboxBackend,
    paths: list[str],
) -> list[FileDownloadResponse]:
    """Asynchronously download paths mapped through the virtual root."""
    mapped = [self._map_path(path) for path in paths]
    candidates = [item for item in mapped if item is not None]
    if not candidates:
        return [
            FileDownloadResponse(
                path=path,
                content=None,
                error=INVALID_PATH,
            )
            for path in paths
        ]

    async def operation(
        backend: OpenSandboxBackend,
    ) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for index, item in enumerate(mapped):
            if item is None:
                responses.append(
                    FileDownloadResponse(
                        path=paths[index],
                        content=None,
                        error=INVALID_PATH,
                    )
                )
                continue
            raw = await backend._adownload_rooted_file(
                root=self._root,
                path=item.virtual,
            )
            responses.append(
                FileDownloadResponse(
                    path=item.requested,
                    content=raw.content,
                    error=self._restore_error(raw.error),
                )
            )
        return responses

    return await self._run_async(operation)


def upload_files(
    self: RootedOpenSandboxBackend,
    files: list[tuple[str, bytes]],
) -> list[FileUploadResponse]:
    """Reject valid sync uploads while returning local invalid-only batches."""
    mapped = [self._map_path(path) for path, _ in files]
    if any(item is not None for item in mapped):
        self._reject_sync_remote_io()
    return [FileUploadResponse(path=path, error=INVALID_PATH) for path, _ in files]


async def aupload_files(
    self: RootedOpenSandboxBackend,
    files: list[tuple[str, bytes]],
) -> list[FileUploadResponse]:
    """Asynchronously upload paths mapped through the virtual root."""
    mapped = [self._map_path(path) for path, _ in files]
    candidates = [item for item in mapped if item is not None]
    if not candidates:
        return [FileUploadResponse(path=path, error=INVALID_PATH) for path, _ in files]

    async def operation(
        backend: OpenSandboxBackend,
    ) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for index, item in enumerate(mapped):
            if item is None:
                responses.append(
                    FileUploadResponse(
                        path=files[index][0],
                        error=INVALID_PATH,
                    )
                )
                continue
            raw = await backend._aupload_rooted_file(
                root=self._root,
                path=item.virtual,
                content=files[index][1],
            )
            responses.append(
                FileUploadResponse(
                    path=item.requested,
                    error=self._restore_error(raw.error),
                )
            )
        return responses

    return await self._run_async(operation)
