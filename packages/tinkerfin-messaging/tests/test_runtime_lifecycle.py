"""Messaging, source, producer, and subscription lifecycle contracts."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import replace
from typing import ClassVar, Literal, cast

import pytest

import tinkerfin_messaging
from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    BackendOwnershipLost,
    MemoryBackend,
    MessageEnvelope,
    MessageSubscription,
    Messaging,
    MessagingClosed,
    MessagingNotStarted,
    RecoverableMessage,
    RecoveryCheckpoint,
    RunProducerFailed,
)
from tinkerfin_messaging._messaging_ledger import BackendRunHandle
from tinkerfin_messaging.backend_contract import (
    MessagingBackend,
    MessagingBackendSettings,
    MessagingTransition,
    MessagingTransitionResult,
)


def _identity(*, run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="conversation-1", run_id=run_id)


class _TextCodec:
    codec_id: ClassVar[str] = "test.text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _Source:
    def __init__(
        self,
        *items: str,
        release: asyncio.Event | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.items = items
        self.release = release
        self.error = error
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for item in self.items:
                yield item
            if self.release is not None:
                await self.release.wait()
            if self.error is not None:
                raise self.error

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()
        self.closed.set()


class _TrackingIterator:
    def __init__(
        self,
        iterator: AsyncIterator[MessageEnvelope],
        backend: _TrackingBackend,
    ) -> None:
        self._iterator = iterator
        self._backend = backend
        self._closed = False

    def __aiter__(self) -> _TrackingIterator:
        return self

    async def __anext__(self) -> MessageEnvelope:
        return await anext(self._iterator)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._backend.follow_close_calls += 1
        close = getattr(self._iterator, "aclose", None)
        if close is not None:
            await close()


class _TrackingBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.follow_close_calls = 0


class _BlockingCloseIterator(_TrackingIterator):
    def __init__(
        self,
        iterator: AsyncIterator[MessageEnvelope],
        backend: _BlockingFollowerBackend,
    ) -> None:
        super().__init__(iterator, backend)
        self._blocking_backend = backend

    async def aclose(self) -> None:
        self._blocking_backend.close_started.set()
        await self._blocking_backend.close_release.wait()
        await super().aclose()
        self._blocking_backend.close_finished.set()


class _BlockingFollowerBackend(_TrackingBackend):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_finished = asyncio.Event()


def _install_tracking_follow(
    messaging: Messaging,
    backend: _TrackingBackend,
    *,
    block_close: bool = False,
) -> None:
    original_follow = messaging._runtime_backend.follow

    def tracked_follow(
        handle: BackendRunHandle,
        *,
        after: int,
    ) -> AsyncIterator[MessageEnvelope]:
        iterator = original_follow(handle, after=after)
        if block_close:
            assert isinstance(backend, _BlockingFollowerBackend)
            return _BlockingCloseIterator(iterator, backend)
        return _TrackingIterator(iterator, backend)

    setattr(messaging._runtime_backend, "follow", tracked_follow)


class _LeasedMemoryBackend(MemoryBackend):
    @property
    def messaging_settings(self) -> MessagingBackendSettings:
        return replace(
            super().messaging_settings,
            producer_renew_interval_seconds=0.01,
            producer_lease_seconds=0.03,
        )


class _DiagnosticLeaseBackend(MemoryBackend):
    def __init__(
        self,
        *,
        behavior: Literal["exception", "reject", "success"],
        timeout: float = 0.03,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self.behavior = behavior
        self.timeout = timeout
        self.clock = clock
        self.expires_at = self.clock() + timeout
        self.renew_calls = 0

    @property
    def messaging_settings(self) -> MessagingBackendSettings:
        return replace(
            super().messaging_settings,
            producer_renew_interval_seconds=self.timeout / 3,
            producer_lease_seconds=self.timeout,
        )

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if transition.kind == "renew_producer_ownership":
            self.renew_calls += 1
            if self.behavior == "exception":
                raise RuntimeError("diagnostic backend renew failed")
            if self.behavior == "reject" or self.clock() >= self.expires_at:
                return MessagingTransitionResult(
                    kind=transition.kind,
                    producer_ownership_confirmed=False,
                )
            self.expires_at = self.clock() + self.timeout
        result = await super().commit_messaging_transition(transition)
        if transition.kind == "prepare_run" and result.is_producer_owner:
            self.expires_at = self.clock() + self.timeout
        return result


class _BlockingEventLoopSource(_Source):
    def __init__(self, *, block_seconds: float) -> None:
        super().__init__(release=asyncio.Event())
        self.block_seconds = block_seconds

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            time.sleep(self.block_seconds)
            assert self.release is not None
            await self.release.wait()
            if False:  # pragma: no cover - preserves the async generator shape
                yield "unreachable"

        self._iterator = iterate()
        return self._iterator


class _BlockingPrepareBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if transition.kind == "prepare_run":
            self.entered.set()
            await self.release.wait()
        return await super().commit_messaging_transition(transition)


class _StorageSetupBackend(MemoryBackend):
    def __init__(self, *, fail_first: bool = False) -> None:
        super().__init__()
        self.fail_first = fail_first
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled = False

    async def prepare_messaging_storage(self) -> None:
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.fail_first and self.calls == 1:
            raise RuntimeError("storage setup failed")
        self.finished.set()


class _CancellationResistantPrepareBackend(_BlockingPrepareBackend):
    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        try:
            return await super().commit_messaging_transition(transition)
        except asyncio.CancelledError:
            await self.release.wait()
            return await MemoryBackend.commit_messaging_transition(self, transition)


class _BlockingAppendBackend(MemoryBackend):
    def __init__(self, *, blocked_payload: bytes = b"one") -> None:
        super().__init__()
        self.blocked_payload = blocked_payload
        self.append_started = asyncio.Event()
        self.release_append = asyncio.Event()
        self.append_cancelled = asyncio.Event()

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if (
            transition.kind == "append_message"
            and transition.payload == self.blocked_payload
        ):
            self.append_started.set()
            try:
                await self.release_append.wait()
            except asyncio.CancelledError:
                self.append_cancelled.set()
                raise
        return await super().commit_messaging_transition(transition)


class _CountingBlockingAppendBackend(_BlockingAppendBackend):
    def __init__(self, *, blocked_payload: bytes = b"one") -> None:
        super().__init__(blocked_payload=blocked_payload)
        self.finish_calls = 0

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if transition.kind == "finish_run":
            self.finish_calls += 1
        return await super().commit_messaging_transition(transition)


class _FinishFailureBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.finish_started = asyncio.Event()
        self.release_finish = asyncio.Event()
        self.finish_calls = 0

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if transition.kind == "finish_run":
            self.finish_calls += 1
            self.finish_started.set()
            await self.release_finish.wait()
            raise BackendOwnershipLost("Producer lost ownership during finish")
        return await super().commit_messaging_transition(transition)


class _DelayedCancelObservationBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_is_durable = asyncio.Event()
        self.release_observer = asyncio.Event()


class _BlockingSettlementBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.settlement_entered = asyncio.Event()
        self.release_settlement = asyncio.Event()

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if transition.kind == "begin_settlement":
            self.settlement_entered.set()
            await self.release_settlement.wait()
        return await super().commit_messaging_transition(transition)


class _ClaimThenBlockSettlementBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_is_durable = asyncio.Event()
        self.release_observer = asyncio.Event()
        self.settlement_claimed = asyncio.Event()
        self.release_response = asyncio.Event()

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        result = await super().commit_messaging_transition(transition)
        if transition.kind == "begin_settlement":
            self.settlement_claimed.set()
            await self.release_response.wait()
        return result


def _install_delayed_cancel_observer(
    messaging: Messaging,
    backend: _DelayedCancelObservationBackend | _ClaimThenBlockSettlementBackend,
) -> None:
    original_wait = messaging._runtime_backend.wait_for_cancel

    async def delayed_wait(handle: BackendRunHandle) -> bool:
        requested = await original_wait(handle)
        if requested:
            backend.cancel_is_durable.set()
            await backend.release_observer.wait()
        return requested

    setattr(messaging._runtime_backend, "wait_for_cancel", delayed_wait)


class _RecoverableSource:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_calls = 0
        self.close_error = close_error

    def __aiter__(self) -> AsyncIterator[RecoverableMessage[str]]:
        async def iterate() -> AsyncGenerator[RecoverableMessage[str], None]:
            yield RecoverableMessage(
                message_id="stable-message-1",
                data="one",
                checkpoint=RecoveryCheckpoint(
                    position=b"1",
                    last_message_id="stable-message-1",
                ),
            )

        return iterate()

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _BlockingRecoveryFactory:
    def __init__(self, source: _RecoverableSource | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.source = source or _RecoverableSource()

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> _RecoverableSource:
        assert checkpoint is None
        self.entered.set()
        await self.release.wait()
        return self.source


class _CancellationResistantRecoveryFactory:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.source = _RecoverableSource()

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> _RecoverableSource:
        assert checkpoint is None
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return self.source
        raise AssertionError("unreachable")


async def _data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


def test_messaging_backend_is_read_only_after_construction() -> None:
    backend = MemoryBackend()
    messaging = Messaging(backend=backend)

    assert messaging.backend is backend
    with pytest.raises(AttributeError):
        setattr(messaging, "backend", MemoryBackend())


async def test_messaging_is_single_use_and_requires_an_open_lifecycle() -> None:
    messaging = Messaging()

    with pytest.raises(MessagingNotStarted):
        messaging.channel(name="events", codec=_TextCodec())

    async with messaging:
        messaging.channel(name="events", codec=_TextCodec())

    with pytest.raises(MessagingClosed):
        messaging.channel(name="events", codec=_TextCodec())
    with pytest.raises(MessagingClosed):
        await messaging.__aenter__()


async def test_storage_setup_survives_enter_caller_cancellation() -> None:
    backend = _StorageSetupBackend()
    messaging = Messaging(backend=backend)
    entering = asyncio.create_task(messaging.__aenter__())
    await asyncio.wait_for(backend.started.wait(), timeout=1)

    entering.cancel("caller stopped waiting for setup")
    with pytest.raises(
        asyncio.CancelledError, match="caller stopped waiting for setup"
    ):
        await entering
    assert backend.cancelled is False
    backend.release.set()
    await asyncio.wait_for(backend.finished.wait(), timeout=1)

    await messaging.__aenter__()
    assert backend.calls == 1
    await messaging.aclose()


async def test_concurrent_storage_setup_enter_is_rejected() -> None:
    backend = _StorageSetupBackend()
    messaging = Messaging(backend=backend)
    first_enter = asyncio.create_task(messaging.__aenter__())
    await asyncio.wait_for(backend.started.wait(), timeout=1)

    with pytest.raises(MessagingClosed, match="already open"):
        await messaging.__aenter__()

    backend.release.set()
    assert await asyncio.wait_for(first_enter, timeout=1) is messaging
    assert backend.calls == 1
    await messaging.aclose()


async def test_close_during_storage_setup_prevents_reopening() -> None:
    backend = _StorageSetupBackend()
    messaging = Messaging(backend=backend)
    entering = asyncio.create_task(messaging.__aenter__())
    await asyncio.wait_for(backend.started.wait(), timeout=1)

    closing = asyncio.create_task(messaging.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    assert not entering.done()

    backend.release.set()
    with pytest.raises(MessagingClosed, match="closed during storage preparation"):
        await asyncio.wait_for(entering, timeout=1)
    await asyncio.wait_for(closing, timeout=1)

    assert backend.cancelled is False
    assert backend.finished.is_set()
    with pytest.raises(MessagingClosed):
        await messaging.__aenter__()


async def test_storage_setup_failure_can_retry_on_the_next_enter() -> None:
    backend = _StorageSetupBackend(fail_first=True)
    backend.release.set()
    messaging = Messaging(backend=backend)

    with pytest.raises(RuntimeError, match="storage setup failed"):
        await messaging.__aenter__()
    await messaging.__aenter__()

    assert backend.calls == 2
    await messaging.aclose()


async def test_backend_subscription_closes_after_normal_completion() -> None:
    backend = _TrackingBackend()
    source = _Source("one", "two")

    async with Messaging(backend=backend) as messaging:
        _install_tracking_follow(messaging, backend)
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )
        assert await _data(subscription) == ["one", "two"]

    assert backend.follow_close_calls == 1


async def test_backend_subscription_closes_on_early_detach() -> None:
    backend = _TrackingBackend()
    release = asyncio.Event()
    source = _Source("one", release=release)

    async with Messaging(backend=backend) as messaging:
        _install_tracking_follow(messaging, backend)
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "one"

        await subscription.aclose()
        assert backend.follow_close_calls == 1
        assert not source.closed.is_set()
        release.set()
        await asyncio.wait_for(source.closed.wait(), timeout=1)


async def test_subscription_close_survives_caller_cancellation() -> None:
    """A cancelled waiter must not orphan or forget the backend follower close."""

    backend = _BlockingFollowerBackend()
    release = asyncio.Event()
    source = _Source("one", release=release)

    async with Messaging(backend=backend) as messaging:
        _install_tracking_follow(messaging, backend, block_close=True)
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(source, identity=_identity(), after=0)
        assert (await anext(aiter(subscription))).data == "one"

        closing = asyncio.create_task(subscription.aclose())
        await asyncio.wait_for(backend.close_started.wait(), timeout=1)
        closing.cancel("subscriber stopped waiting")
        await asyncio.sleep(0)
        assert not closing.done()
        backend.close_release.set()
        with pytest.raises(asyncio.CancelledError, match="subscriber stopped waiting"):
            await asyncio.wait_for(closing, timeout=1)

        await subscription.aclose()
        assert backend.close_finished.is_set()
        assert backend.follow_close_calls == 1
        release.set()
        await asyncio.wait_for(source.closed.wait(), timeout=1)


async def test_never_iterated_subscription_closes_without_claiming_delivery() -> None:
    backend = _TrackingBackend()
    source = _Source("one")

    async with Messaging(backend=backend) as messaging:
        _install_tracking_follow(messaging, backend)
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )

        await subscription.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            aiter(subscription)

    assert backend.follow_close_calls == 0
    assert source.close_calls == 1


async def test_close_waiter_cancellation_settles_the_active_pull_and_backend() -> None:
    backend = _BlockingFollowerBackend()
    release = asyncio.Event()
    source = _Source("one", release=release)
    async with Messaging(backend=backend) as messaging:
        _install_tracking_follow(messaging, backend, block_close=True)
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(source, identity=_identity(), after=0)
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "one"
        pending = asyncio.ensure_future(anext(delivery))
        await asyncio.sleep(0)
        closing = asyncio.create_task(subscription.aclose())
        try:
            await asyncio.wait_for(backend.close_started.wait(), timeout=1)
            closing.cancel("stop waiting for close")
            await asyncio.sleep(0)
            closing.cancel("stop waiting again")
            assert not closing.done()
            backend.close_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, timeout=1)
            with pytest.raises(asyncio.CancelledError):
                await pending
            await subscription.aclose()
            assert backend.follow_close_calls == 1
            assert not source.closed.is_set()
        finally:
            backend.close_release.set()
            release.set()
            await asyncio.gather(closing, pending, return_exceptions=True)


async def test_messaging_close_settles_a_waiting_subscription() -> None:
    release = asyncio.Event()
    source = _Source("one", release=release)
    async with Messaging() as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(source, identity=_identity(), after=0)
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "one"
        pending = asyncio.ensure_future(anext(delivery))
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(messaging.aclose(), timeout=1)
            with pytest.raises(RunProducerFailed) as failed:
                await asyncio.wait_for(pending, timeout=1)
            assert isinstance(failed.value.cause, asyncio.CancelledError)
            await subscription.aclose()
            assert source.closed.is_set()
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_backend_subscription_closes_on_producer_failure() -> None:
    backend = _TrackingBackend()
    cause = RuntimeError("source failed")
    source = _Source("one", error=cause)

    async with Messaging(backend=backend) as messaging:
        _install_tracking_follow(messaging, backend)
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "one"
        with pytest.raises(RunProducerFailed):
            await anext(delivery)

    assert backend.follow_close_calls == 1


async def test_cooperative_source_silence_keeps_renewing_without_failure_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = 0.0
    renewed = asyncio.Event()

    class CooperativeBackend(_DiagnosticLeaseBackend):
        async def commit_messaging_transition(
            self, transition: MessagingTransition
        ) -> MessagingTransitionResult:
            nonlocal now
            if transition.kind == "renew_producer_ownership":
                now += 0.01
            result = await super().commit_messaging_transition(transition)
            if self.renew_calls >= 5:
                renewed.set()
            return result

    # Lease time follows acknowledged renewals, independent of CPU scheduling.
    backend = CooperativeBackend(behavior="success", clock=lambda: now)
    release = asyncio.Event()
    source = _Source(release=release)

    with caplog.at_level(logging.ERROR, logger="tinkerfin.messaging"):
        async with Messaging(backend=backend) as messaging:
            subscription = await messaging.channel(
                name="events",
                codec=_TextCodec(),
            ).wrap(
                source,
                identity=_identity(),
                after=0,
            )
            await asyncio.wait_for(source.started.wait(), timeout=1)
            await asyncio.wait_for(renewed.wait(), timeout=1)
            release.set()
            assert await _data(subscription) == []

    assert backend.renew_calls >= 5
    assert not any(
        record.getMessage() == "Messaging producer lease renewal failed"
        for record in caplog.records
    )
    assert not any(
        task.get_name().startswith("tinkerfin-messaging-lease:")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    )


@pytest.mark.parametrize(
    ("behavior", "outcome", "error_type"),
    [
        (
            "exception",
            "backend_exception",
            "UnexpectedMessagingBackendError",
        ),
        (
            "reject",
            "ownership_rejected",
            "BackendOwnershipLost",
        ),
    ],
)
async def test_lease_failure_log_classifies_backend_outcomes(
    caplog: pytest.LogCaptureFixture,
    behavior: Literal["exception", "reject"],
    outcome: str,
    error_type: str,
) -> None:
    backend = _DiagnosticLeaseBackend(behavior=behavior)
    source = _Source(release=asyncio.Event())

    with caplog.at_level(logging.ERROR, logger="tinkerfin.messaging"):
        async with Messaging(backend=backend) as messaging:
            subscription = await messaging.channel(
                name="events",
                codec=_TextCodec(),
            ).wrap(
                source,
                identity=_identity(),
                after=0,
            )
            with pytest.raises(RunProducerFailed):
                await asyncio.wait_for(anext(aiter(subscription)), timeout=1)

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "Messaging producer lease renewal failed"
    ]
    assert len(records) == 1
    record = records[0]
    fields = vars(record)
    assert fields["tinkerfin_renewal_phase"] == "owner"
    assert fields["tinkerfin_renewal_outcome"] == outcome
    assert fields["tinkerfin_attempt"] == 1
    assert fields["tinkerfin_deadline_elapsed"] is False
    assert fields["tinkerfin_error_type"] == error_type
    assert fields["tinkerfin_scheduler_delay_seconds"] >= 0
    assert fields["tinkerfin_command_duration_seconds"] >= 0
    assert fields["tinkerfin_seconds_since_last_success"] >= 0
    assert fields["tinkerfin_lease_timeout_seconds"] == backend.timeout
    assert record.exc_info is None
    assert "events" not in caplog.text
    assert "conversation-1" not in caplog.text
    assert "run-1" not in caplog.text
    assert all(
        key.startswith("tinkerfin_") for key in fields if key.startswith("tinkerfin")
    )
    assert "channel" not in fields
    assert "thread_id" not in fields
    assert "run_id" not in fields
    assert "owner_token" not in fields
    assert "fence" not in fields
    assert "payload" not in fields
    assert source.closed.is_set()
    assert not any(
        task.get_name().startswith("tinkerfin-messaging-lease:")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    )


async def test_event_loop_block_is_distinguished_from_on_time_renewal_rejection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _DiagnosticLeaseBackend(behavior="success")
    source = _BlockingEventLoopSource(block_seconds=0.08)

    with caplog.at_level(logging.ERROR, logger="tinkerfin.messaging"):
        async with Messaging(backend=backend) as messaging:
            subscription = await messaging.channel(
                name="events",
                codec=_TextCodec(),
            ).wrap(
                source,
                identity=_identity(),
                after=0,
            )
            with pytest.raises(RunProducerFailed):
                await asyncio.wait_for(anext(aiter(subscription)), timeout=1)

    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Messaging producer lease renewal failed"
    )
    fields = vars(record)
    assert fields["tinkerfin_renewal_outcome"] == "ownership_rejected"
    assert fields["tinkerfin_deadline_elapsed"] is True
    assert fields["tinkerfin_scheduler_delay_seconds"] >= backend.timeout
    assert fields["tinkerfin_seconds_since_last_success"] >= backend.timeout
    assert source.closed.is_set()


async def test_wrap_repeated_cancellation_settles_before_producer_start() -> None:
    backend = _CancellationResistantPrepareBackend()
    source = _Source("unused")
    not_started = 0

    async def delivery_not_started() -> None:
        nonlocal not_started
        assert source.close_calls == 1
        not_started += 1

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        wrapping = asyncio.create_task(
            channel.wrap(
                source,
                identity=_identity(),
                after=0,
                on_delivery_not_started=delivery_not_started,
            )
        )
        await asyncio.wait_for(backend.entered.wait(), timeout=1)
        wrapping.cancel()
        await asyncio.sleep(0)
        wrapping.cancel()
        await asyncio.sleep(0)
        assert not wrapping.done()
        backend.release.set()
        with pytest.raises(asyncio.CancelledError):
            await wrapping

    assert source.close_calls == 1
    assert not source.started.is_set()
    assert not_started == 1


async def test_shutdown_waits_for_inflight_prepare_and_rejects_late_producer() -> None:
    backend = _BlockingPrepareBackend()
    release = asyncio.Event()
    source = _Source("one", release=release)
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    wrapping = asyncio.create_task(
        channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )
    )
    await asyncio.wait_for(backend.entered.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    shutdown_waited = not closing.done()
    backend.release.set()
    wrap_result, _ = await asyncio.gather(
        wrapping,
        closing,
        return_exceptions=True,
    )
    release.set()
    if isinstance(wrap_result, MessageSubscription):
        await _data(wrap_result)

    assert shutdown_waited
    assert isinstance(wrap_result, MessagingClosed)
    assert source.close_calls == 1


async def test_shutdown_waits_for_recoverable_open_and_rejects_late_producer() -> None:
    factory = _BlockingRecoveryFactory()
    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    wrapping = asyncio.create_task(
        channel.wrap_recoverable(
            factory,
            identity=_identity(),
            after=0,
        )
    )
    await asyncio.wait_for(factory.entered.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    shutdown_waited = not closing.done()
    factory.release.set()
    wrap_result, _ = await asyncio.gather(
        wrapping,
        closing,
        return_exceptions=True,
    )
    if isinstance(wrap_result, MessageSubscription):
        await _data(wrap_result)

    assert shutdown_waited
    assert isinstance(wrap_result, MessagingClosed)
    assert factory.source.close_calls == 1


async def test_recoverable_preflight_settles_owner_when_source_close_fails() -> None:
    backend = MemoryBackend()
    close_error = RuntimeError("cannot close rebuilt source")
    source = _RecoverableSource(close_error=close_error)
    factory = _BlockingRecoveryFactory(source)
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    wrapping = asyncio.create_task(
        channel.wrap_recoverable(
            factory,
            identity=_identity(),
            after=0,
        )
    )
    await asyncio.wait_for(factory.entered.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    factory.release.set()
    wrap_result, _ = await asyncio.gather(
        wrapping,
        closing,
        return_exceptions=True,
    )

    async with Messaging(backend=backend) as observer:
        resumed = await observer.channel(
            name="events",
            codec=_TextCodec(),
        ).wrap(
            _Source("next-run"),
            identity=_identity(run_id="run-2"),
            after=None,
        )
        assert await _data(resumed) == ["next-run"]

    assert isinstance(wrap_result, MessagingClosed)
    assert any(
        "cannot close rebuilt source" in note
        for note in getattr(wrap_result, "__notes__", ())
    )
    assert source.close_calls == 1


async def test_cancelled_recoverable_open_closes_a_late_returned_source() -> None:
    factory = _CancellationResistantRecoveryFactory()

    async with Messaging(backend=_LeasedMemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        wrapping = asyncio.create_task(
            channel.wrap_recoverable(
                factory,
                identity=_identity(),
                after=0,
            )
        )
        await asyncio.wait_for(factory.entered.wait(), timeout=1)
        wrapping.cancel()

        with pytest.raises(asyncio.CancelledError):
            await wrapping

    assert factory.source.close_calls == 1


async def test_messaging_shutdown_closes_active_source_and_owned_tasks(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    source = _Source("one", release=release)
    messaging = Messaging(backend=messaging_backend, settlement_timeout=2)

    async def cancel() -> None:
        release.set()

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    await channel.wrap(
        source,
        identity=_identity(),
        after=0,
        cancel=cancel,
    )
    await asyncio.wait_for(source.started.wait(), timeout=1)
    await messaging.__aexit__(None, None, None)

    await asyncio.sleep(0)
    live_names = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    assert source.close_calls == 1
    assert not any(name.startswith("tinkerfin-messaging-") for name in live_names)


async def test_immediate_shutdown_after_wrap_closes_source_and_settles_run() -> None:
    backend = MemoryBackend()
    source = _Source("one", release=asyncio.Event())
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    await channel.wrap(
        source,
        identity=_identity(),
        after=0,
    )

    await messaging.__aexit__(None, None, None)

    assert source.close_calls == 1
    status = await asyncio.wait_for(
        messaging._runtime_backend.wait_finished(
            BackendRunHandle(
                channel="events",
                identity=_identity(),
                owner_token=None,
                fence=None,
            )
        ),
        timeout=1,
    )
    assert status == "failed"


async def test_shutdown_waits_for_an_accepted_cancel_callback_tail(
    messaging_backend: MessagingBackend,
) -> None:
    source_release = asyncio.Event()
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()
    source = _Source("one", release=source_release)
    messaging = Messaging(backend=messaging_backend)

    async def cancel() -> tuple[str, ...]:
        callback_started.set()
        await callback_release.wait()
        source_release.set()
        return ("cancelled-tail",)

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        identity=_identity(),
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
    await asyncio.wait_for(callback_started.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    shutdown_waited = not closing.done()
    callback_release.set()
    cancel_result = await asyncio.wait_for(cancelling, timeout=1)
    await asyncio.wait_for(closing, timeout=1)
    replay = [first.data, *[message.data async for message in delivery]]

    assert shutdown_waited
    assert cancel_result is True
    assert replay == ["one", "cancelled-tail"]
    assert source.close_calls == 1


async def test_shutdown_honors_durable_cancel_before_watcher_returns() -> None:
    backend = _DelayedCancelObservationBackend()
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    callback_calls = 0
    messaging = Messaging(backend=backend)

    async def cancel() -> tuple[str, ...]:
        nonlocal callback_calls
        callback_calls += 1
        source_release.set()
        return ("cancelled-tail",)

    await messaging.__aenter__()
    _install_delayed_cancel_observer(messaging, backend)
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        identity=_identity(),
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
    await asyncio.wait_for(backend.cancel_is_durable.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    status = await asyncio.wait_for(
        messaging._runtime_backend.wait_finished(
            BackendRunHandle(
                channel="events",
                identity=_identity(),
                owner_token=None,
                fence=None,
            )
        ),
        timeout=1,
    )
    backend.release_observer.set()
    cancel_result = await asyncio.wait_for(cancelling, timeout=1)
    await asyncio.wait_for(closing, timeout=1)
    replay = [first.data, *[message.data async for message in delivery]]

    assert status == "cancelled"
    assert cancel_result is True
    assert callback_calls == 1
    assert replay == ["one", "cancelled-tail"]
    assert source.close_calls == 1


async def test_cancel_callback_starts_only_after_settlement_is_claimed() -> None:
    backend = _BlockingSettlementBackend()
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    callback_calls = 0

    async def cancel() -> tuple[str, ...]:
        nonlocal callback_calls
        callback_calls += 1
        source_release.set()
        return ("cancelled-tail",)

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel,
        )
        delivery = aiter(subscription)
        first = await anext(delivery)
        cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
        await asyncio.wait_for(backend.settlement_entered.wait(), timeout=1)
        calls_before_claim = callback_calls
        backend.release_settlement.set()
        cancel_result = await asyncio.wait_for(cancelling, timeout=1)
        replay = [first.data, *[message.data async for message in delivery]]

    assert calls_before_claim == 0
    assert cancel_result is True
    assert callback_calls == 1
    assert replay == ["one", "cancelled-tail"]


async def test_cancelled_shutdown_finishes_a_claimed_settlement() -> None:
    backend = _ClaimThenBlockSettlementBackend()
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    callback_calls = 0
    messaging = Messaging(backend=backend)

    async def cancel() -> tuple[str, ...]:
        nonlocal callback_calls
        callback_calls += 1
        source_release.set()
        return ("cancelled-tail",)

    await messaging.__aenter__()
    _install_delayed_cancel_observer(messaging, backend)
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        identity=_identity(),
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
    await asyncio.wait_for(backend.cancel_is_durable.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.wait_for(backend.settlement_claimed.wait(), timeout=1)
    closing.cancel()
    done, _ = await asyncio.wait({closing}, timeout=0.05)
    closed_before_release = closing in done
    backend.release_response.set()
    backend.release_observer.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    cancel_result = await asyncio.gather(cancelling, return_exceptions=True)
    replay_failure: RunProducerFailed | None = None
    replay = [first.data]
    try:
        replay.extend([message.data async for message in delivery])
    except RunProducerFailed as failure:
        replay_failure = failure

    live_names = {task.get_name() for task in asyncio.all_tasks() if not task.done()}
    assert not closed_before_release
    assert cancel_result == [True]
    assert replay_failure is None
    assert callback_calls == 1
    assert replay == ["one", "cancelled-tail"]
    assert source.close_calls == 1
    assert not any(name.startswith("tinkerfin-messaging-") for name in live_names)


async def test_messaging_shutdown_cancels_an_inflight_backend_append() -> None:
    backend = _BlockingAppendBackend()
    source = _Source("one", release=asyncio.Event())
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    await channel.wrap(
        source,
        identity=_identity(),
        after=0,
    )
    await asyncio.wait_for(backend.append_started.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    done, _ = await asyncio.wait({closing}, timeout=0.1)
    closed_without_backend_release = closing in done
    if not closed_without_backend_release:
        backend.release_append.set()
    await asyncio.wait_for(closing, timeout=1)

    assert closed_without_backend_release
    assert backend.append_cancelled.is_set()
    assert source.close_calls == 1


async def test_messaging_shutdown_waits_for_cancel_tail_settlement() -> None:
    backend = _BlockingAppendBackend(blocked_payload=b"cancelled-tail")
    release_source = asyncio.Event()
    source = _Source("one", release=release_source)
    messaging = Messaging(backend=backend)

    async def cancel() -> tuple[str, ...]:
        release_source.set()
        return ("cancelled-tail",)

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        identity=_identity(),
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    assert (await anext(delivery)).data == "one"
    cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
    await asyncio.wait_for(backend.append_started.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    shutdown_waited = not closing.done()
    backend.release_append.set()
    if shutdown_waited:
        assert await asyncio.wait_for(cancelling, timeout=1) is True
    else:
        cancelling.cancel()
        await asyncio.gather(cancelling, return_exceptions=True)
        orphan_committers = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "tinkerfin-messaging-committer:run-1"
        ]
        for task in orphan_committers:
            task.cancel()
        await asyncio.gather(*orphan_committers, return_exceptions=True)
    await asyncio.wait_for(closing, timeout=1)

    assert shutdown_waited
    assert source.close_calls == 1


@pytest.mark.parametrize(
    ("value", "error_type"),
    (
        pytest.param(True, TypeError, id="boolean"),
        pytest.param("1", TypeError, id="string"),
        pytest.param(-0.01, ValueError, id="negative"),
        pytest.param(float("inf"), ValueError, id="infinity"),
        pytest.param(float("nan"), ValueError, id="nan"),
    ),
)
def test_messaging_rejects_invalid_settlement_timeout(
    value: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        Messaging(settlement_timeout=cast(float | None, value))


@pytest.mark.parametrize("value", (None, 0, 0.01))
def test_messaging_accepts_non_negative_settlement_timeout(
    value: float | None,
) -> None:
    Messaging(settlement_timeout=value)


async def test_finite_close_budget_keeps_cancel_tail_settlement_owned() -> None:
    backend = _CountingBlockingAppendBackend(blocked_payload=b"cancelled-tail")
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    messaging = Messaging(backend=backend, settlement_timeout=0.01)
    await messaging.__aenter__()

    async def cancel() -> tuple[str, ...]:
        source_release.set()
        return ("cancelled-tail",)

    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        identity=_identity(),
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
    await asyncio.wait_for(backend.append_started.wait(), timeout=1)
    timeout_type = getattr(
        tinkerfin_messaging,
        "MessagingSettlementTimeout",
        None,
    )
    assert timeout_type is not None

    try:
        with pytest.raises(timeout_type) as captured:
            await messaging.aclose()

        assert captured.value.timeout == 0.01
        assert not backend.append_cancelled.is_set()
        assert not cancelling.done()
        with pytest.raises(MessagingClosed):
            messaging.channel(name="closed", codec=_TextCodec())

        backend.release_append.set()
        assert await asyncio.wait_for(cancelling, timeout=1) is True
        await messaging.aclose()
        replay = [first.data, *[message.data async for message in delivery]]

        assert replay == ["one", "cancelled-tail"]
        assert source.close_calls == 1
        assert backend.finish_calls == 1
    finally:
        backend.release_append.set()
        await asyncio.gather(cancelling, return_exceptions=True)
        await asyncio.gather(messaging.aclose(), return_exceptions=True)


async def test_default_close_budget_waits_for_complete_settlement() -> None:
    backend = _CountingBlockingAppendBackend(blocked_payload=b"cancelled-tail")
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()

    async def cancel() -> tuple[str, ...]:
        source_release.set()
        return ("cancelled-tail",)

    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        identity=_identity(),
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
    await asyncio.wait_for(backend.append_started.wait(), timeout=1)
    closing = asyncio.create_task(messaging.aclose())

    try:
        done, _ = await asyncio.wait({closing}, timeout=0.05)
        assert closing not in done
        assert not backend.append_cancelled.is_set()

        backend.release_append.set()
        assert await asyncio.wait_for(cancelling, timeout=1) is True
        await asyncio.wait_for(closing, timeout=1)
        replay = [first.data, *[message.data async for message in delivery]]

        assert replay == ["one", "cancelled-tail"]
        assert source.close_calls == 1
        assert backend.finish_calls == 1
    finally:
        backend.release_append.set()
        await asyncio.gather(cancelling, closing, return_exceptions=True)


async def test_close_only_failure_propagates_and_is_idempotent() -> None:
    backend = _FinishFailureBackend()
    source = _Source("one", release=asyncio.Event())
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    await messaging.channel(name="events", codec=_TextCodec()).wrap(
        source,
        identity=_identity(),
        after=0,
    )
    await asyncio.wait_for(source.started.wait(), timeout=1)
    backend.release_finish.set()

    with pytest.raises(BackendOwnershipLost) as first:
        await messaging.aclose()
    with pytest.raises(BackendOwnershipLost) as second:
        await messaging.aclose()

    assert second.value is first.value
    assert source.close_calls == 1
    assert backend.finish_calls == 1


@pytest.mark.parametrize(
    ("body_error", "error_type"),
    (
        pytest.param(ValueError("body failed"), ValueError, id="business-error"),
        pytest.param(
            asyncio.CancelledError("body cancelled"),
            asyncio.CancelledError,
            id="body-cancellation",
        ),
    ),
)
async def test_context_body_failure_outranks_close_failure(
    body_error: BaseException,
    error_type: type[BaseException],
) -> None:
    backend = _FinishFailureBackend()
    backend.release_finish.set()
    source = _Source("one", release=asyncio.Event())

    with pytest.raises(error_type) as captured:
        async with Messaging(backend=backend) as messaging:
            await messaging.channel(name="events", codec=_TextCodec()).wrap(
                source,
                identity=_identity(),
                after=0,
            )
            await asyncio.wait_for(source.started.wait(), timeout=1)
            raise body_error

    notes = "\n".join(getattr(captured.value, "__notes__", ()))
    assert captured.value is body_error
    assert "BackendOwnershipLost" in notes
    assert source.close_calls == 1
    assert backend.finish_calls == 1


async def test_caller_cancellation_outranks_late_close_failure() -> None:
    backend = _FinishFailureBackend()
    source = _Source("one", release=asyncio.Event())
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    await messaging.channel(name="events", codec=_TextCodec()).wrap(
        source,
        identity=_identity(),
        after=0,
    )
    await asyncio.wait_for(source.started.wait(), timeout=1)
    closing = asyncio.create_task(messaging.aclose())
    await asyncio.wait_for(backend.finish_started.wait(), timeout=1)

    closing.cancel("caller cancelled close")
    cancellation_delivered = asyncio.Event()
    asyncio.get_running_loop().call_soon(cancellation_delivered.set)
    await cancellation_delivered.wait()
    settled_before_finish = closing.done()
    backend.release_finish.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await closing

    assert not settled_before_finish
    pending: list[BaseException] = [captured.value]
    seen: set[int] = set()
    retained = False
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        retained = retained or isinstance(error, BackendOwnershipLost)
        pending.extend(
            item for item in (error.__cause__, error.__context__) if item is not None
        )
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
    assert retained
    assert source.close_calls == 1
    assert backend.finish_calls == 1
