"""Conversation queries that keep historical and live AG-UI identities aligned."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Generic, TypeVar

from tinkerfin_contracts import ThreadIdentity
from tinkerfin_contracts.identity import validate_namespace
from tinkerfin_tracing import (
    TraceFollow,
    TraceGraphFilter,
    TraceGraphQuery,
    Tracer,
    TraceThread,
)

from ._agui_history_models import (
    AgUiTraceGraphDelta,
    AgUiTraceGraphPage,
    AgUiTraceHistory,
    AgUiTraceUpdate,
)
from ._agui_history_projection import (
    _project_graph_delta,
    _project_graph_page,
    _project_history,
    _project_update,
)

_InputT = TypeVar("_InputT")
_OutputT = TypeVar("_OutputT")


class _MappedFollow(Generic[_InputT, _OutputT]):
    """Convert one item per pull while the original follower owns settlement."""

    def __init__(
        self, source: TraceFollow[_InputT], convert: Callable[[_InputT], _OutputT]
    ) -> None:
        self._source = source
        self._convert = convert

    def __aiter__(self) -> _MappedFollow[_InputT, _OutputT]:
        return self

    async def __anext__(self) -> _OutputT:
        item = await anext(self._source)
        try:
            return self._convert(item)
        except BaseException as error:
            await self._source.__aexit__(type(error), error, error.__traceback__)
            raise

    async def aclose(self) -> None:
        await self._source.aclose()

    async def __aenter__(self) -> _MappedFollow[_InputT, _OutputT]:
        await self._source.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._source.__aexit__(exc_type, exc_value, traceback)


class AgUiHistoryView:
    """Read and follow one recorded conversation with its live AG-UI references.

    The view borrows the original Trace handle and Store. It retains the handle's
    branch, generation, fixed history prefix, omissions, and pagination semantics.
    Closing a follower never closes the borrowed Store.
    """

    def __init__(self, trace: TraceThread) -> None:
        """Bind a real recorded-history handle without accepting another identity.

        Args:
            trace: Borrowed history returned by a Tracer query.

        Raises:
            TypeError: The supplied value is not a TraceThread.
        """
        if not isinstance(trace, TraceThread):
            raise TypeError("trace must be a TraceThread")
        self._trace = trace
        self._identity = ThreadIdentity(
            namespace=trace.key.namespace, thread_id=trace.key.thread_id
        )

    @property
    def trace(self) -> TraceThread:
        """Read original facts and business projections through the borrowed handle."""
        return self._trace

    @property
    def snapshot(self) -> AgUiTraceHistory:
        """Return the loaded history with references for live messages and actions."""
        return _project_history(self._trace)

    async def load_older(self, *, limit: int = 100) -> AgUiHistoryView:
        """Expand history without changing its branch or fixed prefix.

        Args:
            limit: Additional earlier Turns to load, within Trace's query limits.

        Returns:
            This same view after its borrowed history handle has expanded.

        Raises:
            ValueError: The page limit is invalid.
            TracingError: The recorded history cannot be read at its fixed prefix.
        """
        await self._trace.load_older(limit=limit)
        return self

    def follow(self) -> TraceFollow[AgUiTraceUpdate]:
        """Follow committed history changes with the same live identities.

        Returns:
            A single-use follower. Use ``async with`` when stopping early; external
            close cancels an active pull and waits for original stream settlement.
            Conversion failure also closes the original follower. Cancellation and
            native query failures propagate; no extra task or queue is created.
        """
        return _MappedFollow(
            self._trace.follow(),
            lambda update: _project_update(update, identity=self._identity),
        )


class AgUiGraphQuery:
    """Read and follow a filtered execution graph with live AG-UI references.

    Identity comes from the real query handle, including its generation. This
    view does not create another query, cache, subscription owner, or Store.
    """

    def __init__(self, trace: TraceGraphQuery) -> None:
        """Bind an existing graph query with its original identity and filter.

        Args:
            trace: Borrowed graph query returned by a Tracer.

        Raises:
            TypeError: The supplied value is not a TraceGraphQuery.
        """
        if not isinstance(trace, TraceGraphQuery):
            raise TypeError("trace must be a TraceGraphQuery")
        self._trace = trace
        self._identity = ThreadIdentity(
            namespace=trace.key.namespace, thread_id=trace.key.thread_id
        )

    @property
    def trace(self) -> TraceGraphQuery:
        """Return the borrowed query for access to original graph evidence."""
        return self._trace

    @property
    def snapshot(self) -> AgUiTraceGraphPage:
        """Return the current filtered page with unchanged cursor and ordering."""
        return _project_graph_page(self._trace.snapshot, identity=self._identity)

    def follow(self) -> TraceFollow[AgUiTraceGraphDelta]:
        """Follow changes to this query's first page and preserve its filter.

        Returns:
            A single-use follower with the original concurrent-close, cancellation,
            and error semantics. Use its async context when a loop may stop early.
            The borrowed Store remains open after following ends.

        Raises:
            TraceFollowLifecycleError: The original query cannot be followed.
        """
        return _MappedFollow(
            self._trace.follow(),
            lambda update: _project_graph_delta(update, identity=self._identity),
        )


class AgUiHistory:
    """Read recorded conversations alongside live AG-UI without building an agent.

    Choose the history source explicitly; it is independent of which observers
    receive new runs. The Tracer and its Store remain owned by the caller. No
    subscription or database connection is opened by construction.
    """

    def __init__(self, tracer: Tracer, *, namespace: str) -> None:
        """Bind a borrowed history source to one application-authorized namespace.

        Args:
            tracer: Source of recorded conversation facts and registered projections.
            namespace: Application-selected scope; every query stays in this scope.

        Raises:
            TypeError: The source is not a Tracer or the namespace has the wrong type.
            ValueError: The namespace is invalid.
        """
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must be a Tracer")
        validate_namespace(namespace)
        self._tracer = tracer
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        """Return the immutable scope used by every conversation query."""
        return self._namespace

    async def get(
        self,
        thread_id: str,
        *,
        head_run_id: str | None = None,
        limit: int = 100,
        history_cursor: str | None = None,
        projections: tuple[str, ...] = (),
    ) -> AgUiHistoryView:
        """Read one fixed-prefix conversation and its pending interactions.

        Args:
            thread_id: Application-authorized conversation within this reader's scope.
            head_run_id: Optional branch head; required for an ambiguous history.
            limit: Number of latest Turns to load.
            history_cursor: Cursor that expands the same branch, generation, and prefix.
            projections: Registered business projections available through ``view.trace``.

        Returns:
            A history view whose snapshot, older pages, and follow updates consistently
            carry the live protocol references. Omitted content remains omitted.

        Raises:
            TypeError: Identifiers or projection names have invalid types.
            ValueError: The limit or query arguments are invalid.
            TracingError: The thread, branch, cursor, or recorded facts cannot be read.
        """
        trace = await self._tracer.get(
            ThreadIdentity(namespace=self._namespace, thread_id=thread_id),
            head_run_id=head_run_id,
            limit=limit,
            history_cursor=history_cursor,
            projections=projections,
        )
        return AgUiHistoryView(trace)

    async def query(
        self,
        thread_id: str,
        *,
        where: TraceGraphFilter | None = None,
        head_run_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> AgUiGraphQuery:
        """Read a filtered execution graph for one recorded conversation.

        Args:
            thread_id: Application-authorized conversation within this reader's scope.
            where: Optional graph filters supported by the Trace query.
            head_run_id: Optional branch head; required when several heads exist.
            cursor: Cursor for the same generation, head, filter, and graph tail.
            limit: Maximum direct matches before their parent Subagents are included.

        Returns:
            A graph view whose page and first-page follow share the original query's
            identity. Pass its next cursor to this method for another older page.

        Raises:
            TypeError: Identifiers or filters have invalid types.
            ValueError: Query arguments are invalid.
            TracingError: The graph or cursor cannot be read consistently.
        """
        trace = await self._tracer.query(
            ThreadIdentity(namespace=self._namespace, thread_id=thread_id),
            where=where,
            head_run_id=head_run_id,
            cursor=cursor,
            limit=limit,
        )
        return AgUiGraphQuery(trace)
