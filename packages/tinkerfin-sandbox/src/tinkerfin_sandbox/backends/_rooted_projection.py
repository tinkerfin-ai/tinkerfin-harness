"""Rooted path normalization and protocol response projection."""

from __future__ import annotations

__all__ = [
    "_acleanup_edit_staging",
    "_cleanup_edit_staging",
    "_display_input",
    "_edit_cleanup_command",
    "_edit_staging_paths",
    "_edit_upload_error",
    "_invalid_path_error",
    "_is_safe_path_pattern",
    "_map_path",
    "_project_delete_response",
    "_project_edit_response",
    "_project_glob_response",
    "_project_grep_response",
    "_project_list_response",
    "_project_read_response",
    "_read_as_binary",
    "_restore_error",
]

import secrets
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from deepagents.backends.protocol import (
    INVALID_PATH,
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
)

from ._rooted_protocol import _parse_rooted_response, _RootedCommand
from .sdk import OpenSandboxBackend

if TYPE_CHECKING:
    from .rooted import RootedOpenSandboxBackend

_ROOTED_BINARY_READ_SUFFIXES = frozenset(
    {
        ".aac",
        ".aiff",
        ".avi",
        ".flac",
        ".flv",
        ".gif",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogg",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".wav",
        ".webm",
        ".webp",
        ".wmv",
        ".3gpp",
    }
)


_ROOTED_EDIT_INLINE_MAX_BYTES = 50_000


@dataclass(frozen=True, slots=True)
class _MappedPath:
    """Store one virtual path input and its normalized Sandbox path."""

    requested: str
    virtual: str
    physical: str


def _map_path(self: RootedOpenSandboxBackend, path: str) -> _MappedPath | None:
    """Normalize a caller path into virtual and physical Sandbox paths."""
    if "\x00" in path or path.startswith("//"):
        return None
    virtual_path = PurePosixPath(path)
    if ".." in virtual_path.parts:
        return None

    relative_parts = tuple(
        part for part in virtual_path.parts if part not in {"/", "."}
    )
    normalized_virtual = "/" + "/".join(relative_parts)
    physical = str(PurePosixPath(self._root, *relative_parts))
    return _MappedPath(
        requested=path,
        virtual=normalized_virtual,
        physical=physical,
    )


def _display_input(path: str) -> str:
    """Escape NUL bytes while preserving other model input for diagnostics."""
    return path.replace("\x00", "\\0")


def _invalid_path_error(self: RootedOpenSandboxBackend, path: str) -> str:
    """Return a stable error that reveals no physical root or symlink target."""
    return f"Path '{self._display_input(path)}': {INVALID_PATH}"


def _read_as_binary(path: str) -> bool:
    """Match Deep Agents' non-text extension classification."""
    return PurePosixPath(path).suffix.lower() in _ROOTED_BINARY_READ_SUFFIXES


def _project_read_response(
    self: RootedOpenSandboxBackend,
    *,
    requested: _MappedPath,
    response: ExecuteResponse,
    request: _RootedCommand,
) -> ReadResult:
    """Translate one validated helper envelope into ``ReadResult``."""
    parsed = _parse_rooted_response(response, request=request)
    if parsed.status == "error":
        if parsed.error.code == "invalid_path":
            return ReadResult(error=self._invalid_path_error(requested.requested))
        message = {
            "not_found": "file_not_found",
            "permission_denied": "permission_denied",
            "not_a_file": "not_a_file",
            "not_directory": "not_a_file",
        }.get(parsed.error.code, parsed.error.message)
        return ReadResult(error=f"File '{requested.virtual}': {message}")
    if parsed.operation != "read":
        raise ValueError("rooted helper returned a non-read result")
    result = parsed.result
    return ReadResult(
        file_data={
            "content": result["content"],
            "encoding": result["encoding"],
        },
        total_lines=result["total_lines"],
        start_line=result["start_line"],
        end_line=result["end_line"],
        next_offset=result["next_offset"],
        no_lines_requested=result["no_lines_requested"],
    )


