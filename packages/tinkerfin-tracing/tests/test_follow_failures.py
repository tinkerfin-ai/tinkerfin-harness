"""Public history and graph followers retain operation and settlement failures."""

import asyncio
from collections.abc import AsyncGenerator

import pytest
from test_follow_notifications import _write_run
from test_store_task_ownership import _contains, _ProcessStop

from tinkerfin_contracts import ThreadIdentity
from tinkerfin_tracing import (
    InMemoryTraceStore,
    Tracer,
    TraceStoreUpdate,
    TraceThreadKey,
)


@pytest.mark.parametrize("view_kind", ["history", "graph"])
@pytest.mark.parametrize("body_kind", ["normal", "error", "cancel", "control"])
async def test_follow_scope_preserves_independent_failure_objects(
    monkeypatch: pytest.MonkeyPatch, view_kind: str, body_kind: str
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    identity = ThreadIdentity(namespace="test", thread_id="follow-failures")
    await _write_run(store, thread=identity.thread_id, run="first")
    view = (
        await tracer.get(identity)
        if view_kind == "history"
        else await tracer.query(identity)
    )
    source_follow = store.follow
    cleanup = OSError("reader settlement failed")
    cleanup_cause = RuntimeError("reader original cause")
    cleanup.__cause__ = cleanup_cause
    primary = {
        "normal": None,
        "error": ValueError("business projection failed"),
        "cancel": asyncio.CancelledError("consumer cancelled"),
        "control": _ProcessStop("consumer stopped"),
    }[body_kind]
    original_cause = LookupError("business original cause")
    if primary is not None:
        primary.__cause__ = original_cause

    async def followed(
        key: TraceThreadKey, *, after_seq: int
    ) -> AsyncGenerator[TraceStoreUpdate, None]:
        source = source_follow(key, after_seq=after_seq)
        try:
            async for update in source:
                yield update
        finally:
            await source.aclose()
            raise cleanup

    monkeypatch.setattr(store, "follow", followed)
    await _write_run(store, thread=identity.thread_id, run="second")
    expected = cleanup if primary is None else primary
    with pytest.raises(type(expected)) as caught:
        async with view.follow() as updates:
            await anext(updates)
            if primary is not None:
                raise primary
    assert caught.value is expected
    assert _contains(caught.value, cleanup)
    assert _contains(caught.value, cleanup_cause)
    if primary is not None:
        assert _contains(caught.value, original_cause)


@pytest.mark.parametrize("cancel_close", [False, True])
async def test_concurrent_close_waits_for_reader_not_consumer_business(
    monkeypatch: pytest.MonkeyPatch, cancel_close: bool
) -> None:
    store = InMemoryTraceStore()
    identity = ThreadIdentity(namespace="test", thread_id="owned-pull")
    await _write_run(store, thread=identity.thread_id, run="first")
    trace = await Tracer(store=store).get(identity)
    original = store.follow
    source_started = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    source_closed = asyncio.Event()
    business_started = asyncio.Event()
    release_business = asyncio.Event()
    cancellations = []

    async def source(
        key: TraceThreadKey, *, after_seq: int
    ) -> AsyncGenerator[TraceStoreUpdate, None]:
        source_started.set()
        try:
            await asyncio.Event().wait()
            async for update in original(key, after_seq=after_seq):
                yield update
        finally:
            cleaning.set()
            await release_cleanup.wait()
            source_closed.set()

    monkeypatch.setattr(store, "follow", source)
    follower = trace.follow()

    async def consume() -> None:
        try:
            await anext(follower)
        except asyncio.CancelledError:
            cancellations.append("reader")
            business_started.set()
            # The host may continue after detaching its reader. Closing the
            # reader must not cancel or await this unrelated business work.
            await release_business.wait()

    consumer = asyncio.create_task(consume())
    close_tasks: list[asyncio.Task[None]] = []
    try:
        await source_started.wait()
        close_tasks.append(asyncio.create_task(follower.aclose()))
        await cleaning.wait()
        close_tasks.append(asyncio.create_task(follower.aclose()))
        if cancel_close:
            for _ in range(2):
                close_tasks[0].cancel()
                delivered = asyncio.Event()
                asyncio.get_running_loop().call_soon(delivered.set)
                await delivered.wait()
        release_cleanup.set()
        results = await asyncio.gather(*close_tasks, return_exceptions=True)
        if cancel_close:
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert results[0] is None
        assert results[1] is None
        await business_started.wait()
        assert source_closed.is_set()
        assert not consumer.done()
        assert cancellations == ["reader"]
        release_business.set()
        await consumer
    finally:
        release_cleanup.set()
        release_business.set()
        for task in (*close_tasks, consumer):
            if not task.done():
                task.cancel()
        await asyncio.gather(*close_tasks, consumer, return_exceptions=True)
        await follower.aclose()


@pytest.mark.parametrize("retained_failure", [False, True])
@pytest.mark.parametrize("grouped", [False, True])
async def test_external_close_preserves_failures_in_cancellation_chain(
    monkeypatch: pytest.MonkeyPatch, retained_failure: bool, grouped: bool
) -> None:
    store = InMemoryTraceStore()
    await _write_run(store, thread="cancel-chain", run="first")
    trace = await Tracer(store=store).get(
        ThreadIdentity(namespace="test", thread_id="cancel-chain")
    )
    entered = asyncio.Event()
    cause = (
        OSError("reader cleanup failed")
        if retained_failure
        else asyncio.CancelledError("upstream read cancelled")
    )
    retained = (
        BaseExceptionGroup("reader cancellation", [cause, asyncio.CancelledError()])
        if grouped
        else cause
    )
    original = store.follow

    async def source(
        key: TraceThreadKey, *, after_seq: int
    ) -> AsyncGenerator[TraceStoreUpdate, None]:
        entered.set()
        try:
            await asyncio.Event().wait()
            async for update in original(key, after_seq=after_seq):
                yield update
        except asyncio.CancelledError as error:
            raise error from retained

    monkeypatch.setattr(store, "follow", source)
    follower = trace.follow()
    pending = asyncio.create_task(anext(follower))
    try:
        await entered.wait()
        if retained_failure:
            with pytest.raises(asyncio.CancelledError) as closed:
                await follower.aclose()
            assert _contains(closed.value, cause)
        else:
            await follower.aclose()
        with pytest.raises(asyncio.CancelledError) as consumed:
            await pending
        assert _contains(consumed.value, cause)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await follower.aclose()
