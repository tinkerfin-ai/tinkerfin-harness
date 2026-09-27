"""Reusable asynchronous source lifecycle and transformation contracts."""

from __future__ import annotations

import asyncio
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
)
from typing import assert_type, cast

import pytest

import tinkerfin_messaging.sources as source_adapters
from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    CancelCallback,
    CancelContext,
    CancellableMessageSource,
)
from tinkerfin_messaging.protocols import MessageSource
from tinkerfin_messaging.sources import FiniteMessageSource, map_source


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")


class _TrackedSource:
    def __init__(self, *items: int) -> None:
        self._items = items
        self.pulled: list[int] = []
        self.close_calls = 0
        self._iterator: AsyncGenerator[int, None] | None = None

    def __aiter__(self) -> AsyncIterator[int]:
        async def iterate() -> AsyncGenerator[int, None]:
            for item in self._items:
                self.pulled.append(item)
                yield item

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()


class _CancellableTrackedSource(_TrackedSource):
    def __init__(self, *items: int, tail: tuple[int, ...] = (9,)) -> None:
        super().__init__(*items)
        self.tail = tail
        self.cancel_calls = 0

    @property
    def messaging_cancel_callback(self) -> CancelCallback[int]:
        return self.cancel

    async def cancel(self, _context: CancelContext) -> tuple[int, ...]:
        self.cancel_calls += 1
        return self.tail


class _InvalidCancelSource(_TrackedSource):
    @property
    def messaging_cancel_callback(self) -> Callable[[object, object], None]:
        return self.cancel

    def cancel(self, _first: object, _second: object) -> None:
        return None


class _BlockingCloseSource(_TrackedSource):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.close_release.wait()


async def test_deferred_source_closed_before_claim_never_opens() -> None:
    """An attach-only caller must not execute an unused source opener."""

    open_calls = 0

    async def open_source():
        nonlocal open_calls
        open_calls += 1
        return source_adapters.MessageSourceBinding(source=_TrackedSource(1))

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=False,
    )

    await source.aclose()
    await source.aclose()

    assert open_calls == 0
    with pytest.raises(RuntimeError, match="closed"):
        aiter(source)


async def test_deferred_source_opens_once_on_first_pull_and_closes_once() -> None:
    """Pulling multiple values must share one opened source and one close path."""

    opened = _TrackedSource(1, 2)
    open_calls = 0

    async def open_source():
        nonlocal open_calls
        open_calls += 1
        return source_adapters.MessageSourceBinding(source=opened)

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=False,
    )

    assert open_calls == 0
    assert [item async for item in source] == [1, 2]
    await source.aclose()

    assert open_calls == 1
    assert opened.close_calls == 1
    with pytest.raises(RuntimeError, match="only be consumed once"):
        aiter(source)


async def test_deferred_source_cancel_waits_for_the_shared_open() -> None:
    """Owner cancellation during opening must use that same eventual binding."""

    opening = asyncio.Event()
    release = asyncio.Event()
    received: list[CancelContext] = []
    opened = _TrackedSource(1)

    async def cancel(context: CancelContext) -> tuple[int, ...]:
        received.append(context)
        return (9,)

    async def open_source():
        opening.set()
        await release.wait()
        return source_adapters.MessageSourceBinding(
            source=opened,
            cancel=cancel,
        )

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=True,
    )
    pulling = asyncio.create_task(anext(aiter(source)))
    await opening.wait()
    context = CancelContext(channel="events", identity=_identity())
    cancelling = asyncio.create_task(source.cancel(context))
    await asyncio.sleep(0)
    assert not cancelling.done()

    release.set()

    assert await cancelling == (9,)
    assert await pulling == 1
    assert received == [context]
    await source.aclose()
    assert opened.close_calls == 1


async def test_deferred_source_rejects_binding_that_breaks_cancellable_contract() -> (
    None
):
    """Durable cancellability must be known before backend ownership is claimed."""

    opened = _TrackedSource(1)

    async def open_source():
        return source_adapters.MessageSourceBinding(source=opened)

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=True,
    )

    with pytest.raises(TypeError, match="does not match"):
        await anext(aiter(source))
    assert opened.close_calls == 1


async def test_deferred_source_close_cancels_an_in_flight_open() -> None:
    """External close must settle a blocked opener without constructing a source."""

    opening = asyncio.Event()
    cancelled = asyncio.Event()

    async def open_source():
        opening.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=False,
    )
    pulling = asyncio.create_task(anext(aiter(source)))
    await opening.wait()

    await source.aclose()

    assert cancelled.is_set()
    with pytest.raises(asyncio.CancelledError):
        await pulling


