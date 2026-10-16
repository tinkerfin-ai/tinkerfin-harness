"""Canonical namespace, thread, and run identities shared by integrations."""

from __future__ import annotations

from pydantic import Field, field_validator

from ._models import ContractModel


class ThreadIdentity(ContractModel):
    """Identify a durable conversation within an opaque isolation namespace.

    Namespace and thread identifiers are immutable, case-sensitive UTF-8 strings.
    The host chooses namespace ownership; identifiers do not grant authorization.
    """

    namespace: str = Field(
        min_length=1,
        max_length=128,
        description="Opaque logical isolation namespace chosen by the host",
    )

    thread_id: str = Field(
        min_length=1,
        max_length=1024,
        description="Stable thread identity shared by runtime and durable systems",
    )

    @field_validator("namespace", "thread_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str) -> str:
        """Reject surrounding whitespace before the identity causes side effects."""

        return _canonical_identifier(value)


class RunIdentity(ThreadIdentity):
    """Identify one semantic run in a namespaced conversation.

    JSON retains flat namespace, threadId, and runId fields. The derived thread
    property is not a second serialized identity and cannot diverge from this run.
    """

    run_id: str = Field(
        min_length=1,
        max_length=1024,
        description="Idempotent identity for one semantic run within the thread",
    )

    @field_validator("run_id")
    @classmethod
    def run_identifier_is_canonical(cls, value: str) -> str:
        """Reject values that cannot safely identify a persistent run."""

        return _canonical_identifier(value)

    @property
    def thread(self) -> ThreadIdentity:
        """Return the namespace and thread identity without the run identifier."""

        return ThreadIdentity(namespace=self.namespace, thread_id=self.thread_id)


def _canonical_identifier(value: str) -> str:
    if value != value.strip():
        raise ValueError("identity values must not contain surrounding whitespace")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("identity values must be encodable as UTF-8") from error
    return value


def validate_namespace(value: str) -> str:
    """Validate an opaque namespace without changing its business meaning.

    Args:
        value: Host-selected namespace, containing at most 128 Unicode characters.

    Returns:
        The unchanged namespace.

    Raises:
        TypeError: The value is not a string.
        ValueError: The namespace is empty, too long, padded, or not UTF-8 encodable.
    """

    if not isinstance(value, str):
        raise TypeError("namespace must be a string")
    if not value or len(value) > 128:
        raise ValueError("namespace must contain between 1 and 128 characters")
    return _canonical_identifier(value)


__all__ = ["RunIdentity", "ThreadIdentity", "validate_namespace"]
