"""Dispatch concrete backend methods and encode finite, bounded results."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from pathlib import PurePosixPath
from typing import Protocol, TypeVar, runtime_checkable

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    ExecuteResponse,
    SandboxBackendProtocol,
)
from pydantic import JsonValue, TypeAdapter

from tinkerfin_contracts import PreparedWorkspace, ToolResult

from ._wire import digest, digest_bytes, encode
from .errors import RemoteError, RemoteErrorCode
from .limits import RemoteLimits
from .models import (
    BinaryChunk,
    CallTool,
    DeleteFile,
    DownloadedFile,
    DownloadFiles,
    EditFile,
    ExecuteCommand,
    GlobFiles,
    GrepFiles,
    ListFiles,
    OperationRequest,
    ReadFile,
    ResolvePath,
    ResultTransfer,
    UploadFiles,
    WriteFile,
)

WorkspaceT = TypeVar("WorkspaceT")
_json: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@runtime_checkable
class StreamingCommands(Protocol):
    """Expose bounded command progress without transferring process ownership."""

    async def aexecute_stream(
        self,
        command: str,
        *,
        timeout: int | None = None,
        emit_output: Callable[[str], Awaitable[None]],
    ) -> ExecuteResponse: ...


async def _checked_path(
    prepared: PreparedWorkspace[WorkspaceT, BackendProtocol],
    path: str | None,
    *,
    write: bool = False,
) -> str:
    resolver = prepared.file_path_resolver
    if resolver is None:
        raise RemoteError(
            RemoteErrorCode.REJECTED,
            "Remote workspace requires an authorized path resolver",
        )
    canonical = await resolver(path, operation="write" if write else "read")
    # Compare spelling without folding parent traversal or following symlinks;
    # the local resolver and backend retain authority over actual access.
    if (
        path is not None
        and path.startswith("/")
        and PurePosixPath(canonical) != PurePosixPath(path)
    ):
        raise RemoteError(
            RemoteErrorCode.REJECTED, "Absolute file target changed before execution"
        )
    return canonical


async def _chunks(
    identifier: str,
    content: bytes,
    *,
    channel: str,
    index: int,
    limits: RemoteLimits,
    emit: Callable[[BinaryChunk], Awaitable[None]],
) -> None:
    for offset in range(0, len(content), limits.chunk_bytes):
        part = content[offset : offset + limits.chunk_bytes]
        await emit(
            BinaryChunk(
                operation_id=identifier,
                channel="file" if channel == "file" else "result",
                file_index=index,
                offset=offset,
                data=base64.b64encode(part).decode("ascii"),
                digest=digest_bytes(part),
            )
        )


async def invoke(
    prepared: PreparedWorkspace[WorkspaceT, BackendProtocol],
    request: OperationRequest,
    *,
    uploads: tuple[bytes, ...],
    limits: RemoteLimits,
    emit_output: Callable[[JsonValue], Awaitable[None]],
    emit_binary: Callable[[BinaryChunk], Awaitable[None]],
) -> ToolResult:
    operation = request.operation
    backend = prepared.backend
    if isinstance(operation, ResolvePath):
        if prepared.file_path_resolver is None:
            raise RemoteError(
                RemoteErrorCode.REJECTED, "Workspace has no native path resolver"
            )
        return ToolResult(
            value=await prepared.file_path_resolver(
                operation.path, operation=operation.operation
            )
        )
    if isinstance(operation, CallTool):
        registered = next(
            (tool for tool in prepared.tools if tool.descriptor.name == operation.name),
            None,
        )
        if (
            registered is None
            or digest(registered.descriptor) != operation.descriptor_digest
        ):
            raise RemoteError(
                RemoteErrorCode.REJECTED, "Registered tool declaration changed"
            )
        return await registered.invoke(operation.arguments)
    if isinstance(operation, ExecuteCommand):
        commands = backend.default if isinstance(backend, CompositeBackend) else backend
        if isinstance(commands, StreamingCommands):

            async def progress(value: str) -> None:
                await emit_output(value)

            result = await commands.aexecute_stream(
                operation.command,
                timeout=operation.timeout,
                emit_output=progress,
            )
        elif isinstance(commands, SandboxBackendProtocol):
            result = await commands.aexecute(
                operation.command, timeout=operation.timeout
            )
        else:
            raise RemoteError(
                RemoteErrorCode.REJECTED, "Workspace does not execute commands"
            )
        return ToolResult(value=_json.validate_python(asdict(result), strict=True))
    if isinstance(operation, DownloadFiles):
        paths = [await _checked_path(prepared, path) for path in operation.paths]
        downloaded = await backend.adownload_files(paths)
        if len(downloaded) != len(paths) or any(
            item.path != path for item, path in zip(downloaded, paths, strict=True)
        ):
            raise RemoteError(
                RemoteErrorCode.PROTOCOL, "Download result changed its file correlation"
            )
        if sum(len(item.content or b"") for item in downloaded) > limits.file_bytes:
            raise RemoteError(
                RemoteErrorCode.CAPACITY, "Download exceeds its total byte limit"
            )
        descriptions: list[JsonValue] = []
        for index, item in enumerate(downloaded):
            content = item.content
            descriptions.append(
                _json.validate_python(
                    DownloadedFile(
                        path=operation.paths[index],
                        size=len(content or b""),
                        digest=digest_bytes(content) if content is not None else None,
                        error=item.error,
                    ).model_dump(mode="json"),
                    strict=True,
                )
            )
            if content is not None:
                await _chunks(
                    request.operation_id,
                    content,
                    channel="file",
                    index=index,
                    limits=limits,
                    emit=emit_binary,
                )
        return ToolResult(value=descriptions)
    if isinstance(operation, UploadFiles):
        if len(uploads) != len(operation.files):
            raise RemoteError(
                RemoteErrorCode.PROTOCOL, "Upload bytes are not completely verified"
            )
        paths = [
            await _checked_path(prepared, item.path, write=True)
            for item in operation.files
        ]
        uploaded = await backend.aupload_files(list(zip(paths, uploads, strict=True)))
        if len(uploaded) != len(paths) or any(
            item.path != path for item, path in zip(uploaded, paths, strict=True)
        ):
            raise RemoteError(
                RemoteErrorCode.PROTOCOL, "Upload result changed its file correlation"
            )
        return ToolResult(
            value=_json.validate_python(
                [
                    asdict(replace(item, path=request.path))
                    for item, request in zip(uploaded, operation.files, strict=True)
                ],
                strict=True,
            )
        )
    if isinstance(operation, ListFiles):
        return ToolResult(
            value=_json.validate_python(
                asdict(
                    await backend.als(await _checked_path(prepared, operation.path))
                ),
                strict=True,
            )
        )
    if isinstance(operation, ReadFile):
        return ToolResult(
            value=_json.validate_python(
                asdict(
                    await backend.aread(
                        await _checked_path(prepared, operation.path),
                        operation.offset,
                        operation.limit,
                    )
                ),
                strict=True,
            )
        )
    if isinstance(operation, WriteFile):
        return ToolResult(
            value=_json.validate_python(
                asdict(
                    await backend.awrite(
                        await _checked_path(prepared, operation.path, write=True),
                        operation.content,
                    )
                ),
                strict=True,
            )
        )
    if isinstance(operation, EditFile):
        return ToolResult(
            value=_json.validate_python(
                asdict(
                    await backend.aedit(
                        await _checked_path(prepared, operation.path, write=True),
                        operation.old,
                        operation.new,
                        operation.replace_all,
                    )
                ),
                strict=True,
            )
        )
    if isinstance(operation, DeleteFile):
        return ToolResult(
            value=_json.validate_python(
                asdict(
                    await backend.adelete(
                        await _checked_path(prepared, operation.path, write=True)
                    )
                ),
                strict=True,
            )
        )
    if isinstance(operation, GlobFiles):
        return ToolResult(
            value=_json.validate_python(
                asdict(
                    await backend.aglob(
                        operation.pattern, await _checked_path(prepared, operation.path)
                    )
                ),
                strict=True,
            )
        )
    assert isinstance(operation, GrepFiles)
    return ToolResult(
        value=_json.validate_python(
            asdict(
                await backend.agrep(
                    operation.pattern,
                    await _checked_path(prepared, operation.path),
                    operation.glob,
                    max_count=operation.max_count,
                )
            ),
            strict=True,
        )
    )


async def result_transfer(
    identifier: str,
    result: ToolResult,
    *,
    limits: RemoteLimits,
    emit: Callable[[BinaryChunk], Awaitable[None]],
) -> ResultTransfer | None:
    content = encode(result).encode()
    if len(content) > limits.output_bytes:
        raise RemoteError(
            RemoteErrorCode.CAPACITY, "Tool result exceeds its byte limit"
        )
    if len(content) < min(limits.request_bytes, limits.frame_bytes // 4):
        return None
    await _chunks(
        identifier, content, channel="result", index=0, limits=limits, emit=emit
    )
    return ResultTransfer(size=len(content), digest=digest_bytes(content))
