"""Stable lifecycle failures for Gateway-owned client streams."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_ContextValue: TypeAlias = str | int | float | bool | None


class GatewayErrorCode(StrEnum):
    """Stable categories that never expose transport credentials or payloads."""

    ERROR = "gateway.error"
    CLOSED = "gateway.closed"


class GatewayError(Exception):
    """Separate client-safe failure information from the original trusted cause."""

    code = GatewayErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Copy contexts and retain the original exception without exposing it."""
        self.message = message
        self.context = MappingProxyType(dict(context or {}))
        self.diagnostic_context = MappingProxyType(dict(diagnostic_context or {}))
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause
        super().__init__(message)


class GatewayClosed(GatewayError):
    """The Gateway-owned stream or response has already been used or closed."""

    code = GatewayErrorCode.CLOSED


__all__ = ["GatewayClosed", "GatewayError", "GatewayErrorCode"]