async def test_deferred_source_propagates_opener_failure_without_retry() -> None:
    """An owner opener failure must remain the source failure and execute once."""

    expected = RuntimeError("cannot open source")
    open_calls = 0

    async def open_source():
        nonlocal open_calls
        open_calls += 1
        raise expected

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=False,
    )

    with pytest.raises(RuntimeError) as raised:
        await anext(aiter(source))
    await source.aclose()

    assert raised.value is expected
    assert open_calls == 1


async def test_deferred_source_keeps_cancel_binding_until_owner_close() -> None:
    """Natural exhaustion must not outrun an already accepted cancellation callback."""

    opened = _TrackedSource()
    context = CancelContext(channel="events", identity=_identity())

    async def cancel(received: CancelContext) -> tuple[int, ...]:
        assert received == context
        return (9,)

    async def open_source():
        return source_adapters.MessageSourceBinding(
            source=opened,
            cancel=cancel,
        )

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=True,
    )
    iterator = aiter(source)
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)

    assert await source.cancel(context) == (9,)
    assert opened.close_calls == 0
    await source.aclose()
    assert opened.close_calls == 1


async def test_deferred_source_derives_cancel_from_opened_source() -> None:
    """A deferred wrapper must preserve a source-owned callback without host glue."""

    opened = _CancellableTrackedSource(1)

    async def open_source():
        return source_adapters.MessageSourceBinding(source=opened)

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=True,
    )
    context = CancelContext(channel="events", identity=_identity())

    assert await anext(aiter(source)) == 1
    assert await source.cancel(context) == (9,)
    assert opened.cancel_calls == 1
    await source.aclose()


async def test_deferred_source_explicit_cancel_precedes_source_callback() -> None:
    """An explicit binding remains the cancellation owner when both are available."""

    opened = _CancellableTrackedSource(1)
    explicit_calls = 0

    async def explicit(_context: CancelContext) -> tuple[int, ...]:
        nonlocal explicit_calls
        explicit_calls += 1
        return (7,)

    async def open_source():
        return source_adapters.MessageSourceBinding(
            source=opened,
            cancel=explicit,
        )

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=True,
    )

    assert await source.cancel(
        CancelContext(channel="events", identity=_identity())
    ) == (7,)
    assert explicit_calls == 1
    assert opened.cancel_calls == 0
    await source.aclose()


async def test_deferred_source_closes_an_invalid_derived_cancel_source() -> None:
    """Callback validation failure must not leak the source opened by the owner."""

    opened = _InvalidCancelSource(1)

    async def open_source():
        return source_adapters.MessageSourceBinding(source=opened)

    source = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=True,
    )

    with pytest.raises(TypeError, match="no arguments or one positional"):
        await anext(aiter(source))

    assert opened.close_calls == 1


async def test_finite_source_snapshots_order_and_rejects_second_consumption() -> None:
    """Mutating caller input or iterating twice must not alter one finite source."""

    events = [1, 2]
    source = FiniteMessageSource.from_events(events)
    events.append(3)

    assert [item async for item in source] == [1, 2]
    with pytest.raises(RuntimeError, match="only be consumed once"):
        aiter(source)
    await source.aclose()
    await source.aclose()


async def test_finite_source_rejects_consumption_after_close() -> None:
    """A source closed before its claim must never emit its retained events."""

    source = FiniteMessageSource.from_events((1, 2))

    await source.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        aiter(source)


async def test_map_source_supports_sync_and_async_transforms() -> None:
    """Both transform forms must preserve order and close their borrowed source."""

    sync_source = _TrackedSource(1, 2)
    async_source = _TrackedSource(3, 4)

    async def asynchronously(item: int) -> str:
        await asyncio.sleep(0)
        return f"async:{item}"

    sync_mapped = map_source(sync_source, lambda item: f"sync:{item}")
    async_mapped = map_source(async_source, asynchronously)
    assert_type(sync_mapped, MessageSource[str])
    assert_type(async_mapped, MessageSource[str])

    assert [item async for item in sync_mapped] == ["sync:1", "sync:2"]
    assert [item async for item in async_mapped] == ["async:3", "async:4"]
    assert sync_source.close_calls == 0
    assert async_source.close_calls == 0
    await sync_mapped.aclose()
    await async_mapped.aclose()
    assert sync_source.close_calls == 1
    assert async_source.close_calls == 1


async def test_map_source_waits_for_transform_before_pulling_next_item() -> None:
    """An asynchronous transform must apply backpressure to the upstream source."""

    source = _TrackedSource(1, 2)
    transform_started = asyncio.Event()
    transform_release = asyncio.Event()

    async def transform(item: int) -> int:
        transform_started.set()
        await transform_release.wait()
        return item * 10

    mapped = map_source(source, transform)
    iterator = aiter(mapped)
    first = asyncio.ensure_future(anext(iterator))
    await transform_started.wait()

    assert source.pulled == [1]
    assert not first.done()

    transform_release.set()
    assert await first == 10
    assert await anext(iterator) == 20
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert source.close_calls == 0
    await mapped.aclose()
    assert source.close_calls == 1


