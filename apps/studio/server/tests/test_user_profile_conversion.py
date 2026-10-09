"""用户资料一次性整理只在当前测试独占的 MySQL 数据库执行"""

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from apps.studio.server.tools import convert_user_profile as conversion
from apps.studio.server.tools.convert_user_profile import (
    UserProfileBackup,
    convert_user_profile,
)
from sqlalchemy import event, text
from sqlalchemy.engine import AdaptedConnection, Connection, ExecutionContext, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.studio_mysql_integration


@pytest_asyncio.fixture
async def profile_database(mysql_admin_url: str) -> AsyncIterator[AsyncEngine]:
    name = f"tinkerfin_profile_{uuid4().hex}"
    url = make_url(mysql_admin_url)
    admin = create_async_engine(url)
    engine = create_async_engine(url.set(database=name), hide_parameters=True)
    created = False
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            created = True
        schema = (Path(__file__).parents[1] / "database/mysql/schema.sql").read_text()
        statements = [
            statement
            for statement in schema.split(";")
            if statement.strip().startswith(("CREATE TABLE users", "INSERT INTO users"))
        ]
        async with engine.begin() as connection:
            for statement in statements:
                await connection.exec_driver_sql(statement)
            await connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN avatar_content MEDIUMBLOB NULL, ADD COLUMN display_name VARCHAR(128) NOT NULL DEFAULT '测试昵称' COMMENT '展示名称' AFTER username"
            )
        yield engine
    finally:
        await engine.dispose()
        if created:
            async with admin.begin() as connection:
                await connection.exec_driver_sql(f"DROP DATABASE `{name}`")
                assert not await connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name"
                    ),
                    {"name": name},
                )
        await admin.dispose()


async def test_profile_conversion_is_explicit_backed_up_and_preserves_users(
    profile_database: AsyncEngine, tmp_path: Path
) -> None:
    engine = profile_database
    database = engine.url.database
    assert database is not None
    backup = tmp_path / "users.json"
    async with engine.connect() as connection:
        before = [
            dict(row)
            for row in (
                await connection.execute(text("SELECT * FROM users ORDER BY id"))
            ).mappings()
        ]
        ddl = (await connection.execute(text("SHOW CREATE TABLE users"))).one()[1]
    checked = await convert_user_profile(engine, expected_database=database)
    assert checked == {"database": database, "users": 1, "changes": 2, "applied": False}
    assert not backup.exists()
    with pytest.raises(ValueError, match="指定业务库"):
        await convert_user_profile(
            engine, expected_database="wrong", apply=True, backup=backup
        )
    with pytest.raises(ValueError, match="备份路径"):
        await convert_user_profile(engine, expected_database=database, apply=True)
    assert (
        await convert_user_profile(
            engine, expected_database=database, apply=True, backup=backup
        )
    )["applied"] is True
    saved = UserProfileBackup.model_validate_json(backup.read_text())
    assert saved.rows == before
    assert saved.ddl == ddl
    assert saved.indexes
    assert backup.stat().st_mode & 0o777 == 0o600
    async with engine.connect() as connection:
        row = (
            await connection.execute(text("SELECT username, avatar_url FROM users"))
        ).one()
        assert tuple(row) == ("tinkerfin", None)
    assert (await convert_user_profile(engine, expected_database=database))[
        "changes"
    ] == 0
    assert await user_table_locks(engine) == []


async def test_profile_conversion_never_overwrites_a_backup(
    profile_database: AsyncEngine, tmp_path: Path
) -> None:
    database = profile_database.url.database
    assert database is not None
    backup = tmp_path / "existing.json"
    backup.write_text("keep")
    with pytest.raises(FileExistsError):
        await convert_user_profile(
            profile_database, expected_database=database, apply=True, backup=backup
        )
    assert backup.read_text() == "keep"
    assert (await convert_user_profile(profile_database, expected_database=database))[
        "changes"
    ] == 2
    assert await user_table_locks(profile_database) == []


