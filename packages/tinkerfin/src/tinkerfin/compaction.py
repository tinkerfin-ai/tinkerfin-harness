"""Results of explicitly requested conversation context compression."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CompactionResult(BaseModel, frozen=True):
    """A saved compression result, also used at stream and history boundaries.

    ``summary`` contains the actual model-generated summary only after it has
    replaced older effective context. Original conversation messages remain intact.
    ``nothing_to_compact`` skips generation. ``not_reduced`` means the generated
    summary was not shorter and the effective context was left unchanged.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    status: Literal["compacted", "nothing_to_compact", "not_reduced"]
    summary: str | None = None
    compacted_messages: int = Field(
        default=0,
        ge=0,
        description="Number of stored messages newly replaced in effective context.",
    )
