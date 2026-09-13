"""Opaque Graph cursors and closeable first-page following."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

from pydantic import Field, ValidationError, field_validator

from ._models import TraceModel
from .errors import InvalidTraceCursor
from .follow import TraceFollow, _close_trace_source, create_trace_follow
from .graph import (
    TraceGraphCompleteness,
    TraceGraphDelta,
    TraceGraphFilter,
    TraceGraphNode,
    TraceGraphPage,
    TraceGraphTurn,
    graph_delta,
)
from .store import TraceStore, TraceThreadKey


class _GraphCursor(TraceModel, frozen=True):
    namespace: str = Field(min_length=1, max_length=2048)
    thread_id: str = Field(min_length=1, max_length=2048)
    generation: str = Field(min_length=1, max_length=2048)
    head_run_id: str | None = Field(default=None, min_length=1, max_length=1024)
    as_of_seq: int = Field(ge=1)
    filter_digest: str = Field(min_length=64, max_length=64)
    before_started_at: datetime
    before_node_id: str = Field(min_length=1, max_length=2048)

    @field_validator("before_started_at")
    @classmethod
    def time_is_utc(cls, value: datetime) -> datetime:
        """Reject forged cursors whose ordering time is not aware UTC."""

        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Trace Graph cursor time must be aware UTC")
        return value


def _filter_digest(where: TraceGraphFilter) -> str:
    canonical = {
        "kinds": sorted(value.value for value in where.kinds),
        "statuses": sorted(value.value for value in where.statuses),
        "modelCallId": where.model_call_id,
        "agentNames": sorted(where.agent_names),
        "providers": sorted(where.providers),
        "models": sorted(where.models),
        "graphNamespaces": sorted(list(value) for value in where.graph_namespaces),
        "search": where.search,
        "startedAfter": (
            None if where.started_after is None else where.started_after.isoformat()
        ),
        "startedBefore": (
            None if where.started_before is None else where.started_before.isoformat()
        ),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def encode_graph_cursor(
    *,
    key: TraceThreadKey,
    head_run_id: str | None,
    as_of_seq: int,
    where: TraceGraphFilter,
    before_started_at: datetime,
    before_node_id: str,
) -> str:
    """Encode one generation-, prefix-, and filter-bound Graph cursor."""

    payload = _GraphCursor(
        namespace=key.namespace,
        thread_id=key.thread_id,
        generation=key.generation,
        head_run_id=head_run_id,
        as_of_seq=as_of_seq,
        filter_digest=_filter_digest(where),
        before_started_at=before_started_at,
        before_node_id=before_node_id,
    ).model_dump_json(by_alias=True)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_graph_cursor(
    value: str,
    *,
    key: TraceThreadKey,
    head_run_id: str | None,
    as_of_seq: int,
    where: TraceGraphFilter,
) -> tuple[datetime, str]:
    """Validate and decode one cursor for the exact current Graph prefix."""

    if not isinstance(value, str) or not value:
        raise InvalidTraceCursor("Trace Graph cursor must be non-empty text")
    if len(value) > 16_384:
        raise InvalidTraceCursor("Trace Graph cursor exceeds the maximum length")
    try:
        padding = "=" * (-len(value) % 4)
        cursor = _GraphCursor.model_validate_json(
            base64.urlsafe_b64decode((value + padding).encode())
        )
    except (ValueError, ValidationError) as error:
        raise InvalidTraceCursor(
            "Trace Graph cursor is invalid", cause=error
        ) from error
    if (
        cursor.namespace != key.namespace
        or cursor.thread_id != key.thread_id
        or cursor.generation != key.generation
        or cursor.head_run_id != head_run_id
        or cursor.as_of_seq != as_of_seq
        or cursor.filter_digest != _filter_digest(where)
    ):
        raise InvalidTraceCursor("Trace Graph cursor belongs to another query")
    return cursor.before_started_at, cursor.before_node_id


class TraceGraphQuery:
    """Expose one Graph page and optional closeable live first-page updates."""

    __slots__ = (
        "_follow_enabled",
        "_key",
        "_max_page_bytes",
        "_page",
        "_refresh",
        "_store",
    )

    def __init__(
        self,
        page: TraceGraphPage,
        *,
        store: TraceStore,
        key: TraceThreadKey,
        refresh: Callable[[], Awaitable[TraceGraphPage]],
        follow_enabled: bool,
        max_page_bytes: int,
    ) -> None:
        """Create a defensive query view over one exact Store generation."""

        self._page = page.model_copy(deep=True)
        self._store = store
        self._key = key
        self._refresh = refresh
        self._follow_enabled = follow_enabled
        self._max_page_bytes = max_page_bytes

    @property
    def key(self) -> TraceThreadKey:
        """Return the immutable thread and generation selected by this query."""

        return self._key

    @property
    def nodes(self) -> tuple[TraceGraphNode, ...]:
        """Return the framework-ordered visible Graph nodes."""

        return tuple(node.model_copy(deep=True) for node in self._page.nodes)

    @property
    def snapshot(self) -> TraceGraphPage:
        """Return a defensive copy of the complete current Graph page."""

        return self._page.model_copy(deep=True)

    @property
    def turns(self) -> tuple[TraceGraphTurn, ...]:
        """Return selected-lineage Turns represented by this page."""

        return tuple(turn.model_copy(deep=True) for turn in self._page.turns)

    @property
    def ordered_node_ids(self) -> tuple[str, ...]:
        """Return the complete authoritative visible node order."""

        return self._page.ordered_node_ids

    @property
    def matched_node_ids(self) -> tuple[str, ...]:
        """Return directly matched nodes in authoritative visible order."""

        return self._page.matched_node_ids

    @property
    def next_cursor(self) -> str | None:
        """Return the cursor for the next older matching page."""

        return self._page.next_cursor

    @property
    def as_of_seq(self) -> int:
        """Return the Ledger tail represented by this page."""

        return self._page.as_of_seq

    @property
    def completeness(self) -> TraceGraphCompleteness:
        """Return explicit Graph evidence gaps."""

        return self._page.completeness.model_copy(deep=True)

    def follow(self) -> TraceFollow[TraceGraphDelta]:
        """Follow changes to the unpaginated current first page."""

        if not self._follow_enabled:
            raise ValueError("Trace Graph follow requires the current first page")

        async def updates() -> AsyncIterator[TraceGraphDelta]:
            previous = self._page
            batches = self._store.follow(self._key, after_seq=self._page.as_of_seq)
            primary_error: BaseException | None = None
            try:
                async for batch in batches:
                    # Graph nodes describe committed call evidence. Ownership-only
                    # changes affect Run completeness, not this independent model.
                    if not batch.events:
                        continue
                    current = await self._refresh()
                    delta = graph_delta(
                        previous,
                        current,
                        max_bytes=self._max_page_bytes,
                    )
                    if (
                        delta.turn_upserts
                        or delta.turn_removes
                        or delta.node_upserts
                        or delta.node_removes
                        or current.as_of_seq != previous.as_of_seq
                        or current.ordered_node_ids != previous.ordered_node_ids
                        or current.matched_node_ids != previous.matched_node_ids
                        or current.completeness != previous.completeness
                        or current.next_cursor != previous.next_cursor
                    ):
                        yield delta
                    previous = current
            except BaseException as error:
                primary_error = error
                raise
            finally:
                await _close_trace_source(
                    batches,
                    primary_error=primary_error,
                )

        return create_trace_follow(updates)


__all__ = [
    "TraceGraphQuery",
    "decode_graph_cursor",
    "encode_graph_cursor",
]
