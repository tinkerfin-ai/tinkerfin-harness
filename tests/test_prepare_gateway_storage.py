"""Offline conversion preserves existing records and current storage semantics."""

from __future__ import annotations

import argparse
import json
from collections.abc import AsyncIterator, Awaitable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from scripts.prepare_gateway_storage import (
    StoragePreparationError,
    _execute,
    main,
    prepare_redis_storage,
    prepare_sql_storage,
)
from sqlalchemy import (
    CheckConstraint,
    Column,
    Index,
    MetaData,
    Table,
    UniqueConstraint,
    inspect,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.support.docker_services import MySQLTestService, PostgreSQLTestService
from tinkerfin_automation import ExecutionLimits, SqlAlchemyAutomationStore
from tinkerfin_automation.service import AutomationService
from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import RecoveryCheckpoint, RunRequestConflict
from tinkerfin_messaging.backend_contract import (
    CommittedMessageQuery,
    MessagingStateQuery,
    MessagingTransition,
)
from tinkerfin_messaging.redis import RedisBackend
from tinkerfin_messaging.sqlalchemy import SqlAlchemyBackend
from tinkerfin_studio.conversation.models import ConversationRunRegistration


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("mysql", marks=pytest.mark.docker_integration),
        pytest.param("postgresql", marks=pytest.mark.docker_integration),
    ]
)
async def storage_engine(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[AsyncEngine]:
    """Own exactly one disposable database or schema per test."""
    if request.param == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'storage.db'}")
        try:
            yield engine
        finally:
            await engine.dispose()
        return
    service = request.getfixturevalue(f"{request.param}_test_service")
    name = f"gateway_storage_{uuid4().hex}"
    if isinstance(service, MySQLTestService):
        admin = create_async_engine(service.url("tinkerfin_test_admin"))
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"
            )
        engine = create_async_engine(service.url(name))
        drop = f"DROP DATABASE `{name}`"
    else:
        assert isinstance(service, PostgreSQLTestService)
        admin = create_async_engine(service.url())
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{name}"')
        engine = create_async_engine(
            service.url(), connect_args={"server_settings": {"search_path": name}}
        )
        drop = f'DROP SCHEMA "{name}" CASCADE'
    try:
        yield engine
    finally:
        await engine.dispose()
        try:
            async with admin.begin() as connection:
                await connection.exec_driver_sql(drop)
        finally:
            await admin.dispose()


async def _snapshot(engine: AsyncEngine, omit: tuple[str, str] | None = None):
    async with engine.connect() as connection:
        metadata = MetaData()
        await connection.run_sync(metadata.reflect)
        result = {}
        for name, table in metadata.tables.items():
            rows = (
                await connection.execute(
                    select(table).order_by(*table.primary_key.columns)
                )
            ).mappings()
            result[name] = [dict(row) for row in rows]
            if omit is not None and name == omit[0]:
                for row in result[name]:
                    row.pop(omit[1], None)
        return result


async def _without_column(engine: AsyncEngine, name: str, column: str) -> None:
    """Build the precise pre-conversion fixture using only this test's records."""
    async with engine.begin() as connection:
        table = await connection.run_sync(
            lambda sync: Table(name, MetaData(), autoload_with=sync)
        )
        rows = [
            dict(row) for row in (await connection.execute(select(table))).mappings()
        ]
        columns = [
            Column(
                item.name,
                item.type,
                nullable=item.nullable,
                primary_key=item.primary_key,
                autoincrement=item.autoincrement,
                server_default=item.server_default,
                comment=item.comment,
            )
            for item in table.c
            if item.name != column
        ]
        constraints = []
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                constraints.append(
                    UniqueConstraint(
                        *(item.name for item in constraint.columns),
                        name=constraint.name,
                    )
                )
            elif isinstance(constraint, CheckConstraint) and column not in str(
                constraint.sqltext
            ):
                constraints.append(
                    CheckConstraint(str(constraint.sqltext), name=constraint.name)
                )
        prior = Table(
            name,
            MetaData(),
            *columns,
            *constraints,
            comment=table.comment,
            **dict(table.kwargs),
        )
        for index in table.indexes:
            if index.name != "ix_tinkerfin_automation_runs_queue_deadline":
                Index(
                    index.name,
                    *(prior.c[item.name] for item in index.columns),
                    unique=index.unique,
                )
        await connection.run_sync(table.drop)
        await connection.run_sync(prior.create)
        for row in rows:
            row.pop(column)
        if rows:
            await connection.execute(prior.insert(), rows)


async def _seed_messaging(backend: SqlAlchemyBackend | RedisBackend):
    await backend.prepare_messaging_storage()
    identity = RunIdentity(namespace="offline", thread_id="thread", run_id="ordinary")
    prepared = await backend.commit_messaging_transition(
        MessagingTransition(
            kind="prepare_run",
            transition_id=uuid4().hex,
            channel="output",
            identity=identity,
            settings=backend.messaging_settings,
            codec_id="test.bytes",
            after_sequence=0,
            cancellable=True,
            recoverable=True,
        )
    )
    assert prepared.run_reference is not None
    await backend.commit_messaging_transition(
        MessagingTransition(
            kind="append_message",
            transition_id=uuid4().hex,
            channel="output",
            identity=identity,
            settings=backend.messaging_settings,
            run_reference=prepared.run_reference,
            message_id="saved",
            codec_id="test.bytes",
            payload=b"retained\x00message",
            checkpoint=RecoveryCheckpoint(
                position=b"saved\x00checkpoint", last_message_id="saved"
            ),
        )
    )
    return identity


