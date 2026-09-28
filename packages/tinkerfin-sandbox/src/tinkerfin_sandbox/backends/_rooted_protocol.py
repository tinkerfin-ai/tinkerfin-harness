"""Private protocol for descriptor-confined Rooted operations."""

from __future__ import annotations

__all__ = [
    "_build_rooted_transfer_command",
    "_parse_rooted_response",
    "_parse_rooted_transfer_handshake",
]

import base64
import json
import shlex
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Annotated, Literal, TypeAlias

from deepagents.backends.protocol import ExecuteResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from typing_extensions import TypedDict

_RootedOperation: TypeAlias = Literal[
    "probe",
    "read",
    "edit",
    "delete",
    "list",
    "glob",
    "grep",
    "transfer",
    "offload",
    "reset",
]
_JsonScalar: TypeAlias = str | int | float | bool | None
_JsonValue: TypeAlias = _JsonScalar | list["_JsonValue"] | dict[str, "_JsonValue"]


@dataclass(frozen=True, slots=True)
class _RootedCommand:
    """Carry one encoded helper command and its response correlation fields."""

    command: str
    request_id: str
    operation: _RootedOperation


@dataclass(frozen=True, slots=True)
class _RootedTransferCommand:
    """Carry one background descriptor-transfer command and correlation data."""

    command: str
    request_id: str
    token: str
    mode: Literal["upload", "download"]


class _RootedTransferHandshake(BaseModel):
    """Validated descriptor lease emitted by the background helper."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    token: str = Field(min_length=1, description="Transfer correlation secret.")
    mode: Literal["upload", "download"] = Field(
        description="Direction supported by the pinned descriptor."
    )
    pid: int = Field(gt=0, description="Sandbox helper process identifier.")
    fd: int = Field(gt=0, description="Pinned file descriptor number.")


class _RootedError(BaseModel):
    """Describe a confirmed helper failure without exposing physical paths."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: Literal[
        "invalid_path",
        "not_found",
        "permission_denied",
        "not_directory",
        "not_a_file",
        "not_text_file",
        "binary_too_large",
        "offset_exceeds_file_length",
        "string_not_found",
        "multiple_occurrences",
        "operation_failed",
    ] = Field(description="Stable machine-readable helper error code.")
    message: str = Field(
        description="Path-neutral diagnostic for the failed operation."
    )


class _RootedProbeResult(TypedDict):
    """Successful probe payload."""

    kind: Literal["file", "directory", "other"]


class _RootedReadResult(TypedDict):
    """Successful descriptor-backed read payload."""

    encoding: Literal["utf-8", "base64"]
    content: str
    total_lines: int | None
    start_line: int | None
    end_line: int | None
    next_offset: int | None
    no_lines_requested: bool


class _RootedEditResult(TypedDict):
    """Successful descriptor-backed edit payload."""

    count: int


class _RootedDeleteResult(TypedDict):
    """Successful descriptor-backed deletion payload."""

    deleted: bool


class _RootedListEntry(TypedDict):
    """One no-follow directory entry."""

    path: str
    is_dir: bool


class _RootedListResult(TypedDict):
    """Successful descriptor-backed listing payload."""

    entries: list[_RootedListEntry]
    partial_error: str | None


class _RootedGlobResult(TypedDict):
    """Successful descriptor-backed glob payload."""

    matches: list[_RootedListEntry]
    truncated: bool
    partial_error: str | None


class _RootedGrepMatch(TypedDict):
    """One literal text match."""

    path: str
    line: int
    text: str


class _RootedGrepResult(TypedDict):
    """Successful descriptor-backed grep payload."""

    matches: list[_RootedGrepMatch]
    truncated: bool
    partial_error: str | None


class _RootedOffloadResult(TypedDict):
    """Successful single-execution capture payload."""

    offloaded: bool
    output: str
    exit_code: int
    truncated: bool
    preview_has_truncation_marker: bool


class _RootedResetResult(TypedDict):
    """Successful descriptor-backed reset payload."""

    reset: bool


