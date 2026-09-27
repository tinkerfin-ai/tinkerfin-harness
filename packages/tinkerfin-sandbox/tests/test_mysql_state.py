from __future__ import annotations

import asyncio
from typing import Never

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from tests.support.sql_engines import SqlEngineFactory
from tests.support.sql_faults import after_sql_commit

import tinkerfin_sandbox
from tinkerfin_sandbox import (
    SQLAlchemyOpenSandboxState,
    get_sqlalchemy_opensandbox_state_schema,
)


def _ddl_statements(ddl: str) -> tuple[str, ...]:
    return tuple(
        statement.strip().removesuffix(";")
        for statement in ddl.split(";\n\n")
        if statement.strip()
    )


def _reflect_mysql_schema(
    connection: Connection,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    inspector = inspect(connection)
    table_names = tuple(
        sorted(
            name
            for name in inspector.get_table_names()
            if name.startswith("tinkerfin_opensandbox_")
        )
    )
    index_names = tuple(
        sorted(
            str(index["name"])
            for table_name in table_names
            for index in inspector.get_indexes(table_name)
            if index.get("name") is not None
        )
    )
    table_comments = tuple(
        str(inspector.get_table_comment(table_name).get("text") or "")
        for table_name in table_names
    )
    column_comments = tuple(
        str(column.get("comment") or "")
        for table_name in table_names
        for column in inspector.get_columns(table_name)
    )
    return table_names, index_names, table_comments, column_comments


async def _reset_and_apply_exported_schema(mysql_url: str) -> None:
    schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            for table_name in reversed(schema.table_names):
                await connection.exec_driver_sql(f"DROP TABLE IF EXISTS `{table_name}`")
            for statement in _ddl_statements(schema.ddl):
                await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


@pytest.mark.docker_integration
@pytest.mark.mysql_integration
async def test_mysql57_fallback_restores_borrowed_session_lock_wait(
    mysql57_sandbox_url: str,
) -> None:
    engine = create_async_engine(
        mysql57_sandbox_url,
        pool_size=2,
        max_overflow=0,
    )
    state = SQLAlchemyOpenSandboxState(
        engine=engine,
        namespace="integration-mysql57-session-restore",
        poll_interval=0.01,
    )
    try:
        await state.start(warm_pool_size=0)
        async with (
            engine.connect() as first_connection,
            engine.connect() as second_connection,
        ):
            await first_connection.exec_driver_sql(
                "SET SESSION innodb_lock_wait_timeout = 37"
            )
            await second_connection.exec_driver_sql(
                "SET SESSION innodb_lock_wait_timeout = 37"
            )
            await first_connection.rollback()
            await second_connection.rollback()
        claim = await state.acquire_owner("mysql57-owner")
        await state.release_owner(claim)
        await state.aclose()
        async with (
            engine.connect() as first_connection,
            engine.connect() as second_connection,
        ):
            values = {
                (
                    await connection.exec_driver_sql(
                        "SELECT @@SESSION.innodb_lock_wait_timeout"
                    )
                ).scalar_one()
                for connection in (first_connection, second_connection)
            }
            assert values == {37}
    finally:
        await state.aclose()
        await engine.dispose()


async def _mysql_pool_connection_ids(engine: AsyncEngine) -> set[int]:
    async with (
        engine.connect() as first_connection,
        engine.connect() as second_connection,
    ):
        return {
            await connection.run_sync(
                lambda sync_connection: id(sync_connection.connection.dbapi_connection)
            )
            for connection in (first_connection, second_connection)
        }


async def _mysql_pool_lock_wait_values(engine: AsyncEngine) -> set[int]:
    async with (
        engine.connect() as first_connection,
        engine.connect() as second_connection,
    ):
        return {
            int(
                (
                    await connection.exec_driver_sql(
                        "SELECT @@SESSION.innodb_lock_wait_timeout"
                    )
                ).scalar_one()
            )
            for connection in (first_connection, second_connection)
        }


@pytest.mark.docker_integration
@pytest.mark.mysql_integration
async def test_mysql57_borrowed_settlement_restores_or_invalidates_session(
    mysql57_sandbox_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(mysql57_sandbox_url, pool_size=2, max_overflow=0)
    state = SQLAlchemyOpenSandboxState(
        engine=engine,
        namespace="integration-mysql57-settlement",
        poll_interval=0.01,
    )
    await state.start(warm_pool_size=0)
    async with (
        engine.connect() as first_connection,
        engine.connect() as second_connection,
    ):
        for connection in (first_connection, second_connection):
            await connection.exec_driver_sql(
                "SET SESSION innodb_lock_wait_timeout = 37"
            )
            await connection.rollback()
    failure = RuntimeError("borrowed MySQL operation failed")

    async def fail_operation(_connection: AsyncConnection) -> Never:
        raise failure

    entered = asyncio.Event()
    release_operation = asyncio.Event()

    async def block_operation(_connection: AsyncConnection) -> None:
        entered.set()
        await release_operation.wait()

    with pytest.raises(RuntimeError) as captured:
        await state._run_write_transaction(fail_operation)
    assert captured.value is failure
    assert await _mysql_pool_lock_wait_values(engine) == {37}

    operation = asyncio.create_task(state._run_write_transaction(block_operation))
    await entered.wait()
    operation.cancel("borrowed MySQL operation cancelled")
    await asyncio.sleep(0)
    # Complete the operation owned by the test so State can roll it back and
    # return the connection without interrupting unconsumed driver results.
    release_operation.set()
    with pytest.raises(
        asyncio.CancelledError,
        match="borrowed MySQL operation cancelled",
    ):
        await operation
    assert await _mysql_pool_lock_wait_values(engine) == {37}

    original_ids = await _mysql_pool_connection_ids(engine)
    failure = OperationalError("COMMIT", None, RuntimeError("response lost"))

    async def lose_acknowledgement() -> Never:
        raise failure

    with after_sql_commit(engine, lose_acknowledgement):
        with pytest.raises(
            tinkerfin_sandbox.OpenSandboxStateCommitUncertainError
        ) as captured_uncertainty:
            await state.enqueue_cleanup("uncertain-target")
    assert captured_uncertainty.value.cause is failure
    assert await _mysql_pool_connection_ids(engine) != original_ids

    await state.aclose()
    await engine.dispose()


@pytest.mark.docker_integration
@pytest.mark.mysql_integration
async def test_mysql57_cancelled_failed_commit_invalidates_without_warning(
    mysql57_sandbox_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(mysql57_sandbox_url, pool_size=2, max_overflow=0)
    state = SQLAlchemyOpenSandboxState(
        engine=engine,
        namespace="integration-mysql57-cancelled-commit",
        poll_interval=0.01,
    )
    await state.start(warm_pool_size=0)
    original_ids = await _mysql_pool_connection_ids(engine)
    commit_entered = asyncio.Event()
    release_commit = asyncio.Event()
    original_commit = AsyncConnection.commit
    loop = asyncio.get_running_loop()
    original_exception_handler = loop.get_exception_handler()
    loop_errors: list[dict[str, object]] = []

    async def fail_commit(_connection: AsyncConnection) -> Never:
        commit_entered.set()
        await release_commit.wait()
        raise OperationalError(
            "COMMIT",
            None,
            RuntimeError("commit response failed"),
            connection_invalidated=False,
        )

    async def no_op(_connection: AsyncConnection) -> None:
        return None

    def capture_loop_error(
        _loop: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        loop_errors.append(context)

    monkeypatch.setattr(AsyncConnection, "commit", fail_commit)
    loop.set_exception_handler(capture_loop_error)
    operation = asyncio.create_task(state._run_write_transaction(no_op))
    try:
        await commit_entered.wait()
        operation.cancel("caller cancelled during MySQL commit")
        release_commit.set()
        with pytest.raises(
            asyncio.CancelledError,
            match="caller cancelled during MySQL commit",
        ):
            await operation
        await asyncio.sleep(0)
    finally:
        release_commit.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        loop.set_exception_handler(original_exception_handler)
        monkeypatch.setattr(AsyncConnection, "commit", original_commit)
    assert await _mysql_pool_connection_ids(engine) != original_ids
    assert loop_errors == []
    await state.aclose()
    await engine.dispose()


@pytest.mark.mysql_integration
async def test_mysql8_export_and_runtime_claims_are_compatible(
    sql_engine: SqlEngineFactory,
    mysql_sandbox_url: str,
) -> None:
    await _reset_and_apply_exported_schema(mysql_sandbox_url)
    observed_sql: list[str] = []
    owner_queried = asyncio.Event()
    first = SQLAlchemyOpenSandboxState(
        engine=sql_engine(mysql_sandbox_url),
        namespace="integration-mysql8",
        lease_ttl=1.0,
        poll_interval=0.01,
        sqlite_retry_timeout=0,
    )
    second = SQLAlchemyOpenSandboxState(
        engine=sql_engine(mysql_sandbox_url),
        namespace="integration-mysql8",
        lease_ttl=1.0,
        poll_interval=0.01,
        sqlite_retry_timeout=0,
    )

    def capture_sql(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        observed_sql.append(statement.upper())
        if (
            statement.startswith("SELECT")
            and "tinkerfin_opensandbox_owners" in statement
        ):
            owner_queried.set()

    event.listen(first._engine.sync_engine, "before_cursor_execute", capture_sql)
    event.listen(second._engine.sync_engine, "before_cursor_execute", capture_sql)
    try:
        await asyncio.gather(
            first.start(warm_pool_size=2),
            second.start(warm_pool_size=2),
        )
        capabilities = first._require_capabilities()
        assert capabilities.server_version[:2] == (8, 4)
        assert capabilities.skip_locked is True

        initial_owner = await first.acquire_owner("serialized-owner")
        owner_queried.clear()
        waiting_owner = asyncio.create_task(second.acquire_owner("serialized-owner"))
        await owner_queried.wait()
        assert not waiting_owner.done()
        await first.release_owner(initial_owner)
        successor_owner = await waiting_owner
        assert successor_owner.generation > initial_owner.generation
        await second.release_owner(successor_owner)

        warm_claims = await asyncio.gather(
            first.claim_warm_slot(),
            second.claim_warm_slot(),
        )
        assert all(claim is not None for claim in warm_claims)
        first_warm = warm_claims[0]
        second_warm = warm_claims[1]
        assert first_warm is not None
        assert second_warm is not None
        assert first_warm.slot != second_warm.slot
        await asyncio.gather(
            first.publish_warm(first_warm, "warm-a"),
            second.publish_warm(second_warm, "warm-b"),
        )

        first_owner, second_owner = await asyncio.gather(
            first.acquire_owner("owner-a"),
            second.acquire_owner("owner-b"),
        )
        bindings = await asyncio.gather(
            first.consume_warm(first_owner),
            second.consume_warm(second_owner),
        )
        assert {binding.sandbox_id for binding in bindings if binding is not None} == {
            "warm-a",
            "warm-b",
        }
        assert await first.read_binding("owner-a") == bindings[0]
        assert await second.read_binding("owner-b") == bindings[1]
        await asyncio.gather(
            first.release_owner(first_owner),
            second.release_owner(second_owner),
        )

        await asyncio.gather(
            first.enqueue_cleanup("cleanup-a"),
            second.enqueue_cleanup("cleanup-b"),
        )
        concurrent_cleanup_claims = await asyncio.gather(
            first.claim_cleanup(),
            second.claim_cleanup(),
        )
        cleanup_claims = (
            concurrent_cleanup_claims[0] or await first.claim_cleanup(),
            concurrent_cleanup_claims[1] or await second.claim_cleanup(),
        )
        assert all(claim is not None for claim in cleanup_claims)
        claimed_cleanup_ids = {
            claim.sandbox_id for claim in cleanup_claims if claim is not None
        }
        assert claimed_cleanup_ids == {"cleanup-a", "cleanup-b"}
        await asyncio.gather(
            *(
                first.complete_cleanup(claim)
                for claim in cleanup_claims
                if claim is not None
            )
        )

        stale_warm = await first.claim_warm_slot()
        other_stale_warm = await first.claim_warm_slot()
        assert stale_warm is not None
        assert other_stale_warm is not None
        assert stale_warm.slot != other_stale_warm.slot
        async with first._engine.begin() as connection:
            await connection.exec_driver_sql(
                "UPDATE tinkerfin_opensandbox_warm_slots SET lease_expires_at = '2000-01-01 00:00:00'"
            )
        replacement_warm = await second.claim_warm_slot()
        assert replacement_warm is not None
        assert replacement_warm.slot == min(stale_warm.slot, other_stale_warm.slot)
        prior_generation = (
            stale_warm.generation
            if replacement_warm.slot == stale_warm.slot
            else other_stale_warm.generation
        )
        assert replacement_warm.generation > prior_generation
        await second.release_warm(replacement_warm)
        await first.release_warm(stale_warm)
        await first.release_warm(other_stale_warm)

        await first.enqueue_cleanup("cleanup-expiring")
        stale_cleanup = await first.claim_cleanup()
        assert stale_cleanup is not None
        async with first._engine.begin() as connection:
            await connection.exec_driver_sql(
                "UPDATE tinkerfin_opensandbox_cleanup SET lease_expires_at = '2000-01-01 00:00:00'"
            )
        replacement_cleanup = await second.claim_cleanup()
        assert replacement_cleanup is not None
        assert replacement_cleanup.sandbox_id == stale_cleanup.sandbox_id
        assert replacement_cleanup.generation > stale_cleanup.generation
        await second.complete_cleanup(replacement_cleanup)
    finally:
        event.remove(first._engine.sync_engine, "before_cursor_execute", capture_sql)
        event.remove(second._engine.sync_engine, "before_cursor_execute", capture_sql)
        await asyncio.gather(first.aclose(), second.aclose())

    engine = create_async_engine(mysql_sandbox_url)
    try:
        async with engine.connect() as connection:
            reflected = await connection.run_sync(_reflect_mysql_schema)
    finally:
        await engine.dispose()

    table_names, index_names, table_comments, column_comments = reflected
    schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
    assert table_names == schema.table_names
    assert index_names == (
        "ix_tinkerfin_opensandbox_cleanup_lease",
        "ix_tinkerfin_opensandbox_holders_binding",
        "ix_tinkerfin_opensandbox_owners_lease",
        "ix_tinkerfin_opensandbox_warm_slots_available",
        "ix_tinkerfin_opensandbox_workers_lease",
    )
    assert len(table_comments) == 6
    assert all(table_comments)
    assert len(column_comments) == 41
    assert all(column_comments)
    claim_sql = tuple(sql for sql in observed_sql if " FOR UPDATE" in sql)
    assert claim_sql
    assert any("TINKERFIN_OPENSANDBOX_WARM_SLOTS" in sql for sql in claim_sql)
    assert any("TINKERFIN_OPENSANDBOX_CLEANUP" in sql for sql in claim_sql)
    assert any("FOR UPDATE SKIP LOCKED" in sql for sql in claim_sql)
