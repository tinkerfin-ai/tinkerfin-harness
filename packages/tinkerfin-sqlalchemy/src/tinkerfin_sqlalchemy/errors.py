"""Failures owned by the shared SQL transaction boundary."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from sqlalchemy.exc import SQLAlchemyError


class SqlTransactionError(SQLAlchemyError):
    """A transaction cannot establish or release its required database guarantees.

    SQLAlchemyError inheritance lets a storage integration translate this failure
    together with driver failures. Context is safe for callers; cause is diagnostic
    evidence and must not be serialized into client responses.
    """

    code = "sqlalchemy.transaction"

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, str | int | bool | None] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Snapshot safe context and retain the original diagnostic failure."""

        self.message = message
        self.context = MappingProxyType(dict(context or {}))
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause
        super().__init__(message)

    def __str__(self) -> str:
        """Return the safe message without interpreting the code as a SQLAlchemy URL."""

        return self.message
