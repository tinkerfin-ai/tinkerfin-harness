"""Messaging-specific validation for the shared framework run identity."""

from __future__ import annotations

from tinkerfin_contracts import RunIdentity

from .errors import MessagingBackendProtocolError


def validate_request_digest(value: str | None) -> str | None:
    """Validate an optional canonical SHA-256 command binding."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("request_digest must be a string or None")
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("request_digest must be a lowercase SHA-256 digest")
    return value


def stored_request_digest(value: object) -> str | None:
    """Reject corrupted durable bindings without interpreting missing formats."""
    if value is not None and not isinstance(value, str):
        raise MessagingBackendProtocolError(
            "Stored request binding has an invalid type"
        )
    try:
        return validate_request_digest(value)
    except ValueError as error:
        raise MessagingBackendProtocolError(
            "Stored request binding is invalid", cause=error
        ) from error


def thread_key(identity: RunIdentity) -> str:
    """Encode a complete thread identity for internal storage and cleanup."""

    return identity.thread.model_dump_json(by_alias=True)


def required_canonical_text(name: str, value: str) -> str:
    """Validate non-blank text without changing its persisted identity."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-blank without surrounding whitespace")
    return value


def required_identifier(name: str, value: str) -> str:
    """Validate one bounded public identifier without normalizing its identity."""

    required_canonical_text(name, value)
    if len(value) > 1024:
        raise ValueError(f"{name} must contain at most 1024 characters")
    return value


def required_identity(identity: RunIdentity) -> RunIdentity:
    """Validate the shared RunIdentity and Messaging's bounded key constraints."""

    if not isinstance(identity, RunIdentity):
        raise TypeError("identity must be a RunIdentity")
    required_identifier("identity.thread_id", identity.thread_id)
    required_identifier("identity.run_id", identity.run_id)
    return identity
