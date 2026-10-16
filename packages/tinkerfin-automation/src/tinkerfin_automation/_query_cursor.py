"""Keep pagination positions independent of deletions and bound to their query."""

import base64
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .queries import ExecutionFilter, TaskFilter


class _Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str
    at: datetime
    identity: str = Field(min_length=1, max_length=191)

    @field_validator("at")
    @classmethod
    def aware_position(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Cursor time must be aware")
        return value.astimezone(UTC)


def query_scope(
    namespace: str,
    owner_id: str,
    filters: TaskFilter | ExecutionFilter,
    task_id: str | None = None,
) -> str:
    raw = json.dumps(
        [namespace, owner_id, type(filters).__name__, asdict(filters), task_id],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def encode_cursor(scope: str, at: datetime, identity: str) -> str:
    raw = _Position(scope=scope, at=at, identity=identity).model_dump_json().encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str | None, scope: str) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    if len(cursor) > 2048:
        raise ValueError("Invalid pagination cursor")
    try:
        position = _Position.model_validate_json(
            base64.b64decode(cursor, altchars=b"-_", validate=True)
        )
    except (ValueError, ValidationError) as error:
        raise ValueError("Invalid pagination cursor") from error
    if position.scope != scope:
        raise ValueError("Pagination cursor belongs to another query")
    return position.at, position.identity
