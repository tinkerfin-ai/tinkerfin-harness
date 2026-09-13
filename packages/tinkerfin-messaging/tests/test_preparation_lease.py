"""Ownership remains live across all source preparation stages."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import ClassVar, Generic, TypeVar

import pytest

from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_messaging import (
    BackendOwnershipLost,
    DeferredMessageSource,
    MemoryBackend,
    MessageSourceBinding,
    Messaging,
    MessagingClosed,
    RecoverableMessage,
    RecoveryCheckpoint,
)
from tinkerfin_messaging.backend_contract import (
    MessagingBackendSettings,
    MessagingTransition,
    MessagingTransitionResult,
)

T = TypeVar("T")


class _Source(Generic[T]):
    def __init__(self, iterator: AsyncGenerator[T, None]) -> None:
        self.iterator = iterator

    def __aiter__(self) -> AsyncIterator[T]:
        return self.iterator

    async def aclose(self) -> None:
        await self.iterator.aclose()


class _LeaseBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = 0

    @property
    def messaging_settings(self) -> MessagingBackendSettings:
        return replace(
            super().messaging_settings,
            producer_renew_interval_seconds=0.01,
            producer_lease_seconds=0.1,
        )

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        result = await super().commit_messaging_transition(transition)
        if transition.kind == "renew_producer_ownership":
            self.renewed += 1
        return result


class _TextCodec:
    codec_id: ClassVar[str] = "preparation.text"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


@pytest.mark.parametrize("stage", ["preflight", "opener", "ready", "stream"])
async def test_deferred_ownership_is_renewed_during_each_stage(stage: str) -> None:
    backend = _LeaseBackend()
    observed: list[int] = []

    async def delay(current: str) -> None:
        if stage == current:
            before = backend.renewed
            await asyncio.sleep(0.15)
            observed.append(backend.renewed - before)

    async def stream() -> AsyncGenerator[str, None]:
        await delay("stream")
        yield "ready"

    async def opener() -> MessageSourceBinding[str]:
        await delay("opener")
        return MessageSourceBinding(source=_Source(stream()))

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            DeferredMessageSource(
                opener,
                cancellable=False,
                on_owner_preflight=lambda: delay("preflight"),
            ),
            identity=RunIdentity(namespace="test", thread_id="thread", run_id="run"),
            after=0,
            on_source_ready=lambda: delay("ready"),
        )
        assert [message.data async for message in subscription] == ["ready"]
        assert observed[0] > 0
    count = backend.renewed
    await asyncio.sleep(0.03)
    assert backend.renewed == count


@pytest.mark.parametrize("stage", ["preflight", "opener", "ready"])
async def test_recoverable_ownership_is_renewed_during_each_stage(stage: str) -> None:
    backend = _LeaseBackend()
    observed: list[int] = []

    async def delay(current: str) -> None:
        if current == stage:
            before = backend.renewed
            await asyncio.sleep(0.15)
            observed.append(backend.renewed - before)

    async def stream() -> AsyncGenerator[RecoverableMessage[str], None]:
        yield RecoverableMessage(
            data="ready",
            message_id="one",
            checkpoint=RecoveryCheckpoint(position=b"one", last_message_id="one"),
        )

    class Factory:
        async def messaging_owner_preflight(self) -> None:
            await delay("preflight")

        async def open(
            self, checkpoint: RecoveryCheckpoint | None
        ) -> _Source[RecoverableMessage[str]]:
            assert checkpoint is None
            await delay("opener")
            return _Source(stream())

    async with Messaging(backend=backend) as messaging:
        subscription = await messaging.channel(
            name="events", codec=_TextCodec()
        ).wrap_recoverable(
            Factory(),
            identity=RunIdentity(namespace="test", thread_id="thread", run_id="run"),
            after=0,
            on_source_ready=lambda: delay("ready"),
        )
        assert [message.data async for message in subscription] == ["ready"]
        assert observed[0] > 0


@pytest.mark.parametrize("failure_stage", ["opener", "ready"])
async def test_preparation_failure_remains_failed_after_slow_cleanup(
    failure_stage: str,
) -> None:
    backend = _LeaseBackend()
    closed = asyncio.Event()
    identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")

    class Source:
        def __aiter__(self) -> AsyncIterator[str]:
            async def items() -> AsyncIterator[str]:
                yield "unused"

            return items()

        async def messaging_owner_preflight(self) -> None:
            if failure_stage == "opener":
                await asyncio.sleep(0.15)
                raise ValueError("preparation failed")

        async def aclose(self) -> None:
            before = backend.renewed
            await asyncio.sleep(0.15)
            assert backend.renewed > before
            closed.set()

    async def ready() -> None:
        await asyncio.sleep(0.15)
        raise ValueError("preparation failed")

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with pytest.raises(ValueError, match="preparation failed"):
            await channel.wrap(
                Source(), identity=identity, after=0, on_source_ready=ready
            )
        assert closed.is_set()
        assert await channel.get_run_status(identity=identity) == "failed"


async def test_caller_cancellation_during_deferred_preparation_is_propagated() -> None:
    backend = _LeaseBackend()
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def opener() -> MessageSourceBinding[str]:
        entered.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        finally:
            closed.set()

    async with Messaging(backend=backend) as messaging:
        operation = asyncio.create_task(
            messaging.channel(name="events", codec=_TextCodec()).wrap(
                DeferredMessageSource(opener, cancellable=False),
                identity=RunIdentity(
                    namespace="test", thread_id="thread", run_id="run"
                ),
                after=0,
            )
        )
        await entered.wait()
        await asyncio.sleep(0.04)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 1)
        assert closed.is_set()
    count = backend.renewed
    await asyncio.sleep(0.03)
    assert backend.renewed == count


async def test_managed_initialization_timeout_emits_one_failed_lifecycle() -> None:
    from ag_ui.core import RunErrorEvent, RunStartedEvent
    from deepagents.backends import StateBackend

    from tinkerfin import TinkerFin

    renewed = asyncio.Event()
    released = asyncio.Event()

    class Backend(_LeaseBackend):
        async def commit_messaging_transition(
            self, transition: MessagingTransition
        ) -> MessagingTransitionResult:
            result = await super().commit_messaging_transition(transition)
            if transition.kind == "renew_producer_ownership":
                renewed.set()
            return result

    class TimedOutWorkspace:
        @asynccontextmanager
        async def prepare(
            self, identity: RunIdentity
        ) -> AsyncIterator[PreparedWorkspace[None, StateBackend]]:
            assert identity.namespace == "test"
            try:
                await renewed.wait()
                raise TimeoutError("private infrastructure timeout")
                yield PreparedWorkspace(workspace=None, backend=StateBackend())
            finally:
                released.set()

    backend = Backend()
    runtime = (
        TinkerFin()
        .with_namespace("test")
        .build(model="provider:model", backend=TimedOutWorkspace())
    )
    identity = runtime.run_identity("thread", "run")
    source = runtime.open_agui_run(
        thread_id="thread",
        run_id="run",
        messages=[{"id": "user", "role": "user", "content": "hello"}],
    )
    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events")
        subscription = await channel.wrap(source, after=0)
        events = [message.data async for message in subscription]
        assert backend.renewed > 0
        assert released.is_set()
        assert sum(isinstance(event, RunStartedEvent) for event in events) == 1
        terminals = [event for event in events if isinstance(event, RunErrorEvent)]
        assert len(terminals) == 1
        assert terminals[0].code == "runtime_initialization_error"
        assert events[-1] == terminals[0]
        replay = await channel.follow(identity=identity, after=0)
        assert [message.data async for message in replay] == events


@pytest.mark.parametrize("recoverable", [False, True])
async def test_lease_loss_during_cancel_cleanup_does_not_interrupt_resources(
    recoverable: bool,
) -> None:
    fail = asyncio.Event()
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class Backend(_LeaseBackend):
        async def commit_messaging_transition(
            self, transition: MessagingTransition
        ) -> MessagingTransitionResult:
            if transition.kind == "renew_producer_ownership" and fail.is_set():
                raise BackendOwnershipLost("owner lost during cleanup")
            return await super().commit_messaging_transition(transition)

    async def cleanup() -> None:
        cleanup_started.set()
        await asyncio.sleep(0.08)
        cleanup_finished.set()

    class Source:
        def __aiter__(self) -> AsyncIterator[RecoverableMessage[str]]:
            async def messages() -> AsyncIterator[RecoverableMessage[str]]:
                if False:
                    yield RecoverableMessage(
                        data="unused",
                        message_id="unused",
                        checkpoint=RecoveryCheckpoint(
                            position=b"", last_message_id="unused"
                        ),
                    )

            return messages()

        async def aclose(self) -> None:
            await cleanup()

    class Factory:
        async def open(self, checkpoint: RecoveryCheckpoint | None) -> Source:
            assert checkpoint is None
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A third-party opener may finish acquisition concurrently with cancel.
                return Source()
            raise AssertionError("unreachable")

    async def hook() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await cleanup()

    async def opener() -> MessageSourceBinding[str]:
        raise AssertionError("cancelled preparation must not open its source")

    async with Messaging(backend=Backend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
        operation = asyncio.create_task(
            channel.wrap_recoverable(Factory(), identity=identity)
            if recoverable
            else channel.wrap(
                DeferredMessageSource(
                    opener, cancellable=False, on_owner_preflight=hook
                ),
                identity=identity,
            )
        )
        await entered.wait()
        operation.cancel()
        await cleanup_started.wait()
        fail.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert cleanup_finished.is_set()


async def test_recoverable_opener_can_close_its_leased_messaging_owner() -> None:
    messaging = Messaging(backend=_LeaseBackend())
    await messaging.__aenter__()
    closed = asyncio.Event()

    class Source:
        def __aiter__(self) -> AsyncIterator[RecoverableMessage[str]]:
            async def items() -> AsyncIterator[RecoverableMessage[str]]:
                if False:
                    yield RecoverableMessage(
                        data="unused",
                        message_id="unused",
                        checkpoint=RecoveryCheckpoint(
                            position=b"", last_message_id="unused"
                        ),
                    )

            return items()

        async def aclose(self) -> None:
            closed.set()

    class Factory:
        async def open(self, checkpoint: RecoveryCheckpoint | None) -> Source:
            assert checkpoint is None
            await messaging.aclose()
            return Source()

    with pytest.raises(MessagingClosed):
        await asyncio.wait_for(
            messaging.channel(name="events", codec=_TextCodec()).wrap_recoverable(
                Factory(),
                identity=RunIdentity(
                    namespace="test", thread_id="thread", run_id="run"
                ),
            ),
            1,
        )
    assert closed.is_set()


async def test_cancelled_recoverable_opener_can_close_messaging_during_cleanup() -> (
    None
):
    entered = asyncio.Event()
    cleanup_closed = asyncio.Event()

    async with Messaging(backend=_LeaseBackend(), settlement_timeout=0.1) as messaging:

        class Factory:
            async def open(
                self, checkpoint: RecoveryCheckpoint | None
            ) -> _Source[RecoverableMessage[str]]:
                assert checkpoint is None
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await messaging.aclose()
                    cleanup_closed.set()
                raise AssertionError("cancelled reconstruction cannot return a source")

        operation = asyncio.create_task(
            messaging.channel(name="events", codec=_TextCodec()).wrap_recoverable(
                Factory(),
                identity=RunIdentity(
                    namespace="test", thread_id="thread", run_id="run"
                ),
            )
        )
        await entered.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 1)
        assert cleanup_closed.is_set()


@pytest.mark.parametrize("recoverable", [False, True])
async def test_cancelled_preparation_retains_cleanup_failure_without_unhandled_error(
    recoverable: bool,
) -> None:
    entered = asyncio.Event()
    unhandled: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()

    def capture_unhandled(
        event_loop: asyncio.AbstractEventLoop, context: dict[str, object]
    ) -> None:
        assert event_loop is loop
        unhandled.append(context)

    async def prepare() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("private cleanup failure")

    class Factory:
        async def open(
            self, checkpoint: RecoveryCheckpoint | None
        ) -> _Source[RecoverableMessage[str]]:
            assert checkpoint is None
            await prepare()
            raise AssertionError("cancelled reconstruction cannot return a source")

    async def opener() -> MessageSourceBinding[str]:
        raise AssertionError("cancelled preparation must not open its source")

    loop.set_exception_handler(capture_unhandled)
    try:
        async with Messaging(backend=_LeaseBackend()) as messaging:
            channel = messaging.channel(name="events", codec=_TextCodec())
            identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
            operation = asyncio.create_task(
                channel.wrap_recoverable(Factory(), identity=identity)
                if recoverable
                else channel.wrap(
                    DeferredMessageSource(
                        opener, cancellable=False, on_owner_preflight=prepare
                    ),
                    identity=identity,
                )
            )
            await entered.wait()
            operation.cancel()
            with pytest.raises(asyncio.CancelledError) as captured:
                await asyncio.wait_for(operation, 1)
        await asyncio.sleep(0)
        assert unhandled == []
        assert any(
            "private cleanup failure" in note
            for note in getattr(captured.value, "__notes__", ())
        )
    finally:
        loop.set_exception_handler(original_handler)