async def _verify_messaging(
    backend: SqlAlchemyBackend | RedisBackend, identity: RunIdentity
):
    await backend.prepare_messaging_storage()
    state = await backend.load_messaging_state(
        MessagingStateQuery(channel="output", identity=identity)
    )
    assert state.target_run is not None and state.target_run.request_digest is None
    assert state.target_run.checkpoint == RecoveryCheckpoint(
        position=b"saved\x00checkpoint", last_message_id="saved"
    )
    page = await backend.read_committed_messages(
        CommittedMessageQuery(
            channel="output",
            identity=identity,
            after_sequence=0,
            generation=state.target_run.generation,
            through_sequence=None,
            limit=100,
        )
    )
    assert [message.payload for message in page.messages] == [b"retained\x00message"]
    with pytest.raises(RunRequestConflict):
        await backend.commit_messaging_transition(
            MessagingTransition(
                kind="prepare_run",
                transition_id=uuid4().hex,
                channel="output",
                identity=identity,
                settings=backend.messaging_settings,
                codec_id="test.bytes",
                request_digest="a" * 64,
            )
        )
    cancelled = await backend.commit_messaging_transition(
        MessagingTransition(
            kind="request_cancellation",
            transition_id=uuid4().hex,
            channel="output",
            identity=identity,
            settings=backend.messaging_settings,
        )
    )
    assert cancelled.cancellation_requested_by_transition


async def test_messaging_sql_conversion_preserves_payload_ownership_and_unbound_replay(
    storage_engine,
):
    backend = SqlAlchemyBackend(storage_engine)
    identity = await _seed_messaging(backend)
    target = ("tinkerfin_messaging_runs", "request_digest")
    await _without_column(storage_engine, *target)
    before = await _snapshot(storage_engine)
    report = await prepare_sql_storage(storage_engine, "messaging")
    assert report.schema_changes == ("add_column",) and not report.applied
    assert await _snapshot(storage_engine) == before
    await prepare_sql_storage(storage_engine, "messaging", apply=True)
    assert await _snapshot(storage_engine, target) == before
    repeated = await prepare_sql_storage(storage_engine, "messaging", apply=True)
    assert repeated.schema_changes == ()
    await _verify_messaging(SqlAlchemyBackend(storage_engine), identity)


async def test_automation_conversion_uses_each_saved_execution_limit_and_preserves_claims(
    storage_engine,
):
    store = SqlAlchemyAutomationStore(storage_engine)
    service = AutomationService(namespace="offline", store=store)
    try:
        first = await service.execute_once(
            owner_id="owner",
            target="work",
            request_id="one",
            limits=ExecutionLimits(max_concurrent_runs=2),
        )
        second = await service.execute_once(
            owner_id="owner",
            target="work",
            request_id="two",
            limits=ExecutionLimits(max_concurrent_runs=7),
        )
        batch = await store.claim_work(
            "offline",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=30),
            global_concurrency=8,
        )
        assert len(batch.claims) == 1
    finally:
        await service.close()
        await store.close()
    target = ("tinkerfin_automation_runs", "max_concurrent_runs")
    await _without_column(storage_engine, *target)
    before = await _snapshot(storage_engine)
    report = await prepare_sql_storage(storage_engine, "automation")
    assert report.missing_values == 2 and not report.applied
    assert await _snapshot(storage_engine) == before
    await prepare_sql_storage(storage_engine, "automation", apply=True)
    assert await _snapshot(storage_engine, target) == before
    async with storage_engine.connect() as connection:
        table = await connection.run_sync(
            lambda sync: Table(target[0], MetaData(), autoload_with=sync)
        )
        values = dict(
            (
                await connection.execute(
                    select(table.c.execution_id, table.c.max_concurrent_runs)
                )
            )
            .tuples()
            .all()
        )
    assert values == {first.execution_id: 2, second.execution_id: 7}
    repeated = await prepare_sql_storage(storage_engine, "automation", apply=True)
    assert repeated.missing_values == 0 and repeated.schema_changes == ()
    reopened = SqlAlchemyAutomationStore(storage_engine)
    try:
        await reopened.setup()
    finally:
        await reopened.close()


