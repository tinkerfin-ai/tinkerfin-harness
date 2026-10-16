"""Finite replay representation produced by a concrete Native Driver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from tinkerfin_contracts import GraphOrigin, SubagentRequestReference

from .json import to_json_value
from .stream import NativeStreamMode

if TYPE_CHECKING:
    from .frame import NativeStreamFrame


class NativeStreamPart(BaseModel):
    """Detached replay and direct-SSE representation of one Native frame.

    A concrete Runtime Profile must create this model while it still owns the live
    upstream object. Persistence and transport consumers receive the already finite
    model and therefore never need to know which third-party stream shape produced it.
    """

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    mode: NativeStreamMode = Field(alias="type")
    graph_namespace: tuple[str, ...] = Field(alias="ns")
    data: JsonValue
    interrupts: tuple[JsonValue, ...] = ()
    graph_origin: GraphOrigin = Field(default_factory=GraphOrigin, alias="graphOrigin")
    subagent_requests: tuple[SubagentRequestReference, ...] = Field(
        default=(), alias="subagentRequests"
    )

    def to_frame(self) -> NativeStreamFrame:
        """Restore the strict public frame consumed by Native protocol adapters.

        The finite record preserves public message, task, state, and interrupt
        semantics. Provider-private metadata is never reconstructed. Origins are
        validated against task evidence by the consuming stream registry.

        Raises:
            NativeStreamContractError: A record has an invalid mode-specific payload.
        """

        from ._replay import replay_frame

        return replay_frame(self)

    @field_validator("data")
    @classmethod
    def _data_is_finite(cls, value: JsonValue) -> JsonValue:
        """Reject non-finite numbers that JSON parsers may otherwise accept."""

        return to_json_value(value)

    @field_validator("interrupts")
    @classmethod
    def _interrupts_are_finite(
        cls,
        value: tuple[JsonValue, ...],
    ) -> tuple[JsonValue, ...]:
        """Keep every persisted interrupt inside the same finite JSON boundary."""

        return tuple(to_json_value(item) for item in value)


__all__ = ["NativeStreamPart"]