class _RootedResponseBase(BaseModel):
    """Fields shared by every validated helper response."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_id: str = Field(description="Opaque request correlation identifier.")


class _RootedProbeOkResponse(_RootedResponseBase):
    """Validated successful response from the in-Sandbox helper."""

    operation: Literal["probe"] = Field(description="Completed helper operation.")
    status: Literal["ok"] = Field(description="Successful terminal status.")
    error: None = Field(description="Absent error for a successful response.")
    result: _RootedProbeResult = Field(description="Probe result.")

    @model_validator(mode="after")
    def _validate_probe_result(self) -> _RootedProbeOkResponse:
        if set(self.result) != {"kind"}:
            raise ValueError("probe result must contain one valid kind")
        return self


class _RootedReadOkResponse(_RootedResponseBase):
    """Validated descriptor-backed read response."""

    operation: Literal["read"] = Field(description="Completed helper operation.")
    status: Literal["ok"] = Field(description="Successful terminal status.")
    error: None = Field(description="Absent error for a successful response.")
    result: _RootedReadResult = Field(description="Read content and pagination data.")

    @model_validator(mode="after")
    def _validate_read_result(self) -> _RootedReadOkResponse:
        expected = {
            "encoding",
            "content",
            "total_lines",
            "start_line",
            "end_line",
            "next_offset",
            "no_lines_requested",
        }
        if set(self.result) != expected:
            raise ValueError("read result fields are incomplete")
        return self


class _RootedEditOkResponse(_RootedResponseBase):
    """Validated descriptor-backed edit response."""

    operation: Literal["edit"] = Field(description="Completed helper operation.")
    status: Literal["ok"] = Field(description="Successful terminal status.")
    error: None = Field(description="Absent error for a successful response.")
    result: _RootedEditResult = Field(description="Edit replacement count.")

    @model_validator(mode="after")
    def _validate_edit_result(self) -> _RootedEditOkResponse:
        if set(self.result) != {"count"}:
            raise ValueError("edit result must contain one replacement count")
        return self


class _RootedDeleteOkResponse(_RootedResponseBase):
    """Validated descriptor-backed delete response."""

    operation: Literal["delete"] = Field(description="Completed helper operation.")
    status: Literal["ok"] = Field(description="Successful terminal status.")
    error: None = Field(description="Absent error for a successful response.")
    result: _RootedDeleteResult = Field(description="Deletion confirmation.")

    @model_validator(mode="after")
    def _validate_delete_result(self) -> _RootedDeleteOkResponse:
        if set(self.result) != {"deleted"} or self.result["deleted"] is not True:
            raise ValueError("delete result must confirm deletion")
        return self


class _RootedListOkResponse(_RootedResponseBase):
    """Validated descriptor-backed list response."""

    operation: Literal["list"] = Field(description="Completed helper operation.")
    status: Literal["ok"] = Field(description="Successful terminal status.")
    error: None = Field(description="Absent error for a successful response.")
    result: _RootedListResult = Field(description="No-follow directory entries.")

    @model_validator(mode="after")
    def _validate_list_result(self) -> _RootedListOkResponse:
        if set(self.result) != {"entries", "partial_error"} or any(
            set(entry) != {"path", "is_dir"} for entry in self.result["entries"]
        ):
            raise ValueError("list result contains malformed entries")
        return self


class _RootedGlobOkResponse(_RootedResponseBase):
    """Validated descriptor-backed glob response."""

    operation: Literal["glob"] = Field(description="Completed helper operation.")
    status: Literal["ok"] = Field(description="Successful terminal status.")
    error: None = Field(description="Absent error for a successful response.")
    result: _RootedGlobResult = Field(description="Sorted glob matches.")

    @model_validator(mode="after")
    def _validate_glob_result(self) -> _RootedGlobOkResponse:
        if set(self.result) != {"matches", "truncated", "partial_error"} or any(
            set(entry) != {"path", "is_dir"} for entry in self.result["matches"]
        ):
            raise ValueError("glob result contains malformed matches")
        return self


class _RootedGrepOkResponse(_RootedResponseBase):
    """Validated descriptor-backed grep response."""

    operation: Literal["grep"] = Field(description="Completed helper operation.")
    status: Literal["ok"] = Field(description="Successful terminal status.")
    error: None = Field(description="Absent error for a successful response.")
    result: _RootedGrepResult = Field(description="Literal text matches.")

    @model_validator(mode="after")
    def _validate_grep_result(self) -> _RootedGrepOkResponse:
        if set(self.result) != {"matches", "truncated", "partial_error"} or any(
            set(match) != {"path", "line", "text"} for match in self.result["matches"]
        ):
            raise ValueError("grep result contains malformed matches")
        return self


class _RootedOffloadOkResponse(_RootedResponseBase):
    """Validated single-execution offload response."""

    operation: Literal["offload"] = Field(description="Completed helper operation.")
    status: Literal["ok"] = Field(description="Successful terminal status.")
    error: None = Field(description="Absent error for a successful response.")
    result: _RootedOffloadResult = Field(description="Command capture outcome.")

    @model_validator(mode="after")
    def _validate_offload_result(self) -> _RootedOffloadOkResponse:
        if set(self.result) != {
            "offloaded",
            "output",
            "exit_code",
            "truncated",
            "preview_has_truncation_marker",
        }:
            raise ValueError("offload result fields are incomplete")
        if (
            not self.result["offloaded"]
            and self.result["preview_has_truncation_marker"]
        ):
            raise ValueError("inline output cannot carry a preview truncation marker")
        return self


class _RootedResetOkResponse(_RootedResponseBase):
    """Validated descriptor-backed reset response."""

    operation: Literal["reset"] = Field(description="Completed helper operation.")
    status: Literal["ok"] = Field(description="Successful terminal status.")
    error: None = Field(description="Absent error for a successful response.")
    result: _RootedResetResult = Field(description="Reset confirmation.")

    @model_validator(mode="after")
    def _validate_reset_result(self) -> _RootedResetOkResponse:
        if set(self.result) != {"reset"} or self.result["reset"] is not True:
            raise ValueError("reset result must confirm completion")
        return self


class _RootedErrorResponse(_RootedResponseBase):
    """Validated failed response from the in-Sandbox helper."""

    operation: _RootedOperation = Field(description="Attempted helper operation.")
    status: Literal["error"] = Field(description="Failed terminal status.")
    error: _RootedError = Field(description="Confirmed helper failure.")
    result: None = Field(description="Absent result for a failed response.")


_RootedOkResponse: TypeAlias = Annotated[
    _RootedProbeOkResponse
    | _RootedReadOkResponse
    | _RootedEditOkResponse
    | _RootedDeleteOkResponse
    | _RootedListOkResponse
    | _RootedGlobOkResponse
    | _RootedGrepOkResponse
    | _RootedOffloadOkResponse
    | _RootedResetOkResponse,
    Field(discriminator="operation"),
]
_RootedResponse: TypeAlias = Annotated[
    _RootedOkResponse | _RootedErrorResponse,
    Field(discriminator="status"),
]
_ROOTED_RESPONSE_ADAPTER: TypeAdapter[_RootedResponse] = TypeAdapter(_RootedResponse)

_ROOTED_HELPER_SCRIPT = (
    files("tinkerfin_sandbox.backends")
    .joinpath("_rooted_helper.py.txt")
    .read_bytes()
    .decode("utf-8")
    .strip()
)


def _build_rooted_command(
    *,
    root: str,
    operation: _RootedOperation,
    arguments: Mapping[str, _JsonValue],
) -> _RootedCommand:
    """Build one isolated helper command without interpolating caller values."""
    if operation not in {
        "probe",
        "read",
        "edit",
        "delete",
        "list",
        "glob",
        "grep",
        "offload",
        "reset",
        "transfer",
    }:
        raise ValueError(f"unsupported rooted operation: {operation}")
    request_id = uuid.uuid4().hex
    payload = base64.b64encode(
        json.dumps(
            {
                "request_id": request_id,
                "operation": operation,
                "root": root,
                "arguments": dict(arguments),
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    command = (
        f"python3 -I -S -c {shlex.quote(_ROOTED_HELPER_SCRIPT)} {shlex.quote(payload)}"
    )
    return _RootedCommand(
        command=command,
        request_id=request_id,
        operation=operation,
    )


def _build_rooted_transfer_command(
    *,
    root: str,
    path: str,
    mode: Literal["upload", "download"],
    token: str,
    hold_seconds: int,
) -> _RootedTransferCommand:
    """Build one finite background descriptor lease command."""
    request = _build_rooted_command(
        root=root,
        operation="transfer",
        arguments={
            "path": path,
            "mode": mode,
            "token": token,
            "hold_seconds": hold_seconds,
        },
    )
    return _RootedTransferCommand(
        command=request.command,
        request_id=request.request_id,
        token=token,
        mode=mode,
    )


def _parse_rooted_transfer_handshake(
    content: str,
    *,
    request: _RootedTransferCommand,
) -> _RootedTransferHandshake | _RootedError | None:
    """Parse exactly one complete token-matched descriptor handshake."""
    if not content.endswith("\n"):
        return None
    records = [line for line in content.splitlines() if line]
    if not records:
        return None
    if len(records) != 1:
        raise ValueError("rooted transfer handshake count mismatch")
    try:
        handshake = _RootedTransferHandshake.model_validate_json(records[0])
    except ValueError:
        try:
            rejected = _RootedErrorResponse.model_validate_json(records[0])
        except ValueError as exc:
            raise ValueError("rooted transfer handshake malformed") from exc
        if rejected.request_id != request.request_id:
            raise ValueError("rooted transfer rejection request mismatch")
        if rejected.operation != "transfer":
            raise ValueError("rooted transfer rejection operation mismatch")
        return rejected.error
    else:
        if handshake.token != request.token:
            raise ValueError("rooted transfer handshake token mismatch")
        if handshake.mode != request.mode:
            raise ValueError("rooted transfer handshake mode mismatch")
        return handshake


def _parse_rooted_response(
    response: ExecuteResponse,
    *,
    request: _RootedCommand,
) -> _RootedResponse:
    """Validate one complete helper response and its request correlation fields."""
    if response.exit_code != 0:
        raise ValueError("rooted helper command failed")
    if response.truncated:
        raise ValueError("rooted helper response was truncated")
    try:
        parsed = _ROOTED_RESPONSE_ADAPTER.validate_json(response.output)
    except ValueError as exc:
        raise ValueError("rooted helper response malformed") from exc
    if parsed.request_id != request.request_id:
        raise ValueError("rooted helper response request mismatch")
    if parsed.operation != request.operation:
        raise ValueError("rooted helper response operation mismatch")
    return parsed