async def test_studio_conversion_preserves_registration_and_does_not_replace_preparation_ids(
    storage_engine,
):
    table = ConversationRunRegistration.__table__
    assert isinstance(table, Table)
    async with storage_engine.begin() as connection:
        await connection.run_sync(table.create)
        await connection.execute(
            table.insert().values(
                id=1,
                conversation_thread_id=10,
                run_id="registered",
                parent_run_id=None,
                model_id="model",
                access_mode="full",
                input_json={"runId": "registered"},
                started_at=datetime(2030, 1, 1),
                created_at=datetime(2030, 1, 1),
                updated_at=datetime(2030, 1, 1),
            )
        )
    target = ("conversation_run_registrations", "preparation_id")
    await _without_column(storage_engine, *target)
    before = await _snapshot(storage_engine)
    assert (await prepare_sql_storage(storage_engine, "studio")).missing_values == 1
    assert await _snapshot(storage_engine) == before
    await prepare_sql_storage(storage_engine, "studio", apply=True)
    assert await _snapshot(storage_engine, target) == before
    saved = await _snapshot(storage_engine)
    assert len(saved[target[0]][0][target[1]]) == 32
    await prepare_sql_storage(storage_engine, "studio", apply=True)
    assert await _snapshot(storage_engine) == saved


@pytest.mark.docker_integration
async def test_redis_conversion_preserves_hashes_expiry_capacity_and_unbound_replay(
    redis_test_service,
):
    prefix = f"offline-{uuid4().hex}"
    client = Redis.from_url(redis_test_service.url(0))
    backend = RedisBackend(client, key_prefix=prefix)
    try:
        identity = await _seed_messaging(backend)
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        run_key = next(key for key in keys if b":run:" in key)
        await cast(Awaitable[int], client.hdel(run_key.decode(), "request_digest"))
        before = {key: await client.dump(key) for key in keys}
        before_run = await cast(
            Awaitable[dict[bytes, bytes]], client.hgetall(run_key.decode())
        )
        expiry = {
            key: await cast(Awaitable[int], client.pexpiretime(key)) for key in keys
        }
        assert (
            await prepare_redis_storage(client, key_prefix=prefix)
        ).missing_values == 1
        assert {key: await client.dump(key) for key in keys} == before
        await prepare_redis_storage(client, key_prefix=prefix, apply=True)
        assert {key: await client.dump(key) for key in keys if key != run_key} == {
            key: value for key, value in before.items() if key != run_key
        }
        converted: Mapping[bytes, bytes] = await cast(
            Awaitable[dict[bytes, bytes]], client.hgetall(run_key.decode())
        )
        assert converted[b"request_digest"] == b""
        assert {
            key: value for key, value in converted.items() if key != b"request_digest"
        } == before_run
        assert {
            key: await cast(Awaitable[int], client.pexpiretime(key)) for key in keys
        } == expiry
        await cast(
            Awaitable[int], client.hset(run_key.decode(), "request_digest", "b" * 64)
        )
        assert (
            await prepare_redis_storage(client, key_prefix=prefix, apply=True)
        ).missing_values == 0
        assert (
            await cast(
                Awaitable[bytes], client.hget(run_key.decode(), "request_digest")
            )
            == b"b" * 64
        )
        await cast(Awaitable[int], client.hset(run_key.decode(), "request_digest", ""))
        await _verify_messaging(backend, identity)
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def test_invalid_execution_payload_prevents_any_schema_change(storage_engine):
    store = SqlAlchemyAutomationStore(storage_engine)
    service = AutomationService(namespace="offline", store=store)
    try:
        await service.execute_once(owner_id="owner", target="work")
    finally:
        await service.close()
        await store.close()
    await _without_column(
        storage_engine, "tinkerfin_automation_runs", "max_concurrent_runs"
    )
    async with storage_engine.begin() as connection:
        table = await connection.run_sync(
            lambda sync: Table(
                "tinkerfin_automation_runs", MetaData(), autoload_with=sync
            )
        )
        row = (await connection.execute(select(table.c.payload))).scalar_one()
        payload = json.loads(row)
        payload["limits"]["max_concurrent_runs"] = 0
        await connection.execute(table.update().values(payload=json.dumps(payload)))
    before = await _snapshot(storage_engine)
    with pytest.raises(StoragePreparationError, match="positive integer"):
        await prepare_sql_storage(storage_engine, "automation", apply=True)
    assert await _snapshot(storage_engine) == before
    async with storage_engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync: [
                column["name"]
                for column in inspect(sync).get_columns("tinkerfin_automation_runs")
            ]
        )
    assert "max_concurrent_runs" not in names


def test_apply_requires_explicit_stopped_writer_assertion(monkeypatch):
    monkeypatch.setattr("sys.argv", ["prepare_gateway_storage.py", "--apply"])
    with pytest.raises(SystemExit) as failure:
        main()
    assert failure.value.code == 2


async def test_selected_environment_is_required_and_no_dotenv_is_read(
    monkeypatch, tmp_path
):
    (tmp_path / ".env").write_text(
        "GATEWAY_STORAGE_TEST_ABSENT=sqlite+aiosqlite:///unselected.db\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GATEWAY_STORAGE_TEST_ABSENT", raising=False)
    args = argparse.Namespace(
        messaging_database_env="GATEWAY_STORAGE_TEST_ABSENT",
        automation_database_env=None,
        studio_database_env=None,
        redis_url_env=None,
        messaging_prefix=None,
        apply=False,
    )
    with pytest.raises(StoragePreparationError, match="environment variable"):
        await _execute(args)
