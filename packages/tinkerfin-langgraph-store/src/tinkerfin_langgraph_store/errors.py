"""Public failures for persistent LangGraph memory."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


class LangGraphStoreError(Exception):
    """Base Store failure with a safe message and a separate diagnostic cause."""

    code = "langgraph_store.error"

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, str | int | bool | None] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Copy public context; keep driver details out of the public message."""

        self.message = message
        self.context = MappingProxyType(dict(context or {}))
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause
        super().__init__(message)


class StoreDriverError(LangGraphStoreError):
    """A database operation failed; its cause is for trusted diagnostics only."""

    code = "langgraph_store.driver_failure"


class StoreSchemaError(LangGraphStoreError):
    """Existing Store storage does not have the required complete structure."""

    code = "langgraph_store.schema_mismatch"


class StoreClosedError(LangGraphStoreError):
    """The Store has stopped accepting operations."""

    code = "langgraph_store.closed"


class StoreCorruptionError(LangGraphStoreError):
    """Stored identity or document evidence does not match its indexed key."""

    code = "langgraph_store.corruption"
