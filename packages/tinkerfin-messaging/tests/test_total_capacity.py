"""Scope-wide capacity, retained evidence, and expiry admission contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.crc import key_slot

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    MemoryBackend,
    MessagingBackendProtocolError,
    MessagingLimits,
    MessagingQuotaExceeded,
    MessagingRetentionPolicy,
    RecoveryCheckpoint,
    RedisBackend,
    StreamDeleteConflict,
)
from tinkerfin_messaging._messaging_ledger import _MessagingLedger


def test_total_capacity_defaults_and_validation() -> None:
    limits = MessagingLimits()
    assert limits.max_total_bytes == 1024**3
    assert limits.max_total_records == 100_000
    for field in ("max_total_bytes", "max_total_records"):
        with pytest.raises(TypeError, match=field):
            MessagingLimits(**{field: True})
        with pytest.raises(ValueError, match=field):
            MessagingLimits(**{field: 0})


@pytest.fixture(
    params=[
        "memory",
        pytest.param(
            "redis", marks=[pytest.mark.docker_integration, pytest.mark.redis_e2e]
        ),
    ]
)
async def capacity_backend_factory(
    request: pytest.FixtureRequest,
) -> AsyncIterator[
    Callable[[MessagingLimits, MessagingRetentionPolicy], _MessagingLedger]
]:
    """Isolate each capacity contract, sharing a Redis prefix only inside its test."""

    client: Redis | None = None
    prefix = f"tfmsg:total-capacity:{uuid4().hex}"
    memory: MemoryBackend | None = None
    if request.param == "redis":
        url = request.getfixturevalue("redis_url")
        assert isinstance(url, str)
        client = Redis.from_url(url, decode_responses=False, socket_timeout=5)

    def make(
        limits: MessagingLimits, retention: MessagingRetentionPolicy
    ) -> _MessagingLedger:
        nonlocal memory
        if client is None:
            if memory is None:
                memory = MemoryBackend(limits=limits, retention_policy=retention)
            return _MessagingLedger(memory)
        return _MessagingLedger(
            RedisBackend(
                client,
                key_prefix=prefix,
                limits=limits,
                retention_policy=retention,
                producer_lease_seconds=5,
                generation_cleanup_retry_seconds=0.01,
            )
        )

    try:
        yield make
    finally:
        if client is not None:
            keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
            if keys:
                await client.unlink(*keys)
            await client.aclose()


async def _start(
    backend: _MessagingLedger, thread: str, *, channel: str = "events", run: str = "run"
):
    return await backend.prepare(
        channel=channel,
        identity=RunIdentity(namespace="test", thread_id=thread, run_id=run),
        codec="bytes",
        after=0,
        cancellable=True,
        recoverable=True,
    )


async def test_total_bytes_cross_channels_and_exact_idempotency(
    capacity_backend_factory,
) -> None:
    limits = MessagingLimits(max_total_bytes=4)
    first_worker = capacity_backend_factory(limits, MessagingRetentionPolicy())
    second_worker = capacity_backend_factory(limits, MessagingRetentionPolicy())
    first = await _start(first_worker, "first", channel="a")
    second = await _start(second_worker, "second", channel="b")
    one = await first_worker.append(
        first.handle, message_id="one", codec="bytes", payload=b"1234"
    )
    assert (
        await first_worker.append(
            first.handle, message_id="one", codec="bytes", payload=b"1234"
        )
        == one
    )
    with pytest.raises(MessagingQuotaExceeded) as error:
        await second_worker.append(
            second.handle, message_id="two", codec="bytes", payload=b"x"
        )
    assert error.value.resource == "total_bytes"
    assert (
        await second_worker.latest_seq(channel="b", identity=second.handle.identity)
        == 0
    )
    assert await first_worker.request_cancel(first.handle) is True
    assert await first_worker.begin_settlement(first.handle) is True
    await first_worker.finish(first.handle, status="cancelled")
    await first_worker.delete_stream(channel="a", identity=first.handle.identity)
    await second_worker.append(
        second.handle, message_id="two", codec="bytes", payload=b"1234"
    )
    await second_worker.finish(second.handle, status="completed")


async def test_total_records_count_empty_runs_channels_generations_and_tombstones(
    capacity_backend_factory,
) -> None:
    backend = capacity_backend_factory(
        MessagingLimits(max_total_records=6), MessagingRetentionPolicy()
    )
    first = await _start(backend, "thread")  # channel, thread, generation, Run
    await backend.finish(first.handle, status="completed")
    second = await _start(backend, "thread", run="second")  # second Run
    await backend.append(second.handle, message_id="empty", codec="bytes", payload=b"")
    with pytest.raises(MessagingQuotaExceeded) as error:
        await backend.append(
            second.handle, message_id="excess", codec="bytes", payload=b""
        )
    assert error.value.resource == "total_records"
    with pytest.raises(StreamDeleteConflict):
        await backend.delete_stream(channel="events", identity=second.handle.identity)
    await backend.finish(second.handle, status="completed")
    await backend.delete_stream(channel="events", identity=second.handle.identity)
    await backend.delete_stream(channel="events", identity=second.handle.identity)
    third = await _start(
        backend, "thread", run="third"
    )  # channel, thread, tombstone, generation, Run
    await backend.finish(third.handle, status="completed")
    await backend.delete_stream(channel="events", identity=third.handle.identity)
    fourth = await _start(
        backend, "thread", run="fourth"
    )  # two tombstones and current generation
    with pytest.raises(MessagingQuotaExceeded):
        await _start(backend, "new-thread", channel="new-channel")
    await backend.finish(fourth.handle, status="completed")
    await backend.delete_stream(channel="events", identity=fourth.handle.identity)
    with pytest.raises(MessagingQuotaExceeded):
        await _start(backend, "thread", run="fifth")


async def test_failed_admission_leaves_no_channel_or_run_records(
    capacity_backend_factory,
) -> None:
    backend = capacity_backend_factory(
        MessagingLimits(max_total_records=5), MessagingRetentionPolicy()
    )
    first = await _start(backend, "first")
    for index in range(100):
        with pytest.raises(MessagingQuotaExceeded):
            await _start(backend, f"rejected-{index}", channel=f"rejected-{index}")
    await backend.finish(first.handle, status="completed")
    await backend.delete_stream(channel="events", identity=first.handle.identity)
    second = await _start(backend, "first", run="second")
    assert second.handle.generation == 2
    await backend.finish(second.handle, status="completed")


async def test_checkpoint_evidence_counts_copies_and_replaces_latest(
    capacity_backend_factory,
) -> None:
    backend = capacity_backend_factory(
        MessagingLimits(max_total_bytes=16), MessagingRetentionPolicy()
    )
    prepared = await _start(backend, "checkpoints")
    checkpoint = RecoveryCheckpoint(position=b"1234567", last_message_id="a")
    await backend.append(
        prepared.handle,
        message_id="a",
        codec="bytes",
        payload=b"",
        checkpoint=checkpoint,
    )
    # Both the committed evidence and latest Run checkpoint retain eight bytes.
    with pytest.raises(MessagingQuotaExceeded):
        await backend.append(
            prepared.handle, message_id="b", codec="bytes", payload=b"x"
        )
    # Replacing the eight-byte latest checkpoint with two bytes releases six bytes;
    # the first message's checkpoint evidence remains retained for idempotency.
    await backend.append(
        prepared.handle,
        message_id="b",
        codec="bytes",
        payload=b"",
        checkpoint=RecoveryCheckpoint(position=b"x", last_message_id="b"),
    )
    await backend.append(
        prepared.handle,
        message_id="a",
        codec="bytes",
        payload=b"",
        checkpoint=checkpoint,
    )
    await backend.append(
        prepared.handle, message_id="c", codec="bytes", payload=b"1234"
    )
    with pytest.raises(MessagingQuotaExceeded):
        await backend.append(
            prepared.handle, message_id="d", codec="bytes", payload=b"x"
        )
    await backend.finish(prepared.handle, status="completed")
    await backend.delete_stream(channel="events", identity=prepared.handle.identity)
    replacement = await _start(backend, "replacement")
    await backend.append(
        replacement.handle, message_id="free", codec="bytes", payload=b"0" * 16
    )
    await backend.finish(replacement.handle, status="completed")


async def test_concurrent_workers_cannot_overshoot_total_bytes(
    capacity_backend_factory,
) -> None:
    limits = MessagingLimits(max_total_bytes=10)
    policy = MessagingRetentionPolicy()
    workers = [capacity_backend_factory(limits, policy) for _ in range(20)]
    runs = [
        await _start(worker, str(index), channel=str(index))
        for index, worker in enumerate(workers)
    ]
    results = await asyncio.gather(
        *[
            worker.append(run.handle, message_id="message", codec="bytes", payload=b"x")
            for worker, run in zip(workers, runs, strict=True)
        ],
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 10
    assert all(
        isinstance(error, MessagingQuotaExceeded) and error.resource == "total_bytes"
        for error in errors
    )
    for worker, run in zip(workers, runs, strict=True):
        await worker.finish(run.handle, status="completed")


@pytest.mark.docker_integration
@pytest.mark.redis_e2e
async def test_redis_scope_format_and_limits_are_shared_across_channels(
    redis_url: str,
) -> None:
    client = Redis.from_url(redis_url, decode_responses=False, socket_timeout=5)
    prefix = f"tfmsg:capacity-format:{uuid4().hex}"
    try:
        backend = _MessagingLedger(
            RedisBackend(
                client, key_prefix=prefix, limits=MessagingLimits(max_total_records=12)
            )
        )
        first = await _start(backend, "thread", channel="one")
        second = await _start(backend, "thread", channel="two")
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        assert len({key_slot(key) for key in keys}) == 1
        quota_keys = [key for key in keys if key.endswith(b":capacity")]
        assert len(quota_keys) == 1
        assert (
            await cast(
                Awaitable[bytes | None], client.hget(quota_keys[0], "total_records")
            )
            == b"8"
        )
        different = _MessagingLedger(
            RedisBackend(
                client, key_prefix=prefix, limits=MessagingLimits(max_total_records=13)
            )
        )
        with pytest.raises(MessagingBackendProtocolError):
            await _start(different, "thread", channel="three")
        assert (
            await cast(
                Awaitable[bytes | None], client.hget(quota_keys[0], "total_records")
            )
            == b"8"
        )
        await backend.finish(first.handle, status="completed")
        await backend.finish(second.handle, status="completed")
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.unlink(*keys)
        await client.aclose()


async def test_concurrent_empty_admissions_cannot_overshoot_total_records(
    capacity_backend_factory,
) -> None:
    limits = MessagingLimits(max_total_records=8)
    policy = MessagingRetentionPolicy()
    workers = [capacity_backend_factory(limits, policy) for _ in range(20)]
    results = await asyncio.gather(
        *[
            _start(worker, str(index), channel=str(index))
            for index, worker in enumerate(workers)
        ],
        return_exceptions=True,
    )
    admitted = 0
    for worker, result in zip(workers, results, strict=True):
        if isinstance(result, MessagingQuotaExceeded):
            assert result.resource == "total_records"
        else:
            assert not isinstance(result, BaseException)
            admitted += 1
            await worker.finish(result.handle, status="completed")
    assert admitted == 2


async def test_plain_commit_preserves_latest_checkpoint_and_historical_evidence(
    capacity_backend_factory,
) -> None:
    from tinkerfin_messaging.backend_contract import MessagingStateQuery

    backend = capacity_backend_factory(MessagingLimits(), MessagingRetentionPolicy())
    run = await _start(backend, "checkpoint")
    first = RecoveryCheckpoint(position=b"first", last_message_id="one")
    second = RecoveryCheckpoint(position=b"second", last_message_id="two")
    for message_id, checkpoint in (("one", first), ("two", second), ("plain", None)):
        await backend.append(
            run.handle,
            message_id=message_id,
            codec="bytes",
            payload=b"",
            checkpoint=checkpoint,
        )
    snapshot = await backend.backend.load_messaging_state(
        MessagingStateQuery(
            channel="events",
            identity=run.handle.identity,
            generation=run.handle.generation,
            message_id="one",
        )
    )
    assert snapshot.target_run is not None
    assert snapshot.target_run.checkpoint == second
    assert snapshot.matching_message is not None
    assert snapshot.matching_message.checkpoint == first
    await backend.finish(run.handle, status="completed")


@pytest.mark.docker_integration
@pytest.mark.redis_e2e
async def test_redis_expiry_index_has_one_member_per_terminal_thread(
    redis_url: str,
) -> None:
    client = Redis.from_url(redis_url, decode_responses=False, socket_timeout=5)
    prefix = f"tfmsg:capacity-index:{uuid4().hex}"
    try:
        backend = _MessagingLedger(
            RedisBackend(
                client,
                key_prefix=prefix,
                retention_policy=MessagingRetentionPolicy.expire_after(30),
            )
        )
        for index in range(70):
            run = await _start(backend, "same-thread", run=str(index))
            await backend.finish(run.handle, status="completed")
        indices = [
            key async for key in client.scan_iter(match=f"{prefix}:*:expirations")
        ]
        assert len(indices) == 1
        assert await cast(Awaitable[int], client.zcard(indices[0])) == 1
        active = await _start(backend, "same-thread", run="active")
        assert await cast(Awaitable[int], client.zcard(indices[0])) == 0
        await backend.finish(active.handle, status="completed")
        assert await cast(Awaitable[int], client.zcard(indices[0])) == 1
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.unlink(*keys)
        await client.aclose()