def _project_edit_response(
    self: RootedOpenSandboxBackend,
    *,
    requested: _MappedPath,
    old_string: str,
    response: ExecuteResponse,
    request: _RootedCommand,
) -> EditResult:
    """Translate one validated helper envelope into ``EditResult``."""
    parsed = _parse_rooted_response(response, request=request)
    if parsed.status == "error":
        code = parsed.error.code
        if code == "invalid_path":
            return EditResult(error=self._invalid_path_error(requested.requested))
        messages = {
            "not_found": f"Error: File '{requested.virtual}' not found",
            "permission_denied": (
                f"Error: Permission denied editing file '{requested.virtual}'"
            ),
            "not_a_file": f"Error: '{requested.virtual}' is not a regular file",
            "not_text_file": (f"Error: File '{requested.virtual}' is not a text file"),
            "string_not_found": (f"Error: String not found in file: '{old_string}'"),
            "multiple_occurrences": (
                f"Error: String '{old_string}' appears multiple times. "
                "Use replace_all=True to replace all occurrences."
            ),
        }
        return EditResult(
            error=messages.get(
                code,
                f"Error editing file '{requested.virtual}': {parsed.error.message}",
            )
        )
    if parsed.operation != "edit":
        raise ValueError("rooted helper returned a non-edit result")
    return EditResult(
        path=requested.requested,
        occurrences=parsed.result["count"],
    )


def _edit_staging_paths() -> tuple[str, str]:
    """Allocate unguessable Sandbox paths for an oversized edit payload."""
    token = secrets.token_hex(16)
    prefix = f"/tmp/.tinkerfin-rooted-edit-{token}"
    return f"{prefix}-old", f"{prefix}-new"


def _edit_upload_error(
    *,
    requested: _MappedPath,
    responses: list[FileUploadResponse],
) -> EditResult | None:
    """Project staged edit upload failures without starting the target edit."""
    if len(responses) != 2:
        return EditResult(
            error=(
                f"Error editing file '{requested.virtual}': upload returned no response"
            )
        )
    for response in responses:
        if response.error is not None:
            return EditResult(
                error=(f"Error editing file '{requested.virtual}': {response.error}")
            )
    return None


def _edit_cleanup_command(old_path: str, new_path: str) -> str:
    """Build best-effort cleanup for generated staging paths only."""
    return f"rm -f {shlex.quote(old_path)} {shlex.quote(new_path)}"


def _cleanup_edit_staging(
    self: RootedOpenSandboxBackend,
    backend: OpenSandboxBackend,
    *,
    old_path: str,
    new_path: str,
) -> None:
    """Best-effort cleanup without replaying the target edit."""
    try:
        backend.execute(self._edit_cleanup_command(old_path, new_path))
    except Exception:  # noqa: BLE001 - staging cleanup is best effort
        return


async def _acleanup_edit_staging(
    self: RootedOpenSandboxBackend,
    backend: OpenSandboxBackend,
    *,
    old_path: str,
    new_path: str,
) -> None:
    """Asynchronously clean generated staging paths without replaying edit."""
    try:
        await backend.aexecute(self._edit_cleanup_command(old_path, new_path))
    except Exception:  # noqa: BLE001 - staging cleanup is best effort
        return


def _project_delete_response(
    self: RootedOpenSandboxBackend,
    *,
    requested: _MappedPath,
    response: ExecuteResponse,
    request: _RootedCommand,
) -> DeleteResult:
    """Translate one validated helper envelope into ``DeleteResult``."""
    parsed = _parse_rooted_response(response, request=request)
    if parsed.status == "error":
        if parsed.error.code == "invalid_path":
            return DeleteResult(error=self._invalid_path_error(requested.requested))
        if parsed.error.code == "not_found":
            return DeleteResult(error=f"Error: '{requested.virtual}' not found")
        return DeleteResult(
            error=(f"Error deleting file '{requested.virtual}': {parsed.error.message}")
        )
    if parsed.operation != "delete":
        raise ValueError("rooted helper returned a non-delete result")
    return DeleteResult(path=requested.requested)


