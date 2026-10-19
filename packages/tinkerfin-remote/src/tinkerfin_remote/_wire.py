"""Canonical bounded messages and immutable receipt correlation."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .errors import RemoteError, RemoteErrorCode
from .models import (
    BinaryChunk,
    CommandEnvelope,
    OperationEvent,
    Receipt,
    WorkspaceEvent,
)

BoundaryT = TypeVar("BoundaryT", bound=BaseModel)


def encode(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value: BaseModel) -> str:
    return digest_bytes(encode(value).encode())


def key(*values: object) -> str:
    return digest_bytes(
        json.dumps(
            values,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    )


def item_key(value: BaseModel) -> str:
    if isinstance(value, CommandEnvelope):
        return value.command_id
    if isinstance(value, WorkspaceEvent):
        return value.command_id
    if isinstance(value, OperationEvent):
        return f"{value.operation_id}:{value.sequence}"
    if isinstance(value, BinaryChunk):
        return f"{value.operation_id}:{value.channel}:{value.file_index}:{value.offset}"
    raise TypeError("unsupported remote receipt item")


def receipt(value: BaseModel) -> Receipt:
    return Receipt(key=item_key(value), digest=digest(value))


def bounded(value: BaseModel, limit: int) -> str:
    payload = encode(value)
    if len(payload.encode()) > limit:
        raise RemoteError(
            RemoteErrorCode.CAPACITY, "Remote message exceeds its byte limit"
        )
    return payload


def decode(kind: type[BoundaryT], payload: str, limit: int) -> BoundaryT:
    if len(payload.encode()) > limit:
        raise RemoteError(
            RemoteErrorCode.CAPACITY, "Remote frame exceeds its byte limit"
        )
    try:
        return kind.model_validate_json(payload)
    except ValidationError as error:
        raise RemoteError(
            RemoteErrorCode.PROTOCOL, "Remote frame is invalid", cause=error
        ) from error


def chunk_bytes(chunk: BinaryChunk, limit: int) -> bytes:
    if len(chunk.data) > ((limit + 2) // 3) * 4:
        raise RemoteError(
            RemoteErrorCode.CAPACITY, "Binary chunk exceeds its byte limit"
        )
    try:
        content = base64.b64decode(chunk.data, validate=True)
    except ValueError as error:
        raise RemoteError(
            RemoteErrorCode.PROTOCOL, "Binary chunk encoding is invalid", cause=error
        ) from error
    if len(content) > limit or digest_bytes(content) != chunk.digest:
        raise RemoteError(
            RemoteErrorCode.PROTOCOL, "Binary chunk content conflicts with its digest"
        )
    return content
