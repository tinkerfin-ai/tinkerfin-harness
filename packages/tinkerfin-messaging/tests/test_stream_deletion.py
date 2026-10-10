"""Cross-backend stream deletion, fencing, and generation contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from backend_harness import MessagingBackendHarness

import tinkerfin_messaging as messaging_api
from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    CodecMismatch,
    MessageEnvelope,
    RunNotFound,
    StreamDeleted,
)
from tinkerfin_messaging._messaging_ledger import PreparedRun
from tinkerfin_messaging.backend_contract import (
    MessagingStateQuery,
)


def _identity(
    *,
    thread_id: str = "conversation-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


async def _prepare_backend(
    backend: MessagingBackendHarness,
    *,
    identity: RunIdentity | None = None,
    codec: str = "test.bytes.v1",
    cancellable: bool = False,
) -> PreparedRun:
    resolved_identity = identity or _identity()
    return await backend.prepare(
        channel="events",
        identity=resolved_identity,
        codec=codec,
        after=0,
        cancellable=cancellable,
        recoverable=False,
    )


async def _collect(
    iterator: AsyncIterator[MessageEnvelope],
) -> list[MessageEnvelope]:
    return [message async for message in iterator]


async def test_old_handles_cannot_mutate_or_observe_a_rebuilt_stream(
    messaging_backend: MessagingBackendHarness,
) -> None:
    old = await _prepare_backend(
        messaging_backend,
        identity=_identity(run_id="run-old"),
        cancellable=True,
    )
    await messaging_backend.finish(old.handle, status="completed")
    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )
    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )
    new = await _prepare_backend(
        messaging_backend,
        identity=_identity(run_id="run-new"),
    )

    operations: tuple[Callable[[], Awaitable[object]], ...] = (
        lambda: messaging_backend.append(
            old.handle,
            message_id="stale-message",
            codec="test.bytes.v1",
            payload=b"stale",
        ),
        lambda: messaging_backend.begin_settlement(old.handle),
        lambda: messaging_backend.finish(old.handle, status="failed"),
        lambda: messaging_backend.request_cancel(old.handle),
        lambda: messaging_backend.wait_for_cancel(old.handle),
        lambda: messaging_backend.wait_finished(old.handle),
        lambda: messaging_backend.failure(old.handle),
        lambda: messaging_backend.renew(old.handle),
    )
    for operation in operations:
        with pytest.raises(StreamDeleted) as captured:
            await operation()
        assert captured.value.generation == old.handle.generation

    assert (
        await messaging_backend.latest_seq(
            channel="events",
            identity=_identity(),
        )
        == 0
    )
    await messaging_backend.finish(new.handle, status="completed")


async def test_delete_removes_an_empty_terminal_stream_across_backends(
    messaging_backend: MessagingBackendHarness,
) -> None:
    prepared = await _prepare_backend(messaging_backend)
    await messaging_backend.finish(prepared.handle, status="completed")

    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )

    with pytest.raises(RunNotFound):
        await messaging_backend.get_run_status(
            channel="events",
            identity=_identity(),
        )
    assert (
        await messaging_backend.latest_seq(
            channel="events",
            identity=_identity(),
        )
        == 0
    )
    with pytest.raises(StreamDeleted):
        await _collect(messaging_backend.follow(prepared.handle, after=0))


async def test_exact_generation_state_allows_a_missing_target_run_across_backends(
    messaging_backend: MessagingBackendHarness,
) -> None:
    prepared = await _prepare_backend(messaging_backend)
    assert prepared.handle.generation is not None

    snapshot = await messaging_backend.load_messaging_state(
        MessagingStateQuery(
            channel="events",
            identity=_identity(run_id="missing-run"),
            generation=prepared.handle.generation,
        )
    )

    assert snapshot.stream is not None
    assert snapshot.stream.generation == prepared.handle.generation
    assert snapshot.target_run is None
    await messaging_backend.finish(prepared.handle, status="completed")


async def test_delete_rebuilds_an_isolated_generation_across_backends(
    messaging_backend: MessagingBackendHarness,
) -> None:
    old = await _prepare_backend(
        messaging_backend,
        identity=_identity(run_id="run-old"),
    )
    await messaging_backend.append(
        old.handle,
        message_id="old-message",
        codec="test.bytes.v1",
        payload=b"old",
    )
    await messaging_backend.finish(old.handle, status="completed")

    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )

    assert (
        await messaging_backend.latest_seq(
            channel="events",
            identity=_identity(),
        )
        == 0
    )
    assert (
        await messaging_backend.read(
            channel="events",
            identity=_identity(),
            after=0,
        )
        == ()
    )
    with pytest.raises(StreamDeleted):
        await _collect(messaging_backend.follow(old.handle, after=0))

    new = await _prepare_backend(
        messaging_backend,
        identity=_identity(run_id="run-new"),
    )
    assert old.handle.generation is not None
    assert new.handle.generation == old.handle.generation + 1
    committed = await messaging_backend.append(
        new.handle,
        message_id="new-message",
        codec="test.bytes.v1",
        payload=b"new",
    )
    assert committed.seq == 1
    await messaging_backend.finish(new.handle, status="completed")


async def test_delete_rejects_an_active_producer_across_backends(
    messaging_backend: MessagingBackendHarness,
) -> None:
    prepared = await _prepare_backend(
        messaging_backend,
        cancellable=True,
    )

    with pytest.raises(messaging_api.StreamDeleteConflict):
        await messaging_backend.delete_stream(
            channel="events",
            identity=_identity(),
        )

    assert await messaging_backend.request_cancel(prepared.handle) is True
    await messaging_backend.finish(prepared.handle, status="cancelled")


async def test_delete_preserves_other_streams_and_channel_codec_across_backends(
    messaging_backend: MessagingBackendHarness,
) -> None:
    deleted = await _prepare_backend(
        messaging_backend,
        identity=_identity(thread_id="stream-deleted", run_id="run-deleted"),
    )
    retained = await _prepare_backend(
        messaging_backend,
        identity=_identity(thread_id="stream-retained", run_id="run-retained"),
    )
    await messaging_backend.append(
        deleted.handle,
        message_id="deleted-message",
        codec="test.bytes.v1",
        payload=b"deleted",
    )
    await messaging_backend.append(
        retained.handle,
        message_id="retained-message",
        codec="test.bytes.v1",
        payload=b"retained",
    )
    await messaging_backend.finish(deleted.handle, status="completed")
    await messaging_backend.finish(retained.handle, status="completed")

    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(thread_id="stream-deleted", run_id="run-deleted"),
    )

    assert (
        await messaging_backend.latest_seq(
            channel="events",
            identity=_identity(thread_id="stream-deleted", run_id="run-deleted"),
        )
        == 0
    )
    assert [
        message.payload
        for message in await messaging_backend.read(
            channel="events",
            identity=_identity(thread_id="stream-retained", run_id="run-retained"),
            after=0,
        )
    ] == [b"retained"]
    with pytest.raises(CodecMismatch):
        await _prepare_backend(
            messaging_backend,
            identity=_identity(thread_id="stream-new", run_id="run-new"),
            codec="test.other.v1",
        )


async def test_delete_of_a_missing_name_does_not_consume_a_generation(
    messaging_backend: MessagingBackendHarness,
) -> None:
    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )
    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )

    prepared = await _prepare_backend(messaging_backend)

    assert prepared.handle.generation == 1
    await messaging_backend.finish(prepared.handle, status="completed")
