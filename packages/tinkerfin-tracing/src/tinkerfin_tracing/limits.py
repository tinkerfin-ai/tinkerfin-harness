"""Immutable capacity limits for semantic Trace storage."""

from __future__ import annotations

from pydantic import Field, model_validator

from ._models import TraceModel


class TraceLimits(TraceModel, frozen=True):
    """Bound each namespace's events and reserved terminal capacity before writes."""

    max_event_bytes: int = Field(default=1024 * 1024, ge=1024)
    max_thread_events: int = Field(default=100_000, ge=16)
    max_thread_bytes: int = Field(default=256 * 1024 * 1024, ge=64 * 1024)
    max_tracer_threads: int = Field(
        default=10_000, ge=1, description="Maximum retained threads in each namespace"
    )
    max_tracer_bytes: int = Field(
        default=1024 * 1024 * 1024,
        ge=64 * 1024,
        description="Maximum canonical event bytes and terminal reserves in each namespace",
    )
    terminal_reserve_events_per_run: int = Field(default=4, ge=2)
    terminal_reserve_bytes_per_run: int = Field(default=4 * 1024 * 1024, ge=1024)
    follow_batch_size: int = Field(default=256, ge=1, le=10_000)

    @model_validator(mode="after")
    def aggregate_limits_are_consistent(self) -> TraceLimits:
        """Reserve terminal capacity inside both thread and Tracer budgets."""

        if self.max_thread_bytes > self.max_tracer_bytes:
            raise ValueError("max_thread_bytes must not exceed max_tracer_bytes")
        if self.terminal_reserve_bytes_per_run >= self.max_thread_bytes:
            raise ValueError("terminal reserve must be smaller than a thread budget")
        if self.terminal_reserve_events_per_run >= self.max_thread_events:
            raise ValueError(
                "terminal event reserve must be smaller than a thread budget"
            )
        required_terminal_bytes = (
            self.terminal_reserve_events_per_run * self.max_event_bytes
        )
        if self.terminal_reserve_bytes_per_run < required_terminal_bytes:
            raise ValueError(
                "terminal byte reserve must cover every reserved max-size event"
            )
        return self


__all__ = ["TraceLimits"]