async def test_profile_conversion_refuses_to_discard_uploaded_images(
    profile_database: AsyncEngine, tmp_path: Path
) -> None:
    database = profile_database.url.database
    assert database is not None
    async with profile_database.begin() as connection:
        await connection.execute(
            text("UPDATE users SET avatar_content=:content"), {"content": b"image"}
        )
    backup = tmp_path / "users.json"
    with pytest.raises(ValueError, match="仍有数据"):
        await convert_user_profile(
            profile_database, expected_database=database, apply=True, backup=backup
        )
    assert not backup.exists()
    async with profile_database.connect() as connection:
        assert (
            await connection.scalar(text("SELECT avatar_content FROM users"))
            == b"image"
        )


@pytest.mark.parametrize("indexed_column", ["display_name", "avatar_content(8)"])
@pytest.mark.parametrize("apply", [False, True])
async def test_profile_conversion_refuses_indexed_columns_before_changes(
    profile_database: AsyncEngine,
    tmp_path: Path,
    indexed_column: str,
    apply: bool,
) -> None:
    database = profile_database.url.database
    assert database is not None
    async with profile_database.begin() as connection:
        await connection.exec_driver_sql(
            f"CREATE INDEX ix_users_profile_guard ON users ({indexed_column})"
        )
        before = (await connection.execute(text("SHOW CREATE TABLE users"))).one()[1]
    backup = tmp_path / "users.json"
    with pytest.raises(ValueError, match="参与索引"):
        await convert_user_profile(
            profile_database,
            expected_database=database,
            apply=apply,
            backup=backup,
        )
    assert not backup.exists()
    async with profile_database.connect() as connection:
        after = (await connection.execute(text("SHOW CREATE TABLE users"))).one()[1]
    assert after == before


async def user_table_locks(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.scalars(
                    text(
                        "SELECT LOCK_TYPE FROM performance_schema.metadata_locks "
                        "WHERE OBJECT_SCHEMA=:database AND OBJECT_NAME='users' "
                        "AND LOCK_STATUS='GRANTED'"
                    ),
                    {"database": engine.url.database},
                )
            ).all()
        )


@pytest_asyncio.fixture
async def backup_write_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[asyncio.Event, threading.Event]]:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finish = threading.Event()
    write_backup = conversion._write_backup

    def blocked_backup(path: Path, payload: str) -> None:
        loop.call_soon_threadsafe(started.set)
        finish.wait()
        write_backup(path, payload)

    monkeypatch.setattr(conversion, "_write_backup", blocked_backup)
    try:
        yield started, finish
    finally:
        finish.set()


