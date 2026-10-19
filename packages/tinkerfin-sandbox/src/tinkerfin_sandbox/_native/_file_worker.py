"""Perform a single file operation inside an already enforced OS sandbox."""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import asdict
from pathlib import Path

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import perform_string_replacement, slice_read_response

from ._file_request import FileRequest
from ._paths import local_path


def _target(request: FileRequest) -> Path:
    return local_path(
        request.path,
        directory=Path(request.root),
        user_home=Path(request.user_home),
    ).resolve()


def _resolved_path(request: FileRequest) -> Path:
    path = local_path(
        request.path,
        directory=Path(request.root),
        user_home=Path(request.user_home),
    )
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        # Non-strict realpath also suppresses permission failures. Missing
        # write targets are valid, but their existing parent must be resolvable
        # under this sandbox; metadata denial is never evidence of absence.
        resolved = path.resolve()
        parent = resolved
        while True:
            try:
                parent.stat()
            except FileNotFoundError:
                parent = parent.parent
            else:
                return resolved


def _read_bytes(request: FileRequest) -> bytes:
    with _target(request).open("rb") as stream:
        content = stream.read(request.max_file_bytes + 1)
    if len(content) > request.max_file_bytes:
        raise ValueError("file exceeds its byte limit")
    return content


def _read(request: FileRequest) -> ReadResult:
    if request.limit <= 0:
        return ReadResult(
            file_data={"content": "", "encoding": "utf-8"}, no_lines_requested=True
        )
    try:
        content = _read_bytes(request)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return ReadResult(
                file_data={
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                }
            )
        return slice_read_response(
            {"content": text, "encoding": "utf-8"}, request.offset, request.limit
        )
    except (OSError, ValueError):
        return ReadResult(
            error="File is unavailable, outside the grant, or exceeds its byte limit"
        )


def _edit(request: FileRequest, backend: FilesystemBackend) -> EditResult:
    try:
        original = _read_bytes(request).decode("utf-8")
        replaced = perform_string_replacement(
            original, request.old, request.new, request.replace_all
        )
        if isinstance(replaced, str):
            return EditResult(error=replaced)
        content, count = replaced
        if len(content.encode("utf-8")) > request.max_file_bytes:
            return EditResult(error="Edited file exceeds its byte limit")
        # Detect a concurrent change before committing the replacement. Shell
        # writes remain external effects; callers must coordinate concurrent edits.
        if _read_bytes(request).decode("utf-8") != original:
            return EditResult(error="File changed while editing")
        written = backend.write(request.path, content)
        return EditResult(
            error=written.error,
            path=written.path,
            occurrences=count if written.error is None else None,
        )
    except (OSError, ValueError):
        return EditResult(
            error="File is unavailable, outside the grant, or exceeds its byte limit"
        )


def main() -> None:
    """Read a validated request and write only its JSON protocol result."""
    raw = sys.stdin.buffer.read(96 * 1024 * 1024 + 1)
    if len(raw) > 96 * 1024 * 1024:
        raise ValueError("file request exceeds the transfer limit")
    request = FileRequest.model_validate_json(raw)
    if request.operation == "resolve":
        try:
            resolved = str(_resolved_path(request))
        except (OSError, ValueError, RuntimeError):
            resolved = None
        sys.stdout.write(json.dumps(resolved))
        return
    backend = FilesystemBackend(
        root_dir=request.root,
        virtual_mode=False,
        max_file_size_mb=max(1, request.max_file_bytes // (1024 * 1024)),
    )
    request.path = str(
        local_path(
            request.path,
            directory=Path(request.root),
            user_home=Path(request.user_home),
        )
    )
    result: (
        LsResult
        | ReadResult
        | WriteResult
        | EditResult
        | DeleteResult
        | GlobResult
        | GrepResult
    )
    match request.operation:
        case "ls":
            result = backend.ls(request.path)
        case "read":
            result = _read(request)
        case "write":
            if len(request.content.encode("utf-8")) > request.max_file_bytes:
                result = WriteResult(error="File exceeds its byte limit")
            else:
                result = backend.write(request.path, request.content)
        case "edit":
            result = _edit(request, backend)
        case "delete":
            result = backend.delete(request.path)
        case "glob":
            result = backend.glob(request.pattern, path=request.path)
        case "grep":
            result = backend.grep(
                request.pattern,
                path=request.path,
                glob=request.include,
                max_count=request.max_count,
            )
        case "upload" | "download":
            try:
                target = _target(request)
                if request.operation == "upload":
                    data = base64.b64decode(request.content, validate=True)
                    if len(data) > request.max_file_bytes:
                        raise ValueError("file exceeds transfer limit")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    response = {"error": None, "content": None}
                else:
                    with target.open("rb") as handle:
                        data = handle.read(request.max_file_bytes + 1)
                    if len(data) > request.max_file_bytes:
                        raise ValueError("file exceeds transfer limit")
                    response = {
                        "error": None,
                        "content": base64.b64encode(data).decode("ascii"),
                    }
            except FileNotFoundError:
                response = {"error": "file_not_found", "content": None}
            except IsADirectoryError:
                response = {"error": "is_directory", "content": None}
            except PermissionError:
                response = {"error": "permission_denied", "content": None}
            except (OSError, ValueError):
                response = {"error": "invalid_path", "content": None}
            sys.stdout.write(json.dumps(response))
            return
    sys.stdout.write(json.dumps(asdict(result), ensure_ascii=False))


if __name__ == "__main__":
    main()
