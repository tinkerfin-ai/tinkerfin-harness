"""Persistent, asynchronous LangGraph memory stores."""

from typing import TYPE_CHECKING

from .errors import LangGraphStoreError as LangGraphStoreError
from .errors import StoreClosedError as StoreClosedError
from .errors import StoreCorruptionError as StoreCorruptionError
from .errors import StoreDriverError as StoreDriverError
from .errors import StoreSchemaError as StoreSchemaError

if TYPE_CHECKING:
    from .sqlalchemy import SqlAlchemyStore as SqlAlchemyStore

__all__ = [
    "LangGraphStoreError",
    "SqlAlchemyStore",
    "StoreClosedError",
    "StoreCorruptionError",
    "StoreDriverError",
    "StoreSchemaError",
]


def __getattr__(name: str) -> object:
    """Load a requested database integration without making it a core dependency."""

    if name == "SqlAlchemyStore":
        try:
            from .sqlalchemy import SqlAlchemyStore
        except ModuleNotFoundError as error:
            if error.name not in {
                "sqlalchemy",
                "tinkerfin_sqlalchemy",
                "tinkerfin_contracts",
            }:
                raise
            raise ImportError(
                'Install "tinkerfin-langgraph-store[sqlalchemy]" to use SqlAlchemyStore'
            ) from error
        globals()[name] = SqlAlchemyStore
        return SqlAlchemyStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
