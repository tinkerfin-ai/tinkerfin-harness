"""Immutable capacity limits for built-in durable Messaging backends."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MessagingLimits:
    """Bound individual commits, thread payloads, and total retained storage.

    Limits apply to encoded payload and checkpoint bytes before backend mutation.
    Thread totals count committed payload bytes and messages in the current generation;
    `delete_stream()` starts a new generation with empty thread counters. Total limits
    cover one MemoryBackend instance, SQL database, or Redis key_prefix across all
    channels. Bytes
    count payloads and retained checkpoint positions and message identifiers, including
    both per-message evidence and the latest checkpoint of each Run. Records count
    channels, threads, live generations, Runs, messages, and generation tombstones.
    These are logical storage limits, not a bound on Python or Redis allocator usage.
    """

    max_message_payload_bytes: int = 16 * 1024 * 1024
    max_checkpoint_bytes: int = 1024 * 1024
    max_thread_messages: int = 100_000
    max_thread_payload_bytes: int = 1024 * 1024 * 1024

    max_total_bytes: int = 1024 * 1024 * 1024
    max_total_records: int = 100_000

    def __post_init__(self) -> None:
        """Validate strict positive capacities and their aggregate relationship."""

        for name in (
            "max_message_payload_bytes",
            "max_checkpoint_bytes",
            "max_thread_messages",
            "max_thread_payload_bytes",
            "max_total_bytes",
            "max_total_records",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.max_thread_payload_bytes < self.max_message_payload_bytes:
            raise ValueError(
                "max_thread_payload_bytes must not be smaller than "
                "max_message_payload_bytes"
            )


DEFAULT_MESSAGING_LIMITS = MessagingLimits()


__all__ = ["DEFAULT_MESSAGING_LIMITS", "MessagingLimits"]
