"""Public result of reading one retained model input."""

from __future__ import annotations

from pydantic import Field, JsonValue

from ._models import TraceModel


class TraceModelRequest(TraceModel, frozen=True):
    """One complete retained model request, without neighboring calls' inputs."""

    node_id: str = Field(min_length=1)
    request: JsonValue | None
    request_omitted: bool


__all__ = ["TraceModelRequest"]
