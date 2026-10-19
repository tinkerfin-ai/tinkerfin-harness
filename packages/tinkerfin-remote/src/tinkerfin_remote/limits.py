"""Explicit bounds for requests, durable output, and binary transfer."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RemoteLimits:
    """Bound one authenticated endpoint without evicting active evidence.

    Frames, requests and progress use encoded JSON byte bounds. Files use their
    raw byte size. Operations counts admitted work without a terminal outcome.
    Closed, acknowledged sessions release bulk payloads; small results remain
    replayable up to replay_result_bytes. Retained bytes still count compact
    identities and receipt digests, so arbitrary history is not unbounded.
    """

    frame_bytes: int = 1024 * 1024
    chunk_bytes: int = 192 * 1024
    file_bytes: int = 64 * 1024 * 1024
    output_bytes: int = 4 * 1024 * 1024
    replay_result_bytes: int = 16 * 1024
    request_bytes: int = 512 * 1024
    retained_bytes: int = 512 * 1024 * 1024
    operations: int = 8192
    concurrent_operations: int = 16
    sessions: int = 64

    def __post_init__(self) -> None:
        """Reject invalid capacities before any resource opens."""
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.chunk_bytes * 2 > self.frame_bytes:
            raise ValueError("chunk size leaves insufficient framing capacity")
        if self.request_bytes > self.frame_bytes // 2:
            raise ValueError("request size leaves insufficient receipt capacity")


__all__ = ["RemoteLimits"]
