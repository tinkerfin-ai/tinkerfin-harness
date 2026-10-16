"""Conditional document writes shared by storage providers and file consumers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeAlias, runtime_checkable

__all__ = ["ConditionalStore", "DocumentSnapshot", "JsonValue"]

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    """One exact collection result, independent of a provider's query models.

    The queried namespace owns the key. Values are independent finite JSON
    snapshots; timestamps are timezone-aware UTC creation and update times.
    """

    key: str
    value: dict[str, JsonValue]
    created_at: datetime
    updated_at: datetime


@runtime_checkable
class ConditionalStore(Protocol):
    """Query exact collections and condition each document mutation on its value.

    Implementations own the transaction, including exclusion across processes.
    None as the expected value means create only if absent; None as the new
    value means delete. A rejected condition returns False without changing
    data. Values are finite JSON snapshots, never caller-owned mutable state.
    Validation and resource failures retain the provider's documented error
    families; caller cancellation must propagate rather than become a conflict.
    """

    async def asearch_exact(
        self,
        namespace: tuple[str, ...],
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentSnapshot]:
        """Page only this collection, excluding descendant namespaces before paging.

        Results sort by update time descending, then key ascending. Nonnegative
        limit and offset count documents in the exact namespace. Invalid inputs
        and resource failures retain the provider's documented error semantics.

        Args:
            namespace: Complete document collection namespace.
            limit: Nonnegative maximum result count.
            offset: Nonnegative number of documents to skip in this collection.

        Returns:
            Independent document snapshots from the exact collection.
        """
        ...

    async def acompare_and_set(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        expected: dict[str, JsonValue] | None,
        value: dict[str, JsonValue] | None,
    ) -> bool:
        """Compare and commit one document, or leave it untouched on conflict.

        Args:
            namespace: Complete document collection namespace.
            key: Document key within the collection.
            expected: Finite observed JSON value, or None to require absence.
            value: Finite replacement JSON value, or None to delete.

        Returns:
            True after commit, or False if the complete value changed. Object
            member order is immaterial; array order and scalar types are exact.
        """
        ...
