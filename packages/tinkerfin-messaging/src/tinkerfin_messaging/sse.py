"""Pure parsing helpers for durable SSE sequence cursors."""

from __future__ import annotations


def parse_sse_event_id(value: str | None) -> int | None:
    """Parse one canonical decimal SSE event ID into a durable sequence.

    Args:
        value: ``None`` or an ASCII decimal sequence with no sign, whitespace, or
            leading zeroes. The canonical zero value is ``"0"``.

    Returns:
        The non-negative durable sequence, or ``None`` when no cursor was supplied.

    Raises:
        TypeError: ``value`` is neither text nor ``None``.
        ValueError: Text is empty or not in canonical ASCII decimal form.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("SSE event ID must be a string or None")
    if (
        not value
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise ValueError("SSE event ID must be canonical ASCII decimal text")
    return int(value)