async def test_map_source_closes_upstream_after_transform_failure() -> None:
    """A transform failure must propagate and release the upstream source once."""

    source = _TrackedSource(1)
    expected = ValueError("cannot transform")

    def fail(_item: int) -> int:
        raise expected

    mapped = map_source(source, fail)

    with pytest.raises(ValueError) as captured:
        await anext(aiter(mapped))
    assert captured.value is expected
    assert source.close_calls == 0
    await mapped.aclose()
    assert source.close_calls == 1
    await mapped.aclose()
    assert source.close_calls == 1


async def test_map_source_cancellation_closes_upstream() -> None:
    """Cancelling an in-flight transform must not leave the upstream source open."""

    source = _TrackedSource(1)
    transform_started = asyncio.Event()

    async def block(_item: int) -> int:
        transform_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    mapped = map_source(source, block)
    consuming = asyncio.ensure_future(anext(aiter(mapped)))
    await transform_started.wait()
    consuming.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consuming
    assert source.close_calls == 0
    await mapped.aclose()
    assert source.close_calls == 1


async def test_map_source_close_survives_caller_cancellation() -> None:
    """A cancelled waiter must not cancel or orphan the retained upstream close."""

    source = _BlockingCloseSource()
    mapped = map_source(source, lambda item: item)
    closing = asyncio.create_task(mapped.aclose())
    await source.close_started.wait()

    closing.cancel("caller stopped waiting")
    await asyncio.sleep(0)
    assert not closing.done()
    source.close_release.set()
    with pytest.raises(asyncio.CancelledError, match="caller stopped waiting"):
        await closing

    await mapped.aclose()
    assert source.close_calls == 1


async def test_map_source_transforms_normal_events_and_cancel_tail_identically() -> (
    None
):
    """A mapped source must expose one callback whose tail uses the same transform."""

    source = _CancellableTrackedSource(1, tail=(9,))
    transformed: list[int] = []

    async def transform(item: int) -> str:
        transformed.append(item)
        await asyncio.sleep(0)
        return f"mapped:{item}"

    mapped = map_source(source, transform)
    assert_type(mapped, CancellableMessageSource[str])
    assert await anext(aiter(mapped)) == "mapped:1"
    callback = cast(
        Callable[[CancelContext], Awaitable[Iterable[str] | None]],
        cast(CancellableMessageSource[str], mapped).messaging_cancel_callback,
    )
    assert callback is not None
    tail = await callback(CancelContext(channel="events", identity=_identity()))

    assert tuple(tail or ()) == ("mapped:9",)
    assert transformed == [1, 9]
    await mapped.aclose()


async def test_map_source_uses_a_synchronous_transform_for_cancel_tail() -> None:
    source = _CancellableTrackedSource(tail=(9,))
    mapped = map_source(source, lambda item: f"mapped:{item}")
    assert_type(mapped, CancellableMessageSource[str])
    callback = cast(
        Callable[[CancelContext], Awaitable[Iterable[str] | None]],
        cast(CancellableMessageSource[str], mapped).messaging_cancel_callback,
    )

    tail = await callback(CancelContext(channel="events", identity=_identity()))

    assert tuple(tail or ()) == ("mapped:9",)
    await mapped.aclose()


async def test_map_source_discards_the_whole_cancel_tail_on_transform_failure() -> None:
    """A failed tail transform must not return a partially transformed iterable."""

    source = _CancellableTrackedSource(tail=(8, 9))

    def transform(item: int) -> str:
        if item == 9:
            raise ValueError("cannot transform cancellation tail")
        return f"mapped:{item}"

    mapped = map_source(source, transform)
    callback = cast(
        Callable[[CancelContext], Awaitable[Iterable[str] | None]],
        cast(CancellableMessageSource[str], mapped).messaging_cancel_callback,
    )
    assert callback is not None

    with pytest.raises(ValueError, match="cancellation tail"):
        await callback(CancelContext(channel="events", identity=_identity()))

    await mapped.aclose()


async def test_mapping_keeps_upstream_iteration_on_its_consumer_task() -> None:
    """Upstream cancellation owners and context must survive across yielded items."""
    resumed, release = asyncio.Event(), asyncio.Event()
    consumers: list[asyncio.Task[object] | None] = []

    async def events() -> AsyncGenerator[int, None]:
        consumers.append(asyncio.current_task())
        yield 1
        consumers.append(asyncio.current_task())
        resumed.set()
        await release.wait()
        yield 2

    source = map_source(events(), lambda value: value)

    async def consume() -> list[int]:
        return [item async for item in source]

    consuming = asyncio.create_task(consume())
    try:
        await resumed.wait()
        assert consumers == [consuming, consuming]
    finally:
        release.set()
        await consuming
        await source.aclose()
