"""SQL message storage contracts shared by all three database engines."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from backend_harness import MessagingBackendHarness
from sql_messaging_support import database_time
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    MessagingBackendProtocolError,
    MessagingLimits,
    MessagingQuotaExceeded,
    MessagingRetentionPolicy,
    RecoveryCheckpoint,
    SqlAlchemyBackend,
    StreamDeleted,
)
from tinkerfin_messaging._sql_schema import capacity, messages, metadata
from tinkerfin_messaging.backend_contract import (
    CommittedMessageQuery,
    MessagingBackend,
    MessagingStateQuery,
    MessagingTransition,
)
from tinkerfin_messaging.testing import verify_messaging_backend


def identity(
    *, namespace: str = "customer-1", thread: str = "thread-1", run: str = "run-1"
) -> RunIdentity:
    return RunIdentity(namespace=namespace, thread_id=thread, run_id=run)


async def prepare(backend: MessagingBackendHarness, run: RunIdentity):
    return await backend.prepare(
        channel="events",
        identity=run,
        codec="test.bytes",
        after=0,
        cancellable=True,
        recoverable=True,
    )


async def test_sql_backend_public_verifier(messaging_sql_engine: AsyncEngine) -> None:
    backend = SqlAlchemyBackend(messaging_sql_engine)
    assert isinstance(backend, MessagingBackend)

    @asynccontextmanager
    async def open_backend() -> AsyncIterator[MessagingBackend]:
        yield backend

    await verify_messaging_backend(open_backend)


async def test_sql_backend_cross_worker_replay_and_namespace(
    messaging_sql_engine: AsyncEngine,
) -> None:
    first = MessagingBackendHarness(SqlAlchemyBackend(messaging_sql_engine))
    second = MessagingBackendHarness(SqlAlchemyBackend(messaging_sql_engine))
    for namespace in ("customer-1", "customer-2"):
        owner = await prepare(first, identity(namespace=namespace))
        await first.append(
            owner.handle,
            message_id="m-1",
            codec="test.bytes",
            payload=namespace.encode(),
        )
        await first.finish(owner.handle, status="completed")
        page = await second.read(channel="events", identity=owner.handle.identity)
        assert [message.payload for message in page] == [namespace.encode()]
    await first.delete_stream(channel="events", identity=identity())
    assert await second.read(channel="events", identity=identity()) == ()
    with pytest.raises(StreamDeleted):
        await second.read_committed_messages(
            CommittedMessageQuery(
                channel="events",
                identity=identity(),
                generation=1,
                after_sequence=0,
                through_sequence=None,
                limit=10,
            )
        )
    page = await second.read(
        channel="events", identity=identity(namespace="customer-2")
    )
    assert [message.payload for message in page] == [b"customer-2"]


async def test_sql_backend_global_capacity_and_checkpoint_evidence(
    messaging_sql_engine: AsyncEngine,
) -> None:
    limits = MessagingLimits(max_total_records=6, max_total_bytes=20)
    backend = MessagingBackendHarness(
        SqlAlchemyBackend(messaging_sql_engine, limits=limits)
    )
    owner = await prepare(backend, identity())
    saved = RecoveryCheckpoint(position=b"position", last_message_id="m")
    message = await backend.append(
        owner.handle,
        message_id="m",
        codec="test.bytes",
        payload=b"aa",
        checkpoint=saved,
    )
    duplicate = await backend.append(
        owner.handle,
        message_id="m",
        codec="test.bytes",
        payload=b"aa",
        checkpoint=saved,
    )
    assert duplicate == message
    async with messaging_sql_engine.connect() as connection:
        row = (await connection.execute(select(capacity))).mappings().one()
    assert row["total_records"] == 5
    assert row["total_bytes"] == 20
    with pytest.raises(MessagingQuotaExceeded, match="total_bytes"):
        await backend.append(
            owner.handle, message_id="second", codec="test.bytes", payload=b"x"
        )
    await backend.finish(owner.handle, status="completed")
    await backend.delete_stream(channel="events", identity=identity())
    async with messaging_sql_engine.connect() as connection:
        row = (await connection.execute(select(capacity))).mappings().one()
    assert row["total_records"] == 3
    assert row["total_bytes"] == 0


async def test_sql_backend_concurrent_admission_respects_global_limit(
    messaging_sql_engine: AsyncEngine,
) -> None:
    limits = MessagingLimits(max_total_records=4)
    left = MessagingBackendHarness(
        SqlAlchemyBackend(messaging_sql_engine, limits=limits)
    )
    right = MessagingBackendHarness(
        SqlAlchemyBackend(messaging_sql_engine, limits=limits)
    )
    outcomes = await asyncio.gather(
        prepare(left, identity()),
        prepare(right, identity(namespace="another")),
        return_exceptions=True,
    )
    assert sum(isinstance(item, MessagingQuotaExceeded) for item in outcomes) == 1
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1


async def test_sql_backend_preparation_is_borrowed_and_rejects_other_settings(
    messaging_sql_engine: AsyncEngine,
) -> None:
    backend = SqlAlchemyBackend(messaging_sql_engine)
    await asyncio.gather(
        backend.prepare_messaging_storage(),
        SqlAlchemyBackend(messaging_sql_engine).prepare_messaging_storage(),
    )
    other = SqlAlchemyBackend(
        messaging_sql_engine, limits=replace(MessagingLimits(), max_total_records=10)
    )
    with pytest.raises(MessagingBackendProtocolError, match="settings"):
        await other.prepare_messaging_storage()
    async with messaging_sql_engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    state = await backend.load_messaging_state(
        MessagingStateQuery(channel="empty", identity=identity())
    )
    assert state.stream is None
    assert state.observed_at.utcoffset() is not None


async def test_sql_backend_equal_numeric_settings_share_storage(
    messaging_sql_engine: AsyncEngine,
) -> None:
    first = SqlAlchemyBackend(
        messaging_sql_engine, producer_lease_seconds=15, poll_interval_seconds=1
    )
    second = SqlAlchemyBackend(
        messaging_sql_engine, producer_lease_seconds=15.0, poll_interval_seconds=1.0
    )
    assert first.messaging_settings == second.messaging_settings
    await first.prepare_messaging_storage()
    await second.prepare_messaging_storage()


@pytest.mark.parametrize(
    ("codec", "cursor", "error_type"),
    [("bytes", True, TypeError), ("bytes ", 0, ValueError)],
)
async def test_sql_transition_parameter_errors_keep_python_semantics(
    messaging_sql_engine: AsyncEngine,
    codec: str,
    cursor: int,
    error_type: type[Exception],
) -> None:
    backend = SqlAlchemyBackend(messaging_sql_engine)
    with pytest.raises(error_type):
        await backend.commit_messaging_transition(
            MessagingTransition(
                kind="prepare_run",
                transition_id="prepare",
                channel="events",
                identity=identity(),
                settings=backend.messaging_settings,
                codec_id=codec,
                after_sequence=cursor,
            )
        )
    snapshot = await backend.load_messaging_state(
        MessagingStateQuery(channel="events", identity=identity())
    )
    assert snapshot.channel is None and snapshot.stream is None


async def test_sql_backend_preserves_all_legal_text_and_checkpoint_sizes(
    messaging_sql_engine: AsyncEngine,
) -> None:
    backend = SqlAlchemyBackend(messaging_sql_engine)
    ledger = MessagingBackendHarness(backend)
    run = identity(
        namespace="customer\x00中文", thread="thread\x00🌳", run="run\x00中文"
    )
    channel, codec, message_id = "events\x00频道", "codec\x00格式", "消息\x00id"
    prepared = await backend.commit_messaging_transition(
        MessagingTransition(
            kind="prepare_run",
            transition_id="token\x00原文",
            channel=channel,
            identity=run,
            settings=backend.messaging_settings,
            codec_id=codec,
            after_sequence=0,
            recoverable=True,
        )
    )
    assert prepared.run_reference is not None
    assert prepared.run_reference.producer_token == "token\x00原文"
    saved = RecoveryCheckpoint(position=b"\x00\xff", last_message_id=message_id)
    committed = await backend.commit_messaging_transition(
        MessagingTransition(
            kind="append_message",
            transition_id="append",
            channel=channel,
            identity=run,
            settings=backend.messaging_settings,
            run_reference=prepared.run_reference,
            codec_id=codec,
            message_id=message_id,
            payload=b"\xff",
            checkpoint=saved,
        )
    )
    snapshot = await backend.load_messaging_state(
        MessagingStateQuery(
            channel=channel,
            identity=run,
            message_id=message_id,
            include_active_run=True,
        )
    )
    assert snapshot.matching_message is not None
    assert snapshot.matching_message.envelope == committed.envelope
    assert snapshot.matching_message.checkpoint == saved
    assert snapshot.target_run is not None
    assert snapshot.target_run.checkpoint == saved
    async with messaging_sql_engine.connect() as connection:
        size = (await connection.execute(select(capacity.c.total_bytes))).scalar_one()
    assert size == 1 + 2 * (len(saved.position) + len(message_id.encode()))
    failure = RuntimeError("failure\x00中文\ud800")
    await backend.commit_messaging_transition(
        MessagingTransition(
            kind="finish_run",
            transition_id="finish",
            channel=channel,
            identity=run,
            settings=backend.messaging_settings,
            run_reference=prepared.run_reference,
            final_status="failed",
            failure=failure,
        )
    )
    snapshot = await backend.load_messaging_state(
        MessagingStateQuery(channel=channel, identity=run)
    )
    assert snapshot.target_run is not None
    assert snapshot.target_run.failure_message == str(failure)
    await ledger.delete_stream(channel=channel, identity=run)
    async with messaging_sql_engine.connect() as connection:
        assert (
            await connection.execute(select(capacity.c.total_bytes))
        ).scalar_one() == 0


async def test_sql_expiry_reclaims_a_large_other_thread_before_admission(
    messaging_sql_engine: AsyncEngine,
) -> None:
    now = datetime(2030, 1, 1)
    with database_time(messaging_sql_engine, lambda: now):
        backend = MessagingBackendHarness(
            SqlAlchemyBackend(
                messaging_sql_engine,
                limits=MessagingLimits(max_total_bytes=130),
                retention_policy=MessagingRetentionPolicy.expire_after(3600),
            )
        )
        old = await prepare(backend, identity())
        for index in range(130):
            await backend.append(
                old.handle, message_id=str(index), codec="test.bytes", payload=b"x"
            )
        await backend.finish(old.handle, status="completed")
        now += timedelta(seconds=3601)
        current = await backend.prepare(
            channel="another",
            identity=identity(thread="new"),
            codec="test.bytes",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await backend.append(
            current.handle, message_id="full", codec="test.bytes", payload=b"x" * 130
        )
        async with messaging_sql_engine.connect() as connection:
            row = (await connection.execute(select(capacity))).mappings().one()
        assert row["total_bytes"] == 130
        assert row["total_records"] == 8


@pytest.mark.parametrize(
    "damage", ["missing_table", "missing_unique_index", "unknown_table"]
)
async def test_sql_backend_rejects_incomplete_schema_without_repair(
    messaging_sql_engine: AsyncEngine,
    damage: str,
) -> None:
    backend = SqlAlchemyBackend(messaging_sql_engine)
    await backend.prepare_messaging_storage()
    async with messaging_sql_engine.begin() as connection:
        if damage == "missing_table":
            await connection.run_sync(messages.drop)
        elif damage == "missing_unique_index":
            index = next(item for item in messages.indexes if item.unique)
            await connection.run_sync(index.drop)
        else:
            await connection.execute(
                text("CREATE TABLE tinkerfin_messaging_unknown (id INTEGER)")
            )
    for _ in range(2):
        with pytest.raises(MessagingBackendProtocolError):
            await SqlAlchemyBackend(messaging_sql_engine).prepare_messaging_storage()
    async with messaging_sql_engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
        total = (
            await connection.execute(select(capacity.c.total_records))
        ).scalar_one()
    assert total == 0
    if damage == "missing_table":
        assert messages.name not in names
    elif damage == "unknown_table":
        assert names == set(metadata.tables) | {"tinkerfin_messaging_unknown"}
