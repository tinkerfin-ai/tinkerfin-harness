"""Stable failures for the current native stream boundary."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_ContextValue: TypeAlias = str | int | float | bool | None


class NativeErrorCode(StrEnum):
    """Stable machine-readable native stream failure categories."""

    ERROR = "native.error"
    STREAM_CONTRACT = "native.stream_contract"


class NativeError(Exception):
    """Base native boundary failure with public and diagnostic context."""

    code: NativeErrorCode = NativeErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Snapshot client-safe context and retain trusted causal diagnostics."""

        self.message = message
        self.context: Mapping[str, _ContextValue] = MappingProxyType(
            dict(context or {})
        )
        self.diagnostic_context: Mapping[str, _ContextValue] = MappingProxyType(
            dict(diagnostic_context or {})
        )
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause
        super().__init__(message)


class NativeStreamContractError(NativeError, ValueError):
    """A live upstream stream part violates the current native contract."""

    code = NativeErrorCode.STREAM_CONTRACT


__all__ = ["NativeError", "NativeErrorCode", "NativeStreamContractError"]
