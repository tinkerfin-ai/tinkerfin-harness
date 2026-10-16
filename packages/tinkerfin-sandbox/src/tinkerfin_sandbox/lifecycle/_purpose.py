"""Require explicit, consistent capability ownership before Sandbox access."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..errors import OpenSandboxPurposeError
from ..models import OpenSandboxPurpose

if TYPE_CHECKING:
    from .state import OpenSandboxBinding

PURPOSE_METADATA_KEY = "tinkerfin.ai/purpose"


def validate_purpose(value: str | None) -> OpenSandboxPurpose:
    """Reject an unrecognized purpose instead of inferring command access."""
    if value == "commands":
        return "commands"
    if value == "workspaces":
        return "workspaces"
    raise OpenSandboxPurposeError("Sandbox purpose is missing or invalid")


def require_purpose(actual: str | None, expected: OpenSandboxPurpose) -> None:
    """Keep capability selection independent of remote health or availability."""
    validate_purpose(expected)
    observed = validate_purpose(actual)
    if observed != expected:
        raise OpenSandboxPurposeError(
            "Sandbox is bound to a different purpose",
            context={"expected_purpose": expected, "actual_purpose": observed},
        )


def require_remote_purpose(
    metadata: Mapping[str, str] | None, expected: OpenSandboxPurpose
) -> None:
    """Require the provider's reserved metadata to confirm committed ownership."""
    require_purpose(
        None if metadata is None else metadata.get(PURPOSE_METADATA_KEY), expected
    )


def require_binding_purpose(
    binding: OpenSandboxBinding | None, expected: OpenSandboxPurpose
) -> None:
    """Check durable ownership before creation, recovery, or lifecycle effects."""
    validate_purpose(expected)
    if binding is not None:
        require_purpose(binding.purpose, expected)
