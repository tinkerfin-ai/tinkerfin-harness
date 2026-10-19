"""Validate the private JSON boundary before constructing dependency results."""

from __future__ import annotations

from typing import Literal, TypeVar

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileData,
    FileInfo,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from pydantic import BaseModel, ConfigDict

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


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    error: str | None = None


class _FileInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    is_dir: bool | None = None
    size: int | None = None
    modified_at: str | None = None

    def value(self) -> FileInfo:
        result: FileInfo = {"path": self.path}
        if self.is_dir is not None:
            result["is_dir"] = self.is_dir
        if self.size is not None:
            result["size"] = self.size
        if self.modified_at is not None:
            result["modified_at"] = self.modified_at
        return result


class _FileData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str
    encoding: Literal["utf-8", "base64"]
    created_at: str | None = None
    modified_at: str | None = None

    def value(self) -> FileData:
        result: FileData = {"content": self.content, "encoding": self.encoding}
        if self.created_at is not None:
            result["created_at"] = self.created_at
        if self.modified_at is not None:
            result["modified_at"] = self.modified_at
        return result


class _Ls(_Payload):
    entries: list[_FileInfo] | None = None


class _Read(_Payload):
    file_data: _FileData | None = None
    total_lines: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    next_offset: int | None = None
    no_lines_requested: bool = False


class _Write(_Payload):
    path: str | None = None


class _Edit(_Write):
    occurrences: int | None = None


class _Match(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    line: int
    text: str

    def value(self) -> GrepMatch:
        return {"path": self.path, "line": self.line, "text": self.text}


class _Grep(_Payload):
    matches: list[_Match] | None = None
    truncated: bool = False


class _Glob(_Payload):
    matches: list[_FileInfo] | None = None
    truncated: bool = False
    truncation_reason: Literal["budget", "unreadable", "transport"] | None = None


def decode_result(kind: type[_Result], content: bytes) -> _Result:
    """Validate a fixed operation result without depending on runtime TypedDict support.

    Deep Agents 0.7.19 uses typing.TypedDict in its dataclasses. Pydantic cannot
    derive their schemas on Python 3.11; the wire models here also make unknown
    result fields a protocol error instead of silently discarding them.
    """
    result: (
        LsResult
        | ReadResult
        | WriteResult
        | EditResult
        | DeleteResult
        | GlobResult
        | GrepResult
    )
    if kind is LsResult:
        listing = _Ls.model_validate_json(content)
        result = LsResult(
            error=listing.error,
            entries=None
            if listing.entries is None
            else [item.value() for item in listing.entries],
        )
    elif kind is ReadResult:
        read = _Read.model_validate_json(content)
        result = ReadResult(
            error=read.error,
            file_data=None if read.file_data is None else read.file_data.value(),
            total_lines=read.total_lines,
            start_line=read.start_line,
            end_line=read.end_line,
            next_offset=read.next_offset,
            no_lines_requested=read.no_lines_requested,
        )
    elif kind is WriteResult:
        write = _Write.model_validate_json(content)
        result = WriteResult(error=write.error, path=write.path)
    elif kind is DeleteResult:
        delete = _Write.model_validate_json(content)
        result = DeleteResult(error=delete.error, path=delete.path)
    elif kind is EditResult:
        edit = _Edit.model_validate_json(content)
        result = EditResult(
            error=edit.error, path=edit.path, occurrences=edit.occurrences
        )
    elif kind is GrepResult:
        grep = _Grep.model_validate_json(content)
        result = GrepResult(
            error=grep.error,
            matches=None
            if grep.matches is None
            else [item.value() for item in grep.matches],
            truncated=grep.truncated,
        )
    elif kind is GlobResult:
        glob = _Glob.model_validate_json(content)
        result = GlobResult(
            error=glob.error,
            matches=None
            if glob.matches is None
            else [item.value() for item in glob.matches],
            truncated=glob.truncated,
            truncation_reason=glob.truncation_reason,
        )
    else:
        raise TypeError("unsupported file result type")
    if not isinstance(result, kind):
        raise TypeError("file result type does not match its operation")
    return result
