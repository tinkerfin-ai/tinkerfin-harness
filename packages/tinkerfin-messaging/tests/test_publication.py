"""External publication shares durable ordering without owning source progress."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from ag_ui.core import BaseEvent, CustomEvent, RunFinishedEvent, RunStartedEvent
from backend_harness import MessagingBackendHarness

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    AgUiCodec,
    MessageEnvelope,
    MessageIdConflict,
    Messaging,
    PublicationRejected,
)


async def test_publish_during_source_wait_is_replayed_and_idempotent(
    messaging_backend: MessagingBackendHarness,
) -> None:
    identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
    ready, release = asyncio.Event(), asyncio.Event()

    async def observe(envelope: MessageEnvelope) -> None:
        del envelope
        ready.set()

    async def source() -> AsyncGenerator[BaseEvent, None]:
        yield RunStartedEvent(thread_id="thread", run_id="run")
        await release.wait()
        yield RunFinishedEvent(thread_id="thread", run_id="run")

    async with Messaging(backend=messaging_backend.storage_backend) as messaging:
        channel = messaging.channel(name="events", codec=AgUiCodec())
        body = await channel.open_sse(
            source(), identity=identity, after=0, on_committed=observe
        )
        try:
            await ready.wait()
            event = CustomEvent(name="progress", value={"percent": 50})
            envelope = await channel.publish(
                event, identity=identity, message_id="progress"
            )
            assert envelope.seq == 2
            assert (
                await channel.publish(event, identity=identity, message_id="progress")
                == envelope
            )
            with pytest.raises(MessageIdConflict):
                await channel.publish(
                    CustomEvent(name="progress", value=100),
                    identity=identity,
                    message_id="progress",
                )
            with pytest.raises(PublicationRejected):
                await channel.publish(
                    RunFinishedEvent(thread_id="thread", run_id="run"),
                    identity=identity,
                )
            release.set()
            frames = [frame async for frame in body]
            assert [frame.splitlines()[0] for frame in frames] == [
                b"id: 1",
                b"id: 2",
                b"id: 3",
            ]
            assert b'"name":"progress"' in frames[1]
            assert (
                await channel.publish(event, identity=identity, message_id="progress")
                == envelope
            )
            with pytest.raises(PublicationRejected):
                await channel.publish(event, identity=identity)
            replay = await channel.follow(identity=identity, after=1)
            assert [item.envelope.seq async for item in replay] == [2, 3]
        finally:
            release.set()
            await body.aclose()


async def test_main_terminal_seals_publication_before_source_finishes(
    messaging_backend: MessagingBackendHarness,
) -> None:
    identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
    terminal, release = asyncio.Event(), asyncio.Event()

    async def observe(envelope: MessageEnvelope) -> None:
        if envelope.seq == 3:
            terminal.set()

    async def source() -> AsyncGenerator[BaseEvent, None]:
        yield RunStartedEvent(thread_id="thread", run_id="run")
        yield RunFinishedEvent(thread_id="thread", run_id="child")
        yield RunFinishedEvent(thread_id="thread", run_id="run")
        await release.wait()

    async with Messaging(backend=messaging_backend.storage_backend) as messaging:
        channel = messaging.channel(name="events", codec=AgUiCodec())
        body = await channel.open_sse(
            source(), identity=identity, after=0, on_committed=observe
        )
        try:
            await terminal.wait()
            assert await channel.get_run_status(identity=identity) == "running"
            with pytest.raises(PublicationRejected):
                await channel.publish(
                    CustomEvent(name="late", value=1), identity=identity
                )
        finally:
            release.set()
            await body.aclose()


async def test_other_facade_publishes_after_child_terminal_without_owning_source(
    messaging_backend: MessagingBackendHarness,
) -> None:
    identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
    ready, release = asyncio.Event(), asyncio.Event()

    async def observe(envelope: MessageEnvelope) -> None:
        if envelope.seq == 2:
            ready.set()

    async def source() -> AsyncGenerator[BaseEvent, None]:
        yield RunStartedEvent(thread_id="thread", run_id="run")
        yield RunFinishedEvent(thread_id="thread", run_id="child")
        await release.wait()
        yield RunFinishedEvent(thread_id="thread", run_id="run")

    async with Messaging(backend=messaging_backend.storage_backend) as owner:
        channel = owner.channel(name="events", codec=AgUiCodec())
        body = await channel.open_sse(
            source(), identity=identity, after=0, on_committed=observe
        )
        try:
            await ready.wait()
            async with Messaging(backend=messaging_backend.storage_backend) as observer:
                publisher = observer.channel(name="events", codec=AgUiCodec())
                results = await asyncio.gather(
                    *[
                        publisher.publish(
                            CustomEvent(name="progress", value=index), identity=identity
                        )
                        for index in range(8)
                    ]
                )
                assert sorted(item.seq for item in results) == list(range(3, 11))
            assert await channel.get_run_status(identity=identity) == "running"
            release.set()
            assert len([frame async for frame in body]) == 11
        finally:
            release.set()
            await body.aclose()


async def test_publish_requires_first_source_commit(
    messaging_backend: MessagingBackendHarness,
) -> None:
    identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
    release, child_committed = asyncio.Event(), asyncio.Event()

    async def observe(envelope: MessageEnvelope) -> None:
        del envelope
        child_committed.set()

    async def source() -> AsyncGenerator[BaseEvent, None]:
        yield RunStartedEvent(thread_id="thread", run_id="child")
        await release.wait()
        yield RunStartedEvent(thread_id="thread", run_id="run")
        yield RunFinishedEvent(thread_id="thread", run_id="run")

    async with Messaging(backend=messaging_backend.storage_backend) as messaging:
        channel = messaging.channel(name="events", codec=AgUiCodec())
        body = await channel.open_sse(
            source(), identity=identity, after=0, on_committed=observe
        )
        try:
            await child_committed.wait()
            with pytest.raises(PublicationRejected) as raised:
                await channel.publish(
                    CustomEvent(name="early", value=1), identity=identity
                )
            assert raised.value.context["reason"] == "run_not_ready"
        finally:
            release.set()
            await body.aclose()


async def test_external_publication_preserves_recoverable_checkpoint(
    messaging_backend: MessagingBackendHarness,
) -> None:
    from tinkerfin_messaging import RecoverableMessage, RecoveryCheckpoint
    from tinkerfin_messaging.backend_contract import MessagingStateQuery

    identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
    release = asyncio.Event()
    expected_checkpoint = RecoveryCheckpoint(
        position=b"position", last_message_id="source-start"
    )

    class Factory:
        async def open(
            self, checkpoint: RecoveryCheckpoint | None
        ) -> AsyncGenerator[RecoverableMessage[BaseEvent], None]:
            assert checkpoint is None

            async def events() -> AsyncGenerator[RecoverableMessage[BaseEvent], None]:
                yield RecoverableMessage(
                    message_id="source-start",
                    data=RunStartedEvent(thread_id="thread", run_id="run"),
                    checkpoint=expected_checkpoint,
                )
                await release.wait()

            return events()

    async with Messaging(backend=messaging_backend.storage_backend) as messaging:
        channel = messaging.channel(name="events", codec=AgUiCodec())
        subscription = await channel.wrap_recoverable(
            Factory(), identity=identity, after=0
        )
        try:
            iterator = aiter(subscription)
            assert (await anext(iterator)).envelope.seq == 1
            await channel.publish(
                CustomEvent(name="progress", value=1), identity=identity
            )
            state = await messaging_backend.load_messaging_state(
                MessagingStateQuery(channel="events", identity=identity)
            )
            assert (
                state.target_run is not None
                and state.target_run.checkpoint == expected_checkpoint
            )
        finally:
            release.set()
            await subscription.aclose()


def test_publication_error_has_stable_safe_context() -> None:
    from tinkerfin_messaging import MessagingError, MessagingErrorCode

    error = PublicationRejected(
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run"),
        reason="run_closed",
    )
    assert isinstance(error, MessagingError)
    assert error.code == MessagingErrorCode.PUBLICATION_REJECTED
    assert dict(error.context) == {
        "thread_id": "thread",
        "run_id": "run",
        "reason": "run_closed",
    }
    from types import MappingProxyType

    assert isinstance(error.context, MappingProxyType)
