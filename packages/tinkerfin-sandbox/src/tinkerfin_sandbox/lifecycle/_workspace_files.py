"""Own bounded reads without creating a sandbox, project, or execution session."""

from __future__ import annotations

__all__ = ["QueryResult", "query_files", "validate_path"]

import asyncio
import base64
import json
import shlex
from datetime import datetime
from importlib.resources import files
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

from ..backends._isolated import _workspace_call
from ..errors import (
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBusyError,
    OpenSandboxFileChangedError,
    OpenSandboxNotTextError,
    OpenSandboxWorkspaceNotInitializedError,
)
from ..workspace_files import WorkspaceFileInfo
from ._purpose import require_binding_purpose
from ._sql_tasks import capture, run_owned_operation, select_failure

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

_KeyT = TypeVar("_KeyT")
_HELPER = base64.b64encode(
    files("tinkerfin_sandbox.backends")
    .joinpath("_workspace_files_helper.py.txt")
    .read_bytes()
).decode("ascii")
_PROGRAM = f"import base64;exec(compile(base64.b64decode({_HELPER!r}),'<workspace-files>','exec'))"
_RESPONSE_LIMIT = 8 * 1024 * 1024
_QUERY_TIMEOUT = 15


class _FileInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    name: str
    kind: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None = Field(ge=0)
    modified_at: datetime
    etag: str = Field(pattern="^[0-9a-f]{64}$")

    def public(self) -> WorkspaceFileInfo:
        return WorkspaceFileInfo(
            self.path,
            self.name,
            self.kind,
            self.size_bytes,
            self.modified_at,
            self.etag,
        )


class _Directory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["directory"]
    path: str
    entries: list[_FileInfo] = Field(max_length=200)
    next_cursor: str | None = Field(max_length=8192)


class _Info(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["info"]
    file: _FileInfo


class _Text(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["text"]
    file: _FileInfo
    text: str = Field(max_length=1024 * 1024)
    truncated: bool


class _Error(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["error"]
    code: Literal[
        "invalid_path",
        "invalid_limits",
        "invalid_cursor",
        "invalid_operation",
        "changed",
        "not_text",
        "not_initialized",
        "busy",
        "not_found",
        "permission_denied",
        "not_directory",
        "unavailable",
    ]


_Reply = Annotated[_Directory | _Info | _Text | _Error, Field(discriminator="kind")]
_REPLY: TypeAdapter[_Reply] = TypeAdapter(_Reply)
QueryResult = _Directory | _Info | _Text


def validate_path(path: str) -> str:
    """Reject escapes before remote I/O and return the canonical virtual path."""
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\x00" in path
        or ".." in PurePosixPath(path).parts
        or len(path.encode("utf-8")) > 4096
    ):
        raise ValueError("path must be an absolute project path without traversal")
    return str(PurePosixPath(path))


def _parse(output: str, *, expected: str, path: str) -> _Directory | _Info | _Text:
    if len(output.encode("utf-8")) > _RESPONSE_LIMIT:
        raise OpenSandboxBackendProtocolError(
            "Workspace query exceeded its response limit"
        )
    try:
        reply = _REPLY.validate_json(output)
    except ValidationError as error:
        raise OpenSandboxBackendProtocolError(
            "Workspace query returned invalid data", cause=error
        ) from error
    if isinstance(reply, _Error):
        if reply.code == "not_initialized":
            raise OpenSandboxWorkspaceNotInitializedError(
                "The project has no existing workspace"
            )
        if reply.code == "changed":
            raise OpenSandboxFileChangedError(
                "The selected workspace entry changed; read it again"
            )
        if reply.code == "not_text":
            raise OpenSandboxNotTextError(
                "The selected entry is not a regular UTF-8 text file"
            )
        if reply.code == "not_found":
            raise FileNotFoundError(path)
        if reply.code == "permission_denied":
            raise PermissionError(path)
        if reply.code == "not_directory":
            raise NotADirectoryError(path)
        if reply.code.startswith("invalid_"):
            raise ValueError("Workspace query arguments are invalid")
        if reply.code == "busy":
            raise OpenSandboxBusyError("Workspace deletion is in progress")
        raise OpenSandboxBackendUnavailableError("Workspace files are unavailable")
    if reply.kind != expected:
        raise OpenSandboxBackendProtocolError(
            "Workspace query returned the wrong result"
        )
    if isinstance(reply, _Directory):
        valid = reply.path == path and all(
            entry.path == path.rstrip("/") + "/" + entry.name
            and entry.name not in {"", ".", ".."}
            and "/" not in entry.name
            for entry in reply.entries
        )
    else:
        valid = reply.file.path == path
    if not valid:
        raise OpenSandboxBackendProtocolError(
            "Workspace query returned an unrelated path"
        )
    return reply


async def query_files(
    manager: OpenSandboxManager[_KeyT],
    owner_key: str,
    project: str,
    *,
    operation: Literal["list", "stat", "text"],
    path: str,
    arguments: dict[str, JsonValue],
) -> _Directory | _Info | _Text:
    """Keep observation pinned and settle its client before releasing admission."""
    path = validate_path(path)
    request = {"project": project, "operation": operation, "path": path, **arguments}
    command = (
        "/opt/sandbox-runtime/venv/bin/python -I -S -c "
        + shlex.quote(_PROGRAM)
        + " "
        + shlex.quote(json.dumps(request, ensure_ascii=True, separators=(",", ":")))
    )
    expected = {"list": "directory", "stat": "info", "text": "text"}[operation]
    try:
        async with (
            asyncio.timeout(_QUERY_TIMEOUT),
            manager._operation(),
            manager._workspace_read_slots,
        ):
            async with manager._claim_owner(owner_key) as claim:
                binding = claim.binding
                require_binding_purpose(binding, "workspaces")
                if binding is None:
                    raise OpenSandboxWorkspaceNotInitializedError(
                        "The owner has no existing sandbox"
                    )
                availability = await manager._availability.require_running(owner_key)
            # The observer never initializes or reconnects a business handle. No
            # owner lock or execution lease survives across this remote query.
            parent = await _workspace_call(
                manager._client._connect_observer(binding.sandbox_id)
            )
            primary: BaseException | None = None
            try:
                with parent._rooted_file_operation():
                    response = await _workspace_call(
                        parent.aexecute(command, timeout=_QUERY_TIMEOUT)
                    )
                if response.exit_code != 0 or response.truncated:
                    raise OpenSandboxBackendProtocolError(
                        "Workspace reader did not return a complete result"
                    )
                if (
                    await manager._state.read_binding(owner_key) != binding
                    or await manager._availability.require_running(owner_key)
                    != availability
                ):
                    raise OpenSandboxFileChangedError(
                        "Workspace availability changed during the query"
                    )
                return _parse(response.output, expected=expected, path=path)
            except BaseException as error:
                primary = error
                raise
            finally:
                outcome = await capture(
                    run_owned_operation(
                        _workspace_call(parent.aclose()),
                        task_name="tinkerfin-workspace-reader-close",
                    )
                )
                if isinstance(outcome, BaseException):
                    raise (
                        outcome if primary is None else select_failure(primary, outcome)
                    )
    except TimeoutError as error:
        raise OpenSandboxBackendTimeoutError(
            "Workspace query timed out", cause=error
        ) from error
