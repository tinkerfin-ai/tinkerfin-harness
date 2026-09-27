"""摘要跟随关闭必须结算全部自有任务，并保留取消和清理失败"""

from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import MessageChannel
from tinkerfin_studio.conversation.coordinator import ConversationTraceCoordinator
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_tracing import Tracer, TraceThreadNotFound


@pytest.mark.parametrize("cancel_waiters", [False, True])
async def test_concurrent_closers_wait_for_follow_cleanup(
    notifications, cancel_waiters: bool
) -> None:
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = False

    async def read_trace(*_args: object, **_kwargs: object) -> None:
        nonlocal cleaned
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned = True

    tracer = create_autospec(Tracer, instance=True)
    tracer.get.side_effect = read_trace
    coordinator = ConversationTraceCoordinator(
        database=create_autospec(Database, instance=True),
        tracer=tracer,
        conversation_channel=create_autospec(MessageChannel, instance=True),
        notifications=notifications,
    )
    coordinator.ensure(
        thread_pk=1,
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run"),
    )
    await entered.wait()
    first = asyncio.create_task(coordinator.aclose())
    await cleaning.wait()
    second_entered = asyncio.Event()

    async def close_again() -> None:
        second_entered.set()
        await coordinator.aclose()

    second = asyncio.create_task(close_again())
    try:
        await second_entered.wait()
        assert not second.done()
        if cancel_waiters:
            first.cancel()
            second.cancel()
            # 两次取消交付之间使用调度标记，不依赖墙钟等待
            delivered = asyncio.Event()
            asyncio.get_running_loop().call_soon(delivered.set)
            await delivered.wait()
            first.cancel()
            second.cancel()
        assert not cleaned
        assert not first.done()
        assert not second.done()
    finally:
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert cleaned
    if cancel_waiters:
        assert all(isinstance(result, asyncio.CancelledError) for result in outcomes)
    else:
        assert outcomes == [None, None]
    await coordinator.aclose()


