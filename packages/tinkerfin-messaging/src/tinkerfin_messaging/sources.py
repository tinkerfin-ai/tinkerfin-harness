"""Reusable single-use asynchronous message source adapters."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, cast, overload

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity
from ._messaging_boundary import _join_owned_task
from ._tasks import TaskOutcome, capture, join_owned_task, select_failure
from .messaging import (
    CancelCallback,
    CancelContext,
    _ContextCancelCallback,
    _invoke_cancel,
    _normalize_cancel_callback,
)
from .protocols import MessageSource

SourceT = TypeVar("SourceT")
SourceT_co = TypeVar("SourceT_co", covariant=True)
MappedT = TypeVar("MappedT")
ReplayT = TypeVar("ReplayT")


class CancellableMessageSource(
    MessageSource[SourceT_co],
    Protocol[SourceT_co],
):
    """Message source that explicitly publishes its producer cancellation callback."""

    @property
    def messaging_cancel_callback(self) -> CancelCallback[SourceT_co] | None:
        """Return the source-owned producer cancellation callback, if supported."""

        ...


@dataclass(frozen=True, slots=True)
class MessageSourceBinding(Generic[SourceT]):
    """Pair one opened source with its optional producer cancellation callback."""

    source: MessageSource[SourceT]
    cancel: CancelCallback[SourceT] | None = None


@dataclass(frozen=True, slots=True)
class _OpenedMessageSource(Generic[SourceT]):
    source: MessageSource[SourceT]
    cancel: _ContextCancelCallback[SourceT] | None


@dataclass(frozen=True, slots=True)
class _DeferredOpenOutcome(Generic[SourceT]):
    """Carry an opened binding or process-control failure back to its owner task."""

    binding: _OpenedMessageSource[SourceT] | None = None
    error: BaseException | None = None


class DeferredMessageSource(Generic[SourceT]):
    """Open one owner source during Messaging readiness or its first standalone pull.

    Messaging opens the binding only after durable owner selection and before its ready
    callback; an unused attachment never opens it. Standalone iteration still opens on
    the first pull. The wrapper owns the binding through cancellation and settlement.
    Natural exhaustion does not close it; the producer owner completes that lifecycle
    through `aclose()`.

    Args:
        opener: Async factory returning the source and its optional cancel callback.
        cancellable: Durable capability declared before backend preparation. The opened
            binding must contain a callback exactly when this value is true.
        cancel_after_first_item: Delay the callback until the first pull has produced
            an item or terminal outcome. Use this when the first item establishes a
            protocol lifecycle that cancellation must not overtake.
        on_owner_preflight: Optional callback settled after durable owner acquisition
            and before the request-owned source is opened.

    Raises:
        TypeError: The opener or cancellable declaration has an invalid shape.
    """

    def __init__(
        self,
        opener: Callable[[], Awaitable[MessageSourceBinding[SourceT]]],
        *,
        cancellable: bool,
        cancel_after_first_item: bool = False,
        on_owner_preflight: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize deferred ownership without invoking the opener."""

        if not callable(opener):
            raise TypeError("opener must be an async callable")
        if not isinstance(cancellable, bool):
            raise TypeError("cancellable must be a boolean")
        if not isinstance(cancel_after_first_item, bool):
            raise TypeError("cancel_after_first_item must be a boolean")
        if cancel_after_first_item and not cancellable:
            raise ValueError("cancel_after_first_item requires a cancellable source")
        if on_owner_preflight is not None and not callable(on_owner_preflight):
            raise TypeError("on_owner_preflight must be an async callable or None")
        self._opener = opener
        self._cancellable = cancellable
        self._cancel_after_first_item = cancel_after_first_item
        self._claimed = False
        self._closed = False
        self._exhausted = False
        self._first_pull_settled = asyncio.Event()
        self._iterator: AsyncIterator[SourceT] | None = None
        self._open_task: asyncio.Task[_DeferredOpenOutcome[SourceT]] | None = None
        self._binding: _OpenedMessageSource[SourceT] | None = None
        self._active_task: asyncio.Task[object] | None = None
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._owner_preflight = on_owner_preflight
        self._owner_preflight_task: asyncio.Task[BaseException | None] | None = None

    @property
    def messaging_cancel_callback(self) -> CancelCallback[SourceT] | None:
        """Advertise the source-owned callback before durable preparation."""

        if not self._cancellable:
            return None
        return self.cancel

    def messaging_cancel_callback_matches(self, callback: object) -> bool:
        """Return whether a supplied callback names this source's same owner."""

        return callback == self.cancel

    async def messaging_owner_preflight(self) -> None:
        """Settle the owner hook and open the source before producer execution."""

        callback = self._owner_preflight
        task = self._owner_preflight_task
        if task is None:

            async def invoke() -> BaseException | None:
                # Return failures as data so shield cancellation cannot log a late
                # provider error independently of the retained preparation owner.
                try:
                    if callback is not None:
                        await callback()
                    await self._open()
                except BaseException as error:  # noqa: BLE001 - re-raised by the owner
                    return error
                return None

            task = asyncio.create_task(
                invoke(),
                name="tinkerfin-messaging-owner-preflight",
            )
            self._owner_preflight_task = task
        try:
            error = await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            # Preparation is interruptible. Joining an uncancelled opener here would
            # prevent the owner from reaching aclose(), which owns the pending binding.
            if not task.done():
                task.cancel()
            try:
                await _join_owned_task(task)
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve cancellation
                if not isinstance(cleanup_error, asyncio.CancelledError):
                    cancellation.add_note(
                        "Owner preparation cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            if not task.cancelled():
                cleanup_error = task.result()
                if cleanup_error is not None and not isinstance(
                    cleanup_error, asyncio.CancelledError
                ):
                    cancellation.add_note(
                        "Owner preparation cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            raise cancellation
        if error is not None:
            raise error.with_traceback(error.__traceback__)

    def __aiter__(self) -> DeferredMessageSource[SourceT]:
        """Claim and return this source's single-use asynchronous iterator."""

        if self._claimed:
            raise RuntimeError("a deferred source can only be consumed once")
        if self._closed:
            raise RuntimeError("a closed deferred source cannot be consumed")
        self._claimed = True
        return self

    async def __anext__(self) -> SourceT:
        """Open lazily and return the next item under one active-pull invariant."""

        if self._closed or self._exhausted:
            raise StopAsyncIteration
        current = cast(asyncio.Task[object] | None, asyncio.current_task())
        if current is None:  # pragma: no cover - async methods run in a Task
            raise RuntimeError("a deferred source requires an asyncio task")
        active = self._active_task
        if active is not None and not active.done():
            raise RuntimeError("a deferred source operation is already active")
        self._active_task = current
        try:
            iterator = self._iterator
            if iterator is None:
                binding = await self._open()
                iterator = aiter(binding.source)
                self._iterator = iterator
            first_pull = not self._first_pull_settled.is_set()
            try:
                item = await anext(iterator)
            except StopAsyncIteration:
                # Messaging owns settlement and closes the source afterwards. Keeping
                # the binding alive here lets an already accepted cancellation invoke
                # its callback without racing natural exhaustion.
                self._exhausted = True
                if first_pull:
                    self._first_pull_settled.set()
                raise
            except asyncio.CancelledError:
                # A source-owned cancel callback can cancel this pull and wait for it.
                # Messaging closes the source after that callback settles; closing here
                # would make the pull and callback wait on each other.
                if first_pull:
                    self._first_pull_settled.set()
                raise
            except BaseException:
                if first_pull:
                    self._first_pull_settled.set()
                raise
            if first_pull:
                self._first_pull_settled.set()
            return item
        finally:
            if self._active_task is current:
                self._active_task = None

    async def cancel(self, context: CancelContext) -> Iterable[SourceT] | None:
        """Wait for the owner source and invoke its normalized cancellation callback."""

        if not self._cancellable:
            raise RuntimeError("this deferred source is not cancellable")
        binding = await self._open()
        if self._cancel_after_first_item:
            await self._first_pull_settled.wait()
        callback = binding.cancel
        if callback is None:  # pragma: no cover - validated while opening
            raise RuntimeError("the opened source has no cancellation callback")
        return await _invoke_cancel(callback, context)

    async def aclose(self) -> None:
        """Close without opening an unused source, or settle an active source once."""

        current = asyncio.current_task()
        active = self._active_task
        if active is not None and active is not current and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        await self._finish()

    async def _open(self) -> _OpenedMessageSource[SourceT]:
        if self._closed:
            raise RuntimeError("a closed deferred source cannot be opened")
        task = self._open_task
        if task is None:
            task = asyncio.create_task(
                self._open_once(),
                name="tinkerfin-messaging-deferred-source-open",
            )
            self._open_task = task
        outcome = await asyncio.shield(task)
        if outcome.error is not None:
            raise outcome.error.with_traceback(outcome.error.__traceback__)
        if outcome.binding is None:  # pragma: no cover - every opener has one outcome
            raise RuntimeError("deferred source opening produced no binding")
        return outcome.binding

    async def _open_once(self) -> _DeferredOpenOutcome[SourceT]:
        """Run opener failures as data so process control reaches the awaiting owner."""

        try:
            return _DeferredOpenOutcome(binding=await self._open_binding())
        except BaseException as error:  # noqa: BLE001 - re-raised by the owner task
            return _DeferredOpenOutcome(error=error)

    async def _open_binding(self) -> _OpenedMessageSource[SourceT]:
        """Validate and retain the single source binding produced by the opener."""

        opened = self._opener()
        if not inspect.isawaitable(opened):
            raise TypeError("opener must return an awaitable")
        binding = await opened
        if not isinstance(binding, MessageSourceBinding):
            raise TypeError("opener must resolve to MessageSourceBinding")
        if not isinstance(binding.source, MessageSource):
            raise TypeError("opened source must implement MessageSource")
        cancel = binding.cancel
        if cancel is None:
            cancel = getattr(binding.source, "messaging_cancel_callback", None)
        try:
            normalized = None if cancel is None else _normalize_cancel_callback(cancel)
        except Exception as error:
            await self._close_rejected_source(binding.source, error)
            raise
        if self._cancellable != (normalized is not None):
            error = TypeError(
                "opened cancellation callback does not match cancellable declaration"
            )
            await self._close_rejected_source(binding.source, error)
            raise error
        resolved = _OpenedMessageSource(
            source=binding.source,
            cancel=normalized,
        )
        self._binding = resolved
        return resolved

    @staticmethod
    async def _close_rejected_source(
        source: MessageSource[SourceT],
        error: Exception,
    ) -> None:
        try:
            await source.aclose()
        except BaseException as close_error:
            if isinstance(close_error, Exception):
                error.add_note(
                    "opened source cleanup also failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )
                return
            close_error.add_note(
                f"deferred source opening also failed: {type(error).__name__}: {error}"
            )
            raise

    async def _finish(self) -> None:
        task = self._close_task
        if task is None or (task.done() and isinstance(task.result(), BaseException)):
            self._closed = True
            task = asyncio.create_task(
                capture(self._close_once()),
                name="tinkerfin-messaging-deferred-source-close",
            )
            self._close_task = task
        await join_owned_task(task)

    async def _close_once(self) -> None:
        open_task = self._open_task
        if open_task is not None and not open_task.done():
            open_task.cancel()
            await asyncio.gather(open_task, return_exceptions=True)
        binding = self._binding
        self._iterator = None
        self._first_pull_settled.set()
        if binding is None:
            return
        # Keep the binding when cleanup fails so a later explicit aclose can
        # finish an upstream operation that outlived its first waiting limit.
        await binding.source.aclose()
        self._binding = None


class ProfiledDeferredMessageSource(
    DeferredMessageSource[SourceT],
    Generic[SourceT, ReplayT],
):
    """Defer opening while publishing a complete immutable codec profile."""

    def __init__(
        self,
        opener: Callable[[], Awaitable[MessageSourceBinding[SourceT]]],
        *,
        identity: RunIdentity,
        codec_profile: str,
        source_type: type[SourceT],
        replay_type: type[ReplayT],
        cancellable: bool,
        cancel_after_first_item: bool = False,
        on_owner_preflight: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize deferred ownership and an immutable built-in codec profile.

        Args:
            opener: Async factory invoked only after owner preflight succeeds.
            identity: Immutable durable run identity available before source opening.
            codec_profile: Registered built-in codec profile name.
            source_type: Exact live item type consumed by the profile codec.
            replay_type: Exact decoded replay item type.
            cancellable: Whether the future opened binding must expose cancellation.
            cancel_after_first_item: Whether remote cancellation waits for the first
                owner pull to settle.
            on_owner_preflight: Optional owner-only callback completed after durable
                preparation and before the source is opened.

        Raises:
            TypeError: A profile type or inherited deferred-source option is invalid.
            ValueError: An identifier or cancellation option is invalid.
        """

        super().__init__(
            opener,
            cancellable=cancellable,
            cancel_after_first_item=cancel_after_first_item,
            on_owner_preflight=on_owner_preflight,
        )
        self._messaging_identity = required_identity(identity)
        self._messaging_codec_profile = required_identifier(
            "codec_profile",
            codec_profile,
        )
        if not isinstance(source_type, type):
            raise TypeError("source_type must be a type")
        if not isinstance(replay_type, type):
            raise TypeError("replay_type must be a type")
        self._messaging_source_type = source_type
        self._messaging_replay_type = replay_type

    @property
    def messaging_identity(self) -> RunIdentity:
        """Return the durable run identity without opening the source."""

        return self._messaging_identity

    @property
    def messaging_codec_profile(self) -> str:
        """Return the codec profile without opening the source."""

        return self._messaging_codec_profile

    @property
    def messaging_source_type(self) -> type[SourceT]:
        """Return the live source item type."""

        return self._messaging_source_type

    @property
    def messaging_replay_type(self) -> type[ReplayT]:
        """Return the replay item type."""

        return self._messaging_replay_type


class FiniteMessageSource(Generic[SourceT]):
    """Expose a fixed event snapshot as one asynchronous source."""

    def __init__(self, events: Iterable[SourceT]) -> None:
        """Snapshot a finite iterable without borrowing its mutable container."""

        self._events = tuple(events)
        self._claimed = False
        self._closed = False
        self._iterator: AsyncGenerator[SourceT, None] | None = None

    @classmethod
    def from_events(
        cls,
        events: Iterable[SourceT],
    ) -> FiniteMessageSource[SourceT]:
        """Snapshot finite events without taking ownership of caller containers.

        Args:
            events: Finite iterable copied immediately in its current order.

        Returns:
            A source that can be consumed exactly once.
        """

        return cls(events)

    def __aiter__(self) -> AsyncIterator[SourceT]:
        """Claim and return the finite source's one asynchronous iterator."""

        if self._claimed:
            raise RuntimeError("a finite source can only be consumed once")
        if self._closed:
            raise RuntimeError("a closed finite source cannot be consumed")
        self._claimed = True
        iterator = self._iterate()
        self._iterator = iterator
        return iterator

    async def _iterate(self) -> AsyncGenerator[SourceT, None]:
        try:
            for event in self._events:
                yield event
        finally:
            self._closed = True
            self._iterator = None

    async def aclose(self) -> None:
        """Close an unclaimed or partially consumed finite source idempotently."""

        if self._closed:
            return
        self._closed = True
        iterator = self._iterator
        self._iterator = None
        if iterator is not None:
            await iterator.aclose()


class _MappedMessageSource(Generic[SourceT, MappedT]):
    """Transform one item at a time while retaining source preparation and cleanup.

    Pulls stay in their consumer task, preserving upstream cancellation and
    context. Close attempts have explicit owners. A failed close can be awaited
    again because MessageSource.aclose is idempotent; only a successful attempt
    finishes ownership. No pull or transform is prefetched.
    """

    def __init__(
        self,
        source: MessageSource[SourceT],
        transform: Callable[[SourceT], MappedT | Awaitable[MappedT]],
    ) -> None:
        self._source = source
        self._transform = transform
        source_cancel = getattr(source, "messaging_cancel_callback", None)
        self._source_cancel = source_cancel
        self._normalized_cancel = (
            None if source_cancel is None else _normalize_cancel_callback(source_cancel)
        )
        owner_preflight = getattr(source, "messaging_owner_preflight", None)
        if owner_preflight is not None and not callable(owner_preflight):
            raise TypeError("messaging_owner_preflight must be an async callable")
        self._owner_preflight = owner_preflight
        self._cancel_after_first_item = (
            getattr(source, "messaging_cancel_waits_for_first_item", False) is True
        )
        self._first_pull_settled = asyncio.Event()
        self._claimed = False
        self._closed = False
        self._exhausted = False
        self._iterator: AsyncIterator[SourceT] | None = None
        self._active_consumer: asyncio.Task[object] | None = None
        self._pull_settled = asyncio.Event()
        self._pull_settled.set()
        self._pull_error: BaseException | None = None
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None

    def __aiter__(self) -> _MappedMessageSource[SourceT, MappedT]:
        if self._claimed:
            raise RuntimeError("a mapped source can only be consumed once")
        if self._closed:
            raise RuntimeError("a closed mapped source cannot be consumed")
        self._claimed = True
        return self

    async def __anext__(self) -> MappedT:
        if self._closed or self._exhausted:
            raise StopAsyncIteration
        if self._active_consumer is not None:
            raise RuntimeError("a mapped source pull is already active")
        self._active_consumer = asyncio.current_task()
        self._pull_error = None
        self._pull_settled.clear()
        try:
            # Iterators may retain their consuming Task to implement cancellation
            # or use ContextVars across yields. Do not move pulls to child Tasks.
            return await self._pull_once()
        except BaseException as error:
            self._pull_error = error
            raise
        finally:
            self._active_consumer = None
            self._pull_settled.set()

    async def _pull_once(self) -> MappedT:
        try:
            iterator = self._iterator
            if iterator is None:
                iterator = aiter(self._source)
                self._iterator = iterator
            try:
                item = await anext(iterator)
            except StopAsyncIteration:
                self._exhausted = True
                raise
            return await self._apply_transform(item)
        finally:
            # Cancellation cannot overtake the first transformed lifecycle event.
            # Failed or exhausted first pulls must also release a waiting cancel.
            self._first_pull_settled.set()

    @property
    def messaging_cancel_callback(self) -> CancelCallback[MappedT] | None:
        """Publish a mapped callback only when the source owns one."""

        if self._normalized_cancel is None:
            return None
        return self.cancel

    @property
    def messaging_cancel_waits_for_first_item(self) -> bool:
        """Retain the source's first-event cancellation constraint."""

        return self._cancel_after_first_item

    def messaging_cancel_callback_matches(self, callback: object) -> bool:
        """Match this callback or the equivalent owner before mapping."""

        if callback == self.cancel or callback == self._source_cancel:
            return True
        matcher = getattr(self._source, "messaging_cancel_callback_matches", None)
        return bool(callable(matcher) and matcher(callback))

    async def messaging_owner_preflight(self) -> None:
        """Prepare the source before Messaging announces owner readiness."""

        if self._closed:
            raise RuntimeError("a closed mapped source cannot be prepared")
        if self._owner_preflight is not None:
            pending = self._owner_preflight()
            if not inspect.isawaitable(pending):
                raise TypeError("messaging_owner_preflight must return an awaitable")
            await pending

    async def cancel(self, context: CancelContext) -> tuple[MappedT, ...] | None:
        """Cancel the source and atomically transform its finite tail."""

        callback = self._normalized_cancel
        if callback is None:
            raise RuntimeError("this mapped source is not cancellable")
        if self._cancel_after_first_item:
            await self._first_pull_settled.wait()
        if self._closed:
            return None
        tail = await _invoke_cancel(callback, context)
        if tail is None:
            return None
        transformed: list[MappedT] = []
        for item in tail:
            transformed.append(await self._apply_transform(item))
        return tuple(transformed)

    async def _apply_transform(self, item: SourceT) -> MappedT:
        transformed = self._transform(item)
        if inspect.isawaitable(transformed):
            transformed = await cast(Awaitable[MappedT], transformed)
        return cast(MappedT, transformed)

    async def aclose(self) -> None:
        """Settle active work and close the source, rejoining after failed cleanup."""

        if self._active_consumer is asyncio.current_task():
            raise RuntimeError("a transform cannot close its own active source")
        task = self._close_task
        if task is None or (task.done() and isinstance(task.result(), BaseException)):
            self._closed = True
            task = asyncio.create_task(
                capture(self._close_once()),
                name="tinkerfin-messaging-mapped-source-close",
            )
            self._close_task = task
        await join_owned_task(task)

    async def _close_once(self) -> None:
        primary: BaseException | None = None
        consumer = self._active_consumer
        if consumer is not None:
            requested = not consumer.done() and not consumer.cancelling()
            if requested:
                consumer.cancel()
            # The consumer can next wait for this close. Join only its current
            # pull, never the whole producer task, to avoid a cyclic wait.
            await self._pull_settled.wait()
            error = self._pull_error
            if error is not None and not (
                requested
                and isinstance(error, asyncio.CancelledError)
                and error.__cause__ is None
                and error.__context__ is None
            ):
                primary = error
        self._first_pull_settled.set()
        try:
            await self._source.aclose()
        except BaseException as error:  # noqa: BLE001 - preserve close outcome
            primary = error if primary is None else select_failure(primary, error)
        if primary is not None:
            raise primary
        self._iterator = None


@overload
def map_source(
    source: CancellableMessageSource[SourceT],
    transform: Callable[[SourceT], Awaitable[MappedT]],
) -> CancellableMessageSource[MappedT]: ...


@overload
def map_source(
    source: CancellableMessageSource[SourceT],
    transform: Callable[[SourceT], MappedT],
) -> CancellableMessageSource[MappedT]: ...


@overload
def map_source(
    source: MessageSource[SourceT],
    transform: Callable[[SourceT], Awaitable[MappedT]],
) -> MessageSource[MappedT]: ...


@overload
def map_source(
    source: MessageSource[SourceT],
    transform: Callable[[SourceT], MappedT],
) -> MessageSource[MappedT]: ...


def map_source(
    source: MessageSource[SourceT],
    transform: Callable[[SourceT], MappedT | Awaitable[MappedT]],
) -> MessageSource[MappedT]:
    """Map source values lazily while preserving lifecycle and backpressure.

    Any awaitable returned by ``transform`` is awaited before the next source item is
    requested. The returned source is deliberately unprofiled because a transform can
    change the value type and therefore invalidate the original codec profile.
    Transform callbacks must not close their own active adapter; close it after
    the pull returns or from its consuming context's cleanup.

    Args:
        source: Single-use source closed when the returned adapter is closed.
        transform: Synchronous or asynchronous per-item transformation.

    Returns:
        A single-use mapped source.
    """

    return _MappedMessageSource(source, transform)
