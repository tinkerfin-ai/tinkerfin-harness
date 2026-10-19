"""Provide the locked Deep Agents backend contract through remote operations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal, TypeVar

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)
from pydantic import JsonValue, TypeAdapter, ValidationError

from ._wire import digest_bytes
from .errors import RemoteError, RemoteErrorCode
from .models import (
    BinaryFile,
    DeleteFile,
    DownloadedFile,
    DownloadFiles,
    EditFile,
    ExecuteCommand,
    GlobFiles,
    GrepFiles,
    ListFiles,
    Operation,
    ReadFile,
    ResolvePath,
    UploadFiles,
    WorkspaceRequest,
    WriteFile,
)

if TYPE_CHECKING:
    from .remote import RemoteEndpoint

ResultT = TypeVar("ResultT")
_ls = TypeAdapter(LsResult)
_read = TypeAdapter(ReadResult)
_write = TypeAdapter(WriteResult)
_edit = TypeAdapter(EditResult)
_delete = TypeAdapter(DeleteResult)
_glob = TypeAdapter(GlobResult)
_grep = TypeAdapter(GrepResult)
_execute = TypeAdapter(ExecuteResponse)
_uploads = TypeAdapter(list[FileUploadResponse])
_downloads = TypeAdapter(list[DownloadedFile])


def _decode_result(value: JsonValue, adapter: TypeAdapter[ResultT]) -> ResultT:
    try:
        return adapter.validate_json(json.dumps(value, allow_nan=False), strict=True)
    except ValidationError as error:
        # Validation errors include the peer's original payload. Keep that input
        # in the private cause, never in the framework's client-safe explanation.
        raise RemoteError(
            RemoteErrorCode.PROTOCOL, "Remote operation result is invalid", cause=error
        ) from error


class RemoteSession(SandboxBackendProtocol):
    """Borrow asynchronous native file and command access for one prepared Run.

    Obtain sessions from RemoteWorkspace.prepare. The selected workspace resolves
    relative and home-relative POSIX paths. Absolute targets are rechecked locally
    before execution. Trailing separators, repeated interior separators and ``.``
    components are accepted; resolution must retain the same POSIX components
    without folding ``..`` or following a symlink to another target.
    Upload and download results preserve requested paths and input order; other
    file results retain the backend's absolute-path contract.
    Synchronous methods are unsupported. Streams and results use the current
    framework operation scope, so callers must not retain access after a tool ends.
    """

    def __init__(self, endpoint: RemoteEndpoint, request: WorkspaceRequest) -> None:
        """Bind a prepared resource borrow without creating a second execution owner."""
        self._endpoint = endpoint
        self._request = request
        self._closed = False

    @property
    def id(self) -> str:
        """Identify this resource borrow, distinct from stable authorization scope."""
        return self._request.session_id

    def _require_open(self) -> None:
        if self._closed:
            raise RemoteError(
                RemoteErrorCode.CLOSED, "Remote workspace borrow has ended"
            )

    async def _run(
        self, operation: Operation, adapter: TypeAdapter[ResultT]
    ) -> ResultT:
        self._require_open()
        result = (await self._endpoint._invoke(self._request, operation)).result
        return _decode_result(result.value, adapter)

    async def resolve_file_path(
        self, path: str | None, *, operation: Literal["read", "write"]
    ) -> str:
        """Observe a current canonical target without creating or writing files."""
        return await self._run(
            ResolvePath(path=path, operation=operation), TypeAdapter(str)
        )

    async def als(self, path: str) -> LsResult:
        """List the current authorized directory."""
        return await self._run(ListFiles(path=path), _ls)

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        """Read the requested line window with the backend's pagination metadata."""
        return await self._run(
            ReadFile(path=file_path, offset=offset, limit=limit), _read
        )

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Create a text file once under the current local access policy."""
        return await self._run(WriteFile(path=file_path, content=content), _write)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Apply the immutable exact-match edit once."""
        return await self._run(
            EditFile(
                path=file_path, old=old_string, new=new_string, replace_all=replace_all
            ),
            _edit,
        )

    async def adelete(self, file_path: str) -> DeleteResult:
        """Delete an authorized file in the selected workspace."""
        return await self._run(DeleteFile(path=file_path), _delete)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find files below an authorized native search root."""
        return await self._run(GlobFiles(pattern=pattern, path=path), _glob)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Search files while preserving truncation and match-count semantics."""
        return await self._run(
            GrepFiles(pattern=pattern, path=path, glob=glob, max_count=max_count), _grep
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Reject synchronous work; use aexecute from managed asynchronous tools."""
        raise NotImplementedError("RemoteSession requires aexecute")

    async def aexecute(
        self, command: str, *, timeout: int | None = None
    ) -> ExecuteResponse:
        """Run a concrete command with progress emitted through the current tool."""
        return await self._run(
            ExecuteCommand(command=command, timeout=timeout), _execute
        )

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """Verify and transfer bounded bytes before one local upload invocation."""
        self._require_open()
        if not files:
            return []
        if (
            sum(len(content) for _path, content in files)
            > self._endpoint._owner._limits.file_bytes
        ):
            raise RemoteError(
                RemoteErrorCode.CAPACITY, "Upload exceeds its total byte limit"
            )
        operation = UploadFiles(
            files=tuple(
                BinaryFile(path=path, size=len(content), digest=digest_bytes(content))
                for path, content in files
            )
        )
        result = await self._endpoint._invoke(
            self._request, operation, uploads=tuple(content for _path, content in files)
        )
        decoded = _decode_result(result.result.value, _uploads)
        if [item.path for item in decoded] != [path for path, _content in files]:
            raise RemoteError(
                RemoteErrorCode.PROTOCOL, "Upload result changed its file correlation"
            )
        return decoded

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Return bytes only after complete length and digest verification."""
        self._require_open()
        if not paths:
            return []
        result = await self._endpoint._invoke(
            self._request, DownloadFiles(paths=tuple(paths))
        )
        decoded = _decode_result(result.result.value, _downloads)
        if [item.path for item in decoded] != paths:
            raise RemoteError(
                RemoteErrorCode.PROTOCOL, "Download result changed its file correlation"
            )
        files: list[FileDownloadResponse] = []
        for index, item in enumerate(decoded):
            content = result.files.get(index, b"")
            if item.digest is not None:
                if (
                    item.error is not None
                    or len(content) != item.size
                    or digest_bytes(content) != item.digest
                ):
                    raise RemoteError(
                        RemoteErrorCode.PROTOCOL,
                        "Downloaded file failed length or digest verification",
                    )
                files.append(FileDownloadResponse(path=item.path, content=content))
            else:
                if content or item.size or item.error is None:
                    raise RemoteError(
                        RemoteErrorCode.PROTOCOL,
                        "Download failure has invalid file bytes",
                    )
                files.append(FileDownloadResponse(path=item.path, error=item.error))
        if set(result.files).difference(range(len(decoded))):
            raise RemoteError(
                RemoteErrorCode.PROTOCOL, "Download contains undeclared files"
            )
        return files


__all__ = ["RemoteSession"]