async def test_backup_cancellation_releases_read_transaction_before_writing(
    profile_database: AsyncEngine,
    tmp_path: Path,
    backup_write_gate: tuple[asyncio.Event, threading.Event],
) -> None:
    database = profile_database.url.database
    assert database is not None
    started, finish = backup_write_gate
    backup = tmp_path / "users.json"
    operation = asyncio.create_task(
        convert_user_profile(
            profile_database,
            expected_database=database,
            apply=True,
            backup=backup,
        )
    )
    try:
        await started.wait()
        assert await user_table_locks(profile_database) == []
        operation.cancel()
        assert await user_table_locks(profile_database) == []
        assert not operation.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        saved = UserProfileBackup.model_validate_json(backup.read_text())
        async with profile_database.connect() as connection:
            rows = await connection.execute(text("SELECT * FROM users ORDER BY id"))
            assert [dict(row) for row in rows.mappings()] == saved.rows
            ddl = (await connection.execute(text("SHOW CREATE TABLE users"))).one()[1]
            assert ddl == saved.ddl
        assert await user_table_locks(profile_database) == []
    finally:
        finish.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("UPDATE users SET disabled=1", id="retained-value"),
        pytest.param("UPDATE users SET display_name='变化昵称'", id="removed-value"),
        pytest.param(
            "INSERT INTO users (username,password_hash,roles,disabled) "
            "VALUES ('second','hash','[]',0)",
            id="inserted-row",
        ),
        pytest.param("DELETE FROM users", id="deleted-row"),
        pytest.param("ALTER TABLE users COMMENT='并发修改'", id="ddl"),
        pytest.param("CREATE INDEX ix_users_disabled ON users (disabled)", id="index"),
    ],
)
async def test_profile_conversion_refuses_changes_during_backup(
    profile_database: AsyncEngine,
    tmp_path: Path,
    backup_write_gate: tuple[asyncio.Event, threading.Event],
    change: str,
) -> None:
    database = profile_database.url.database
    assert database is not None
    started, finish = backup_write_gate
    backup = tmp_path / "users.json"
    operation = asyncio.create_task(
        convert_user_profile(
            profile_database,
            expected_database=database,
            apply=True,
            backup=backup,
        )
    )
    try:
        await started.wait()
        assert await user_table_locks(profile_database) == []
        async with profile_database.begin() as connection:
            await connection.execute(text(change))
            changed_rows = [
                dict(row)
                for row in (
                    await connection.execute(text("SELECT * FROM users ORDER BY id"))
                ).mappings()
            ]
            changed_ddl = (
                await connection.execute(text("SHOW CREATE TABLE users"))
            ).one()[1]
        finish.set()
        with pytest.raises(RuntimeError, match="备份期间.*发生变化"):
            await operation
        saved = UserProfileBackup.model_validate_json(backup.read_text())
        assert saved.rows[0]["display_name"] == "测试昵称"
        assert saved.rows[0]["disabled"] == 0
        assert len(saved.rows) == 1
        async with profile_database.connect() as connection:
            rows = await connection.execute(text("SELECT * FROM users ORDER BY id"))
            assert [dict(row) for row in rows.mappings()] == changed_rows
            ddl = (await connection.execute(text("SHOW CREATE TABLE users"))).one()[1]
            assert ddl == changed_ddl
        assert await user_table_locks(profile_database) == []
    finally:
        finish.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize(
    "failure", ["cancel-lock", "cancel-read", "cancel-alter", "timeout-alter"]
)
async def test_profile_conversion_releases_table_lock_after_interruption(
    profile_database: AsyncEngine, tmp_path: Path, failure: str
) -> None:
    database = profile_database.url.database
    assert database is not None
    backup = tmp_path / "users.json"
    interrupted = asyncio.Event()
    finish = asyncio.Event()
    lock_acquired = False
    injected = False

    async def pause() -> None:
        interrupted.set()
        await finish.wait()
        if failure == "timeout-alter":
            raise TimeoutError("数据库操作超时")

    def interrupt_statement(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        nonlocal lock_acquired, injected
        del cursor, parameters, context, executemany
        if statement == "LOCK TABLES users WRITE":
            lock_acquired = True
        target = {
            "cancel-lock": "LOCK TABLES users WRITE",
            "cancel-read": "SELECT * FROM users",
        }.get(failure, "ALTER TABLE users ")
        if lock_acquired and not injected and statement.startswith(target):
            injected = True
            adapted = connection.connection.dbapi_connection
            assert isinstance(adapted, AdaptedConnection)
            adapted.run_async(lambda _: pause())

    event_name = (
        "after_cursor_execute"
        if failure in {"cancel-lock", "cancel-read"}
        else "before_cursor_execute"
    )
    event.listen(profile_database.sync_engine, event_name, interrupt_statement)
    operation = asyncio.create_task(
        convert_user_profile(
            profile_database,
            expected_database=database,
            apply=True,
            backup=backup,
        )
    )
    try:
        await interrupted.wait()
        assert "SHARED_NO_READ_WRITE" in await user_table_locks(profile_database)
        if failure.startswith("cancel"):
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            finish.set()
            with pytest.raises(TimeoutError, match="数据库操作超时"):
                await operation
        saved = UserProfileBackup.model_validate_json(backup.read_text())
        async with profile_database.connect() as connection:
            await connection.execute(text("LOCK TABLES users WRITE"))
            try:
                rows = await connection.execute(text("SELECT * FROM users ORDER BY id"))
                assert [dict(row) for row in rows.mappings()] == saved.rows
                ddl = (await connection.execute(text("SHOW CREATE TABLE users"))).one()[
                    1
                ]
                assert ddl == saved.ddl
            finally:
                await connection.execute(text("UNLOCK TABLES"))
        assert await user_table_locks(profile_database) == []
    finally:
        finish.set()
        await asyncio.gather(operation, return_exceptions=True)
        event.remove(profile_database.sync_engine, event_name, interrupt_statement)
