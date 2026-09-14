"""Messaging retention policy validation."""

from __future__ import annotations

import pytest

from tinkerfin_messaging import (
    MessagingRetentionPolicy,
)


def test_retention_policy_is_explicit_and_immutable() -> None:
    disabled = MessagingRetentionPolicy.disabled()
    enabled = MessagingRetentionPolicy.expire_after(30)

    assert disabled.enabled is False
    assert disabled.terminal_ttl_seconds is None
    assert enabled.enabled is True
    assert enabled.terminal_ttl_seconds == 30.0
    with pytest.raises((TypeError, ValueError)):
        MessagingRetentionPolicy.expire_after(0)
    with pytest.raises((TypeError, ValueError)):
        MessagingRetentionPolicy.expire_after(float("nan"))
