"""Shared ordered-log behavior exercised against isolated memory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from typing import cast

import pytest

from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    CodecMismatch,
    MemoryBackend,
    MessageEnvelope,
    MessageIdConflict,
    RecoveryCheckpoint,
    RunProducerFailed,
)
from tinkerfin_messaging._messaging_ledger import (
    BackendRunHandle,
    PreparedRun,
    _MessagingLedger,
)


def _identity(
    *,
    thread_id: str = "stream-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


@pytest.fixture(params=(pytest.param("memory", id="memory"),))
async def backend() -> AsyncGenerator[_MessagingLedger, None]:
    yield _MessagingLedger(MemoryBackend())


async def _prepare(
    backend: _MessagingLedger,
    *,
    identity: RunIdentity | None = None,
    codec: str = "test.bytes.v1",
) -> PreparedRun:
    resolved_identity = identity or _identity()
    return await backend.prepare(
        channel="events",
        identity=resolved_identity,
        codec=codec,
        after=0,
        cancellable=False,
        recoverable=False,
    )


async def _collect(iterator: AsyncIterator[MessageEnvelope]) -> list[MessageEnvelope]:
    return [message async for message in iterator]


async def test_concurrent_appends_allocate_one_contiguous_sequence(
    backend: _MessagingLedger,
) -> None:
    prepared = await _prepare(backend)

    committed = await asyncio.gather(
        *(
            backend.append(
                prepared.handle,
                message_id=f"message-{index}",
                codec="test.bytes.v1",
                payload=str(index).encode(),
            )
            for index in range(2)
        )
    )
    await backend.finish(prepared.handle, status="completed")

    assert sorted(message.seq for message in committed) == [1, 2]
    replay = await _collect(backend.follow(prepared.handle, after=0))
    assert [message.seq for message in replay] == [1, 2]


async def test_message_id_retry_is_idempotent_and_content_sensitive(
    backend: _MessagingLedger,
) -> None:
    prepared = await _prepare(backend)

    first = await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"same",
    )
    retried = await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"same",
    )

    assert retried == first
    with pytest.raises(MessageIdConflict):
        await backend.append(
            prepared.handle,
            message_id="message-1",
            codec="test.bytes.v1",
            payload=b"different",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("message_id", " ", id="blank-message-id"),
        pytest.param("message_id", " padded ", id="padded-message-id"),
        pytest.param("message_id", "x" * 1025, id="overlong-message-id"),
        pytest.param("codec", " ", id="blank-codec"),
        pytest.param("codec", "x" * 1025, id="overlong-codec"),
        pytest.param("channel", " ", id="blank-channel"),
        pytest.param("channel", "x" * 1025, id="overlong-channel"),
        pytest.param("thread_id", " padded ", id="padded-thread-id"),
        pytest.param("thread_id", "x" * 1025, id="overlong-thread-id"),
        pytest.param("run_id", " ", id="blank-run-id"),
        pytest.param("run_id", "x" * 1025, id="overlong-run-id"),
    ],
)
async def test_append_rejects_invalid_envelope_identifiers_before_commit(
    backend: _MessagingLedger,
    field: str,
    value: str,
) -> None:
    """A missing identifier check must not reach either backend commit path."""

    prepared = await _prepare(backend)
    handle: BackendRunHandle = prepared.handle
    message_id = "message-1"
    codec = "test.bytes.v1"
    if field == "channel":
        handle = replace(handle, **{field: value})
    elif field == "thread_id":
        handle = replace(
            handle,
            identity=RunIdentity.model_construct(thread_id=value, run_id="run-1"),
        )
    elif field == "run_id":
        handle = replace(
            handle,
            identity=RunIdentity.model_construct(thread_id="stream-1", run_id=value),
        )
    elif field == "message_id":
        message_id = value
    elif field == "codec":
        codec = value
    else:  # pragma: no cover - the literal parameter table is exhaustive
        raise AssertionError(f"unknown append field: {field}")

    with pytest.raises((TypeError, ValueError)):
        await backend.append(
            handle,
            message_id=message_id,
            codec=codec,
            payload=b"must-not-commit",
        )

    assert await backend.latest_seq(channel="events", identity=_identity()) == 0
    await backend.finish(prepared.handle, status="completed")


@pytest.mark.parametrize(
    ("payload", "checkpoint", "error_type"),
    [
        pytest.param(
            bytearray(b"not-bytes"),
            None,
            TypeError,
            id="bytearray-payload",
        ),
        pytest.param(
            b"value",
            {"position": b"1", "last_message_id": "message-1"},
            TypeError,
            id="checkpoint-mapping",
        ),
        pytest.param(
            b"value",
            RecoveryCheckpoint(position=b"1", last_message_id=None),
            ValueError,
            id="checkpoint-without-message-id",
        ),
        pytest.param(
            b"value",
            RecoveryCheckpoint(position=b"1", last_message_id="different-message"),
            ValueError,
            id="checkpoint-with-different-message-id",
        ),
    ],
)
async def test_append_rejects_invalid_payload_and_checkpoint_before_commit(
    backend: _MessagingLedger,
    payload: object,
    checkpoint: object,
    error_type: type[Exception],
) -> None:
    """Backend mutation must remain unreachable for malformed append values."""

    prepared = await _prepare(backend)

    with pytest.raises(error_type):
        await backend.append(
            prepared.handle,
            message_id="message-1",
            codec="test.bytes.v1",
            payload=cast(bytes, payload),
            checkpoint=cast(RecoveryCheckpoint | None, checkpoint),
        )

    assert await backend.latest_seq(channel="events", identity=_identity()) == 0
    await backend.finish(prepared.handle, status="completed")


async def test_append_accepts_complete_identifier_and_checkpoint_boundaries(
    backend: _MessagingLedger,
) -> None:
    identifier = "x" * 1024
    prepared = await backend.prepare(
        channel=identifier,
        identity=_identity(thread_id=identifier, run_id=identifier),
        codec=identifier,
        after=0,
        cancellable=False,
        recoverable=True,
    )
    checkpoint = RecoveryCheckpoint(
        position=b"",
        last_message_id=identifier,
    )

    committed = await backend.append(
        prepared.handle,
        message_id=identifier,
        codec=identifier,
        payload=b"",
        checkpoint=checkpoint,
    )

    assert committed.seq == 1
    assert committed.payload == b""
    assert committed.message_id == identifier
    await backend.finish(prepared.handle, status="completed")


async def test_checkpoint_participates_in_message_id_idempotency(
    backend: _MessagingLedger,
) -> None:
    prepared = await _prepare(backend)
    first_checkpoint = RecoveryCheckpoint(
        position=b"position-1",
        last_message_id="message-1",
    )
    different_checkpoint = RecoveryCheckpoint(
        position=b"position-2",
        last_message_id="message-1",
    )

    first = await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"same",
        checkpoint=first_checkpoint,
    )
    retried = await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"same",
        checkpoint=first_checkpoint,
    )
    with pytest.raises(MessageIdConflict):
        await backend.append(
            prepared.handle,
            message_id="message-1",
            codec="test.bytes.v1",
            payload=b"same",
            checkpoint=different_checkpoint,
        )

    assert retried == first
    assert await backend.latest_seq(channel="events", identity=_identity()) == 1
    await backend.finish(prepared.handle, status="completed")


async def test_concurrent_message_id_retries_commit_once(
    backend: _MessagingLedger,
) -> None:
    prepared = await _prepare(backend)

    first, second = await asyncio.gather(
        backend.append(
            prepared.handle,
            message_id="message-shared",
            codec="test.bytes.v1",
            payload=b"same",
        ),
        backend.append(
            prepared.handle,
            message_id="message-shared",
            codec="test.bytes.v1",
            payload=b"same",
        ),
    )

    assert first == second
    assert await backend.latest_seq(channel="events", identity=_identity()) == 1


async def test_stream_sequences_are_independent(
    backend: _MessagingLedger,
) -> None:
    first = await _prepare(backend, identity=_identity())
    second = await _prepare(
        backend,
        identity=_identity(thread_id="stream-2", run_id="run-2"),
    )

    first_message, second_message = await asyncio.gather(
        backend.append(
            first.handle,
            message_id="message-1",
            codec="test.bytes.v1",
            payload=b"first",
        ),
        backend.append(
            second.handle,
            message_id="message-1",
            codec="test.bytes.v1",
            payload=b"second",
        ),
    )

    assert first_message.seq == 1
    assert second_message.seq == 1


async def test_history_to_live_transition_has_no_gap_or_duplicate(
    backend: _MessagingLedger,
) -> None:
    prepared = await _prepare(backend)
    await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"history",
    )
    follower = asyncio.create_task(_collect(backend.follow(prepared.handle, after=0)))
    await asyncio.sleep(0)

    await backend.append(
        prepared.handle,
        message_id="message-2",
        codec="test.bytes.v1",
        payload=b"live",
    )
    await backend.finish(prepared.handle, status="completed")

    replay = await follower
    assert [message.payload for message in replay] == [b"history", b"live"]


async def test_completed_old_run_stops_before_a_later_run(
    backend: _MessagingLedger,
) -> None:
    old = await _prepare(backend, identity=_identity())
    await backend.append(
        old.handle,
        message_id="run-1:1",
        codec="test.bytes.v1",
        payload=b"old",
    )
    await backend.finish(old.handle, status="completed")
    new = await _prepare(backend, identity=_identity(run_id="run-2"))
    await backend.append(
        new.handle,
        message_id="run-2:1",
        codec="test.bytes.v1",
        payload=b"new",
    )
    await backend.finish(new.handle, status="completed")

    old_replay = await _collect(backend.follow(old.handle, after=0))
    new_replay = await _collect(backend.follow(new.handle, after=0))

    assert [message.payload for message in old_replay] == [b"old"]
    assert [message.payload for message in new_replay] == [b"old", b"new"]


async def test_failed_run_drains_committed_prefix_before_raising(
    backend: _MessagingLedger,
) -> None:
    prepared = await _prepare(backend)
    await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"committed",
    )
    cause = RuntimeError("storage observer failed")
    await backend.finish(prepared.handle, status="failed", error=cause)
    replay = backend.follow(prepared.handle, after=0)

    assert (await anext(replay)).payload == b"committed"
    with pytest.raises(RunProducerFailed) as captured:
        await anext(replay)

    assert captured.value.cause is not None
    assert str(cause) in str(captured.value.cause)


async def test_codec_is_bound_to_the_channel_across_streams(
    backend: _MessagingLedger,
) -> None:
    prepared = await _prepare(backend)
    await backend.finish(prepared.handle, status="completed")

    with pytest.raises(CodecMismatch):
        await _prepare(
            backend,
            identity=_identity(run_id="run-2"),
            codec="test.other.v1",
        )

    with pytest.raises(CodecMismatch):
        await backend.prepare(
            channel="events",
            identity=_identity(thread_id="stream-2"),
            codec="test.other.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )


async def test_append_rejects_a_codec_different_from_the_channel_binding(
    backend: _MessagingLedger,
) -> None:
    prepared = await _prepare(backend, codec="test.codec-a.v1")

    with pytest.raises(CodecMismatch) as raised:
        await backend.append(
            prepared.handle,
            message_id="message-1",
            codec="test.codec-b.v1",
            payload=b"must-not-commit",
        )

    assert raised.value.expected == "test.codec-a.v1"
    assert raised.value.actual == "test.codec-b.v1"
    assert await backend.latest_seq(channel="events", identity=_identity()) == 0

    await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.codec-a.v1",
        payload=b"committed",
    )
    with pytest.raises(CodecMismatch):
        await backend.append(
            prepared.handle,
            message_id="message-1",
            codec="test.codec-b.v1",
            payload=b"committed",
        )
    assert await backend.latest_seq(channel="events", identity=_identity()) == 1
    await backend.finish(prepared.handle, status="completed")


async def test_concurrent_first_use_binds_one_channel_codec_atomically(
    backend: _MessagingLedger,
) -> None:
    outcomes = await asyncio.gather(
        _prepare(
            backend,
            identity=_identity(thread_id="stream-a", run_id="run-a"),
            codec="test.codec-a.v1",
        ),
        _prepare(
            backend,
            identity=_identity(thread_id="stream-b", run_id="run-b"),
            codec="test.codec-b.v1",
        ),
        return_exceptions=True,
    )

    owners = [outcome for outcome in outcomes if isinstance(outcome, PreparedRun)]
    mismatches = [outcome for outcome in outcomes if isinstance(outcome, CodecMismatch)]
    assert len(owners) == 1
    assert len(mismatches) == 1
    await backend.finish(owners[0].handle, status="completed")


async def test_backend_exposes_defensive_cursor_reads(
    backend: _MessagingLedger,
) -> None:
    prepared = await _prepare(backend)
    for index in range(3):
        await backend.append(
            prepared.handle,
            message_id=f"message-{index}",
            codec="test.bytes.v1",
            payload=str(index).encode(),
        )

    assert await backend.latest_seq(channel="events", identity=_identity()) == 3
    page = await backend.read(
        channel="events",
        identity=_identity(),
        after=1,
        limit=1,
    )
    assert [message.seq for message in page] == [2]


@pytest.mark.parametrize(
    ("limit", "error_type"),
    [(True, TypeError), (0, ValueError), (1001, ValueError)],
)
async def test_backend_read_rejects_invalid_limit_boundaries_consistently(
    backend: _MessagingLedger,
    limit: int,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        await backend.read(
            channel="events",
            identity=_identity(),
            limit=limit,
        )


@pytest.mark.parametrize(
    ("after", "error_type"),
    [(True, TypeError), (-1, ValueError)],
)
async def test_backend_read_rejects_invalid_cursor_boundaries_consistently(
    backend: _MessagingLedger,
    after: int,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        await backend.read(
            channel="events",
            identity=_identity(),
            after=after,
        )