def _project_list_response(
    self: RootedOpenSandboxBackend,
    *,
    requested: _MappedPath,
    response: ExecuteResponse,
    request: _RootedCommand,
) -> LsResult:
    """Translate one validated helper envelope into ``LsResult``."""
    parsed = _parse_rooted_response(response, request=request)
    if parsed.status == "error":
        if parsed.error.code == "invalid_path":
            return LsResult(
                error=self._invalid_path_error(requested.requested),
                entries=None,
            )
        message = {
            "not_found": "path_not_found",
            "not_directory": "not_a_directory",
            "permission_denied": "permission_denied",
        }.get(parsed.error.code, parsed.error.message)
        return LsResult(
            error=f"Path '{requested.virtual}': {message}",
            entries=None,
        )
    if parsed.operation != "list":
        raise ValueError("rooted helper returned a non-list result")
    entries: list[FileInfo] = [
        {"path": entry["path"], "is_dir": entry["is_dir"]}
        for entry in parsed.result["entries"]
    ]
    return LsResult(
        error=self._restore_error(parsed.result["partial_error"]),
        entries=entries,
    )


def _project_glob_response(
    self: RootedOpenSandboxBackend,
    *,
    requested: _MappedPath,
    response: ExecuteResponse,
    request: _RootedCommand,
) -> GlobResult:
    """Translate one validated helper envelope into ``GlobResult``."""
    parsed = _parse_rooted_response(response, request=request)
    if parsed.status == "error":
        if parsed.error.code == "invalid_path":
            return GlobResult(
                error=self._invalid_path_error(requested.requested),
                matches=None,
            )
        message = {
            "not_found": "path_not_found",
            "not_directory": "not_a_directory",
            "permission_denied": "permission_denied",
        }.get(parsed.error.code, parsed.error.message)
        return GlobResult(
            error=f"Path '{requested.virtual}': {message}",
            matches=None,
        )
    if parsed.operation != "glob":
        raise ValueError("rooted helper returned a non-glob result")
    matches: list[FileInfo] = [
        {"path": match["path"], "is_dir": match["is_dir"]}
        for match in parsed.result["matches"]
    ]
    return GlobResult(
        error=self._restore_error(parsed.result["partial_error"]),
        matches=matches,
        truncated=parsed.result["truncated"],
    )


def _project_grep_response(
    self: RootedOpenSandboxBackend,
    *,
    requested: _MappedPath,
    response: ExecuteResponse,
    request: _RootedCommand,
) -> GrepResult:
    """Translate one validated helper envelope into ``GrepResult``."""
    parsed = _parse_rooted_response(response, request=request)
    if parsed.status == "error":
        if parsed.error.code == "invalid_path":
            return GrepResult(
                error=self._invalid_path_error(requested.requested),
                matches=None,
            )
        message = {
            "not_found": "path_not_found",
            "permission_denied": "permission_denied",
        }.get(parsed.error.code, parsed.error.message)
        return GrepResult(
            error=f"Path '{requested.virtual}': {message}",
            matches=None,
        )
    if parsed.operation != "grep":
        raise ValueError("rooted helper returned a non-grep result")
    matches: list[GrepMatch] = [
        {
            "path": match["path"],
            "line": match["line"],
            "text": match["text"],
        }
        for match in parsed.result["matches"]
    ]
    return GrepResult(
        error=self._restore_error(parsed.result["partial_error"]),
        matches=matches,
        truncated=parsed.result["truncated"],
    )


def _restore_error(self: RootedOpenSandboxBackend, error: str | None) -> str | None:
    """Replace physical workspace prefixes in errors with the virtual root."""
    if error is None:
        return None
    return self._root_error_pattern.sub("/", error)


def _is_safe_path_pattern(pattern: str) -> bool:
    """Return whether a glob pattern avoids traversal and NUL bytes."""
    return "\x00" not in pattern and ".." not in PurePosixPath(pattern).parts
