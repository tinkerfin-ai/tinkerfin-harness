"""Use one native sandbox session for local file operations."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar

from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from ._file_request import FileRequest
from ._file_results import decode_result
from .errors import LocalError, LocalErrorCode

if TYPE_CHECKING:
    from .session import LocalSession

_Result = TypeVar(
    "_Result",
    LsResult,
    ReadResult,
    WriteResult,
    EditResult,
    DeleteResult,
    GlobResult,
    GrepResult,
)


class _Transfer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    error: (
        Literal["file_not_found", "permission_denied", "is_directory", "invalid_path"]
        | None
    )
    content: str | None


class LocalFiles(BackendProtocol):
    """Resolve native paths while borrowing the session's enforced policy."""

    def __init__(self, session: LocalSession, directory: Path) -> None:
        self._session = session
        self._directory = directory

    async def _file(
        self,
        result_type: type[_Result],
        operation: Literal["ls", "read", "write", "edit", "delete", "glob", "grep"],
        *,
        path: str = ".",
        content: str = "",
        pattern: str = "",
        old: str = "",
        new: str = "",
        offset: int = 0,
        limit: int = 2000,
        replace_all: bool = False,
        include: str | None = None,
        max_count: int | None = None,
    ) -> _Result:
        request = FileRequest(
            operation=operation,
            root=str(self._directory),
            user_home=str(self._session._user_home),
            path=path,
            content=content,
            pattern=pattern,
            old=old,
            new=new,
            offset=offset,
            limit=limit,
            replace_all=replace_all,
            include=include,
            max_count=max_count,
            max_file_bytes=self._session._limits.file_bytes,
        )
        try:
            return decode_result(result_type, await self._session._request(request))
        except ValidationError as error:
            raise LocalError(
                LocalErrorCode.PROTOCOL, "Isolated file result is invalid", cause=error
            ) from error

    async def als(self, path: str) -> LsResult:
        """List an accessible local directory."""
        return await self._file(LsResult, "ls", path=path)

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        """Read a bounded slice of an authorized file."""
        return await self._file(
            ReadResult, "read", path=file_path, offset=offset, limit=limit
        )

    async def awrite(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Write UTF-8 text to an authorized native path."""
        return await self._file(WriteResult, "write", path=file_path, content=content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Apply the file backend's exact-match edit semantics."""
        return await self._file(
            EditResult,
            "edit",
            path=file_path,
            old=old_string,
            new=new_string,
            replace_all=replace_all,
        )

    async def adelete(self, file_path: str) -> DeleteResult:
        """Delete an authorized file using the file backend's directory rules."""
        return await self._file(DeleteResult, "delete", path=file_path)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find authorized paths using the dependency's bounded glob implementation."""
        return await self._file(GlobResult, "glob", pattern=pattern, path=path or ".")

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Search literal file contents with the dependency's match-count bound."""
        return await self._file(
            GrepResult,
            "grep",
            pattern=pattern,
            path=path or ".",
            include=glob,
            max_count=max_count,
        )

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """Transfer files separately so each input is bounded and independently reported."""
        result: list[FileUploadResponse] = []
        for path, content in files:
            if len(content) > self._session._limits.file_bytes:
                raise LocalError(
                    LocalErrorCode.LIMIT, "Local file exceeds its byte limit"
                )
            raw = await self._session._request(
                FileRequest(
                    operation="upload",
                    root=str(self._directory),
                    user_home=str(self._session._user_home),
                    path=path,
                    content=base64.b64encode(content).decode("ascii"),
                    max_file_bytes=self._session._limits.file_bytes,
                )
            )
            transfer = _Transfer.model_validate_json(raw)
            result.append(FileUploadResponse(path=path, error=transfer.error))
        return result

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read bounded binary files from authorized native paths."""
        result: list[FileDownloadResponse] = []
        for path in paths:
            raw = await self._session._request(
                FileRequest(
                    operation="download",
                    root=str(self._directory),
                    user_home=str(self._session._user_home),
                    path=path,
                    max_file_bytes=self._session._limits.file_bytes,
                )
            )
            transfer = _Transfer.model_validate_json(raw)
            content = (
                None
                if transfer.content is None
                else base64.b64decode(transfer.content, validate=True)
            )
            result.append(
                FileDownloadResponse(path=path, content=content, error=transfer.error)
            )
        return result