@pytest.mark.parametrize("failure_type", [OSError, TraceThreadNotFound])
async def test_close_retains_follow_cleanup_failure_on_repeated_calls(
    notifications,
    failure_type: type[OSError] | type[TraceThreadNotFound],
) -> None:
    entered = asyncio.Event()
    failure = failure_type("trace cleanup failed")
    calls = 0

    async def read_trace(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise failure

    tracer = create_autospec(Tracer, instance=True)
    tracer.get.side_effect = read_trace
    coordinator = ConversationTraceCoordinator(
        database=create_autospec(Database, instance=True),
        tracer=tracer,
        conversation_channel=create_autospec(MessageChannel, instance=True),
        notifications=notifications,
    )
    coordinator.ensure(
        thread_pk=1,
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run"),
    )
    await entered.wait()
    for _ in range(2):
        with pytest.raises(failure_type) as raised:
            await coordinator.aclose()
        assert raised.value is failure
    assert calls == 1


async def test_follow_callback_cannot_close_its_own_coordinator(
    notifications,
) -> None:
    rejected = asyncio.Event()

    async def read_trace(*_args: object, **_kwargs: object) -> None:
        with pytest.raises(RuntimeError, match="跟随任务"):
            await coordinator.aclose()
        rejected.set()
        await asyncio.Event().wait()

    tracer = create_autospec(Tracer, instance=True)
    tracer.get.side_effect = read_trace
    coordinator = ConversationTraceCoordinator(
        database=create_autospec(Database, instance=True),
        tracer=tracer,
        conversation_channel=create_autospec(MessageChannel, instance=True),
        notifications=notifications,
    )
    coordinator.ensure(
        thread_pk=1,
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run"),
    )
    await rejected.wait()
    await coordinator.aclose()


@pytest.mark.parametrize("cancel_close", [False, True])
async def test_close_keeps_all_cleanup_failures_after_settlement(
    notifications,
    cancel_close: bool,
) -> None:
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failures = (OSError("first cleanup"), ValueError("second cleanup"))
    started = 0
    finishing = 0

    async def read_trace(*_args: object, **_kwargs: object) -> None:
        nonlocal started, finishing
        failure = failures[started]
        started += 1
        if started == len(failures):
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finishing += 1
            if finishing == len(failures):
                cleaning.set()
            await release.wait()
            raise failure

    tracer = create_autospec(Tracer, instance=True)
    tracer.get.side_effect = read_trace
    coordinator = ConversationTraceCoordinator(
        database=create_autospec(Database, instance=True),
        tracer=tracer,
        conversation_channel=create_autospec(MessageChannel, instance=True),
        notifications=notifications,
    )
    for number in range(len(failures)):
        coordinator.ensure(
            thread_pk=number + 1,
            identity=RunIdentity(
                namespace="test", thread_id="thread", run_id=f"run-{number}"
            ),
        )
    await entered.wait()
    closer = asyncio.create_task(coordinator.aclose())
    await cleaning.wait()
    if cancel_close:
        closer.cancel()
    release.set()
    if cancel_close:
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await closer
        group = cancelled.value.__cause__
        assert isinstance(group, ExceptionGroup)
    else:
        with pytest.raises(ExceptionGroup) as raised:
            await closer
        group = raised.value
    assert group.exceptions == failures
    with pytest.raises(ExceptionGroup) as repeated:
        await coordinator.aclose()
    assert repeated.value.exceptions == failures


@pytest.mark.parametrize("cancel_close", [False, True])
@pytest.mark.parametrize(
    "chain", ["cause", "context", "cycle", "pure_cycle", "pure_group"]
)
async def test_shutdown_preserves_cancellation_cleanup_evidence(
    notifications, cancel_close: bool, chain: str
) -> None:
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = OSError("cleanup evidence")
    owned_cancellation: asyncio.CancelledError | None = None
    linked_cancellation = asyncio.CancelledError("nested cancellation")
    has_failure = chain in {"cause", "context", "cycle"}

    async def read_trace(*_args: object, **_kwargs: object) -> None:
        nonlocal owned_cancellation
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as cancelled:
            owned_cancellation = cancelled
            if chain == "cause":
                cancelled.__cause__ = failure
            elif chain == "context":
                cancelled.__context__ = failure
            elif chain in {"cycle", "pure_cycle"}:
                cancelled.__cause__ = linked_cancellation
                linked_cancellation.__context__ = cancelled
                if has_failure:
                    linked_cancellation.__cause__ = failure
            else:
                cancelled.__cause__ = BaseExceptionGroup(
                    "owned cancellations", [linked_cancellation]
                )
            cleaning.set()
            await release.wait()
            raise

    tracer = create_autospec(Tracer, instance=True)
    tracer.get.side_effect = read_trace
    coordinator = ConversationTraceCoordinator(
        database=create_autospec(Database, instance=True),
        tracer=tracer,
        conversation_channel=create_autospec(MessageChannel, instance=True),
        notifications=notifications,
    )
    coordinator.ensure(
        thread_pk=1,
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run"),
    )
    await entered.wait()
    closer = asyncio.create_task(coordinator.aclose())
    await cleaning.wait()
    if cancel_close:
        closer.cancel()
    release.set()
    if cancel_close or has_failure:
        with pytest.raises(asyncio.CancelledError) as raised:
            await closer
        if has_failure:
            assert owned_cancellation is not None
            if cancel_close:
                assert raised.value.__cause__ is owned_cancellation
            else:
                assert raised.value is owned_cancellation
            if chain == "cause":
                assert owned_cancellation.__cause__ is failure
            elif chain == "context":
                assert owned_cancellation.__context__ is failure
            else:
                assert linked_cancellation.__cause__ is failure
    else:
        await closer
    if has_failure:
        with pytest.raises(asyncio.CancelledError) as repeated:
            await coordinator.aclose()
        assert repeated.value is owned_cancellation
    else:
        await coordinator.aclose()


async def test_empty_coordinator_close_is_repeatable(
    notifications,
) -> None:
    coordinator = ConversationTraceCoordinator(
        database=create_autospec(Database, instance=True),
        tracer=create_autospec(Tracer, instance=True),
        conversation_channel=create_autospec(MessageChannel, instance=True),
        notifications=notifications,
    )
    await asyncio.gather(coordinator.aclose(), coordinator.aclose())
    await coordinator.aclose()
