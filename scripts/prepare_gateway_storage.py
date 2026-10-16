"""Prepare stopped deployments for the current command and notification contracts.

This offline command never reads .env files. Supply connection strings through
explicitly selected environment variables. Apply only after stopping writers and
making a backup. MySQL DDL commits independently; an interrupted run can be
repeated after the cause is resolved. Existing data and populated bindings are
retained. This utility is not called by application or framework startup.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from typing import Literal, cast
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError
from redis.asyncio import Redis
from sqlalchemy import (
    CheckConstraint,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
    tuple_,
    update,
)
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.schema import AddConstraint, CreateColumn, SetColumnComment


class StoragePreparationError(ValueError):
    """Describe an actionable offline-conversion failure without stored content."""


Component = Literal["messaging", "automation", "studio"]
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_HEX_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_PREPARATION_ID = re.compile(r"[a-f0-9]{32}\Z")
_CONCURRENCY_CHECK = "ck_tinkerfin_automation_runs_concurrency"
_QUEUE_INDEX = "ix_tinkerfin_automation_runs_queue_deadline"
_QUEUE_COLUMNS = ("namespace", "status", "queue_deadline", "execution_id")


@dataclass(frozen=True, slots=True)
class StorageReport:
    """Report structural work and examined rows without revealing stored content."""

    component: str
    rows_checked: int
    missing_values: int
    schema_changes: tuple[str, ...]
    applied: bool


@dataclass(frozen=True, slots=True)
class _SqlTarget:
    component: Component
    table: str
    column: str
    comment: str
    length: int | None
    nullable: bool

    def declaration(self) -> Column[str] | Column[int]:
        if self.length is None:
            return Column(
                self.column, Integer, nullable=self.nullable, comment=self.comment
            )
        return Column(
            self.column,
            String(self.length),
            nullable=self.nullable,
            comment=self.comment,
        )


_TARGETS: dict[Component, _SqlTarget] = {
    "messaging": _SqlTarget(
        "messaging",
        "tinkerfin_messaging_runs",
        "request_digest",
        "Immutable lowercase SHA-256 command binding; NULL identifies an ordinary object stream",
        64,
        True,
    ),
    "automation": _SqlTarget(
        "automation",
        "tinkerfin_automation_runs",
        "max_concurrent_runs",
        "Immutable execution-specific concurrency limit for task or owner admission",
        None,
        False,
    ),
    "studio": _SqlTarget(
        "studio",
        "conversation_run_registrations",
        "preparation_id",
        "本次准备登记的随机标识，清理时比较以保护并发提交",
        32,
        False,
    ),
}


def _reflect(connection: Connection, target: _SqlTarget) -> Table:
    if not inspect(connection).has_table(target.table):
        raise StoragePreparationError(
            f"{target.component}: selected database has no expected table"
        )
    return Table(target.table, MetaData(), autoload_with=connection)


def _schema_changes(
    connection: Connection, target: _SqlTarget, table: Table
) -> tuple[str, ...]:
    changes: list[str] = []
    column = table.c.get(target.column)
    if column is None:
        changes.append("add_column")
    else:
        if target.length is None:
            valid_type = isinstance(column.type, Integer)
        else:
            valid_type = (
                isinstance(column.type, String) and column.type.length == target.length
            )
        if not valid_type:
            raise StoragePreparationError(
                f"{target.component}: existing column has an unexpected type"
            )
        if column.nullable != target.nullable:
            if target.nullable:
                raise StoragePreparationError(
                    "messaging: command binding column must be nullable"
                )
            changes.append("require_value")
        if connection.dialect.name != "sqlite" and column.comment != target.comment:
            changes.append("column_comment")
    if target.component == "automation":
        check = next(
            (item for item in table.constraints if item.name == _CONCURRENCY_CHECK),
            None,
        )
        if check is None:
            changes.append("concurrency_constraint")
        elif (
            not isinstance(check, CheckConstraint)
            or re.sub(r'[\s()"`]', "", str(check.sqltext)) != "max_concurrent_runs>=1"
        ):
            raise StoragePreparationError(
                "automation: concurrency constraint has an unexpected definition"
            )
        index = next(
            (item for item in table.indexes if item.name == _QUEUE_INDEX), None
        )
        if index is None:
            changes.append("queue_deadline_index")
        elif (
            tuple(column.name for column in index.columns) != _QUEUE_COLUMNS
            or index.unique
        ):
            raise StoragePreparationError(
                "automation: queue deadline index has an unexpected definition"
            )
    return tuple(changes)


async def _rows(connection: AsyncConnection, table: Table) -> AsyncIterator[RowMapping]:
    # Bounded keyset reads release their result before subsequent writes or DDL.
    # asyncpg server cursors can retain a portal until transaction end even after
    # SQLAlchemy closes the client result; those portals prevent ALTER TABLE.
    keys = tuple(table.primary_key.columns)
    if not keys:
        raise StoragePreparationError("Expected a primary key for offline conversion")
    last: tuple[str | int | bytes, ...] | None = None
    while True:
        statement = select(table).order_by(*keys).limit(100)
        if last is not None:
            statement = statement.where(tuple_(*keys) > last)
        page = (await connection.execute(statement)).mappings().all()
        if not page:
            return
        for row in page:
            yield row
        values: list[str | int | bytes] = []
        for key in keys:
            value: object = page[-1][key.name]
            if not isinstance(value, (int, str, bytes)):
                raise TypeError("Unexpected primary-key value")
            values.append(value)
        last = tuple(values)


def _concurrency(row: RowMapping) -> int:
    payload: object = row["payload"]
    if not isinstance(payload, str):
        raise TypeError("automation: execution payload must be JSON text")
    try:
        document = _JSON_OBJECT.validate_json(payload)
    except ValidationError as error:
        raise StoragePreparationError(
            "automation: execution payload is invalid JSON"
        ) from error
    limits = document.get("limits")
    value = limits.get("max_concurrent_runs") if isinstance(limits, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise StoragePreparationError(
            "automation: execution concurrency limit must be a positive integer"
        )
    return value


async def _validate_rows(
    connection: AsyncConnection, target: _SqlTarget, table: Table
) -> tuple[int, int]:
    checked = missing = 0
    if target.component == "messaging":
        async for row in _rows(connection, table):
            checked += 1
            value: object = row.get(target.column)
            if value is not None and (
                not isinstance(value, str) or _HEX_DIGEST.fullmatch(value) is None
            ):
                raise StoragePreparationError(
                    "messaging: existing command digest is invalid"
                )
        return checked, 0
    async for row in _rows(connection, table):
        checked += 1
        saved: object = row.get(target.column)
        if target.component == "automation":
            expected = _concurrency(row)
            if saved is not None and (type(saved) is not int or saved != expected):
                raise StoragePreparationError(
                    "automation: saved concurrency limit conflicts with execution payload"
                )
        elif saved is not None and (
            not isinstance(saved, str) or _PREPARATION_ID.fullmatch(saved) is None
        ):
            raise StoragePreparationError(
                "studio: existing preparation identity is invalid"
            )
        missing += saved is None
    return checked, missing


async def _fill_values(
    connection: AsyncConnection, target: _SqlTarget, table: Table
) -> None:
    if target.nullable:
        return
    key = tuple(table.primary_key.columns)[0]
    async for row in _rows(connection, table):
        if row[target.column] is not None:
            continue
        value = _concurrency(row) if target.component == "automation" else uuid4().hex
        await connection.execute(
            update(table).where(key == row[key.name]).values({target.column: value})
        )


def _rebuild_sqlite(connection: Connection, target: _SqlTarget, table: Table) -> None:
    # SQLite cannot tighten nullability or add a table CHECK with ALTER COLUMN.
    # Copy every column inside BEGIN IMMEDIATE; preserve all keys and indexes.
    if inspect(connection).get_foreign_keys(table.name):
        raise StoragePreparationError(
            "SQLite conversion does not accept foreign-key-owned tables"
        )
    triggers = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
        (table.name,),
    ).all()
    if triggers:
        raise StoragePreparationError(
            "SQLite conversion does not accept tables with triggers"
        )
    temporary = table.to_metadata(
        MetaData(), name=f"gateway_storage_copy_{uuid4().hex}"
    )
    temporary.c[target.column].nullable = target.nullable
    if target.component == "automation" and not any(
        item.name == _CONCURRENCY_CHECK for item in temporary.constraints
    ):
        temporary.append_constraint(
            CheckConstraint("max_concurrent_runs >= 1", name=_CONCURRENCY_CHECK)
        )
    indexes = tuple(
        (index.name, tuple(column.name for column in index.columns), index.unique)
        for index in table.indexes
    )
    if any(
        index.expressions != list(index.columns) or any(index.dialect_kwargs)
        for index in table.indexes
    ):
        raise StoragePreparationError(
            "SQLite conversion requires ordinary column indexes"
        )
    temporary.indexes.clear()
    temporary.create(connection)
    connection.execute(
        temporary.insert().from_select(tuple(table.c.keys()), select(table))
    )
    table.drop(connection)
    quote = connection.dialect.identifier_preparer.quote
    connection.exec_driver_sql(
        f"ALTER TABLE {quote(temporary.name)} RENAME TO {quote(table.name)}"
    )
    current = _reflect(connection, target)
    for name, columns, unique in indexes:
        Index(name, *(current.c[column] for column in columns), unique=unique).create(
            connection
        )


async def prepare_sql_storage(
    engine: AsyncEngine, component: Component, *, apply: bool = False
) -> StorageReport:
    """Validate and optionally convert one explicitly stopped SQL storage boundary.

    Uses bounded row pages and preserves existing values. The caller owns the
    engine, backup, and writer shutdown. Reruns can complete partially applied
    MySQL DDL; no application runtime recognizes earlier database shapes.
    """
    target = _TARGETS[component]
    if engine.dialect.name not in {"mysql", "postgresql", "sqlite"}:
        raise StoragePreparationError(
            "Supported SQL dialects are MySQL, PostgreSQL, and SQLite"
        )
    async with engine.begin() as connection:
        if apply and engine.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        table = await connection.run_sync(_reflect, target)
        changes = await connection.run_sync(_schema_changes, target, table)
        checked, missing = await _validate_rows(connection, target, table)
        if not apply:
            return StorageReport(component, checked, missing, changes, False)
        quote = engine.dialect.identifier_preparer.quote
        name, field = quote(target.table), quote(target.column)
        if "add_column" in changes:
            column = target.declaration()
            column.nullable = True
            declaration = str(CreateColumn(column).compile(dialect=engine.dialect))
            await connection.exec_driver_sql(
                f"ALTER TABLE {name} ADD COLUMN {declaration}"
            )
            table = await connection.run_sync(_reflect, target)
        await _fill_values(connection, target, table)
        if not target.nullable and (changes or missing):
            if engine.dialect.name == "sqlite":
                await connection.run_sync(_rebuild_sqlite, target, table)
                table = await connection.run_sync(_reflect, target)
            else:
                if engine.dialect.name == "mysql":
                    declaration = str(
                        CreateColumn(target.declaration()).compile(
                            dialect=engine.dialect
                        )
                    )
                    await connection.exec_driver_sql(
                        f"ALTER TABLE {name} MODIFY COLUMN {declaration}"
                    )
                else:
                    await connection.exec_driver_sql(
                        f"ALTER TABLE {name} ALTER COLUMN {field} SET NOT NULL"
                    )
        if component == "automation":
            if "concurrency_constraint" in changes and engine.dialect.name != "sqlite":
                check = CheckConstraint(
                    "max_concurrent_runs >= 1", name=_CONCURRENCY_CHECK
                )
                table.append_constraint(check)
                await connection.execute(AddConstraint(check))
            if "queue_deadline_index" in changes:
                index = Index(
                    _QUEUE_INDEX, *(table.c[column] for column in _QUEUE_COLUMNS)
                )
                await connection.run_sync(index.create)
        if engine.dialect.name == "postgresql" and changes:
            table.c[target.column].comment = target.comment
            await connection.execute(SetColumnComment(table.c[target.column]))
        elif engine.dialect.name == "mysql" and target.nullable and changes:
            declaration = str(
                CreateColumn(target.declaration()).compile(dialect=engine.dialect)
            )
            await connection.exec_driver_sql(
                f"ALTER TABLE {name} MODIFY COLUMN {declaration}"
            )
        current = await connection.run_sync(_reflect, target)
        if await connection.run_sync(_schema_changes, target, current):
            raise StoragePreparationError(
                f"{component}: storage structure did not reach its current contract"
            )
        await _validate_rows(connection, target, current)
        return StorageReport(component, checked, missing, changes, True)


_REDIS_FILL = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if redis.call('TYPE', KEYS[1]).ok ~= 'hash' then return redis.error_reply('unexpected run type') end
if redis.call('HGET', KEYS[1], 'run') ~= ARGV[1] then return redis.error_reply('run identity changed') end
return redis.call('HSETNX', KEYS[1], 'request_digest', '')
"""


async def prepare_redis_storage(
    client: Redis, *, key_prefix: str, apply: bool = False
) -> StorageReport:
    """Fill missing command bindings under one exact Messaging Redis namespace.

    The caller stops writers and owns the client. Existing nonempty digests,
    payloads, capacity counters, ownership fields, and absolute expiry stay intact.
    An expired hash is never recreated. rows_checked counts SCAN entries, which
    Redis may repeat; HSETNX makes the actual conversion idempotent.
    """
    if apply:
        await prepare_redis_storage(client, key_prefix=key_prefix)
    if not key_prefix or key_prefix.strip() != key_prefix:
        raise StoragePreparationError("key_prefix must be a nonempty canonical string")
    namespace = f"{key_prefix}:{{{hashlib.sha256(key_prefix.encode()).hexdigest()}}}"
    pattern = re.compile(
        re.escape(namespace)
        + r":channel:[a-f0-9]{64}:stream:[a-f0-9]{64}:generation:[1-9][0-9]*:run:([a-f0-9]{64})\Z"
    )
    glob = (
        "".join("\\" + char if char in "*?[]\\" else char for char in namespace)
        + ":channel:*:stream:*:generation:*:run:*"
    )
    checked = missing = 0
    async for key in client.scan_iter(match=glob, count=100):
        if not isinstance(key, bytes):
            raise TypeError("Redis client must use decode_responses=False")
        matched = pattern.fullmatch(key.decode())
        if matched is None:
            raise StoragePreparationError(
                "Unexpected key inside the selected Messaging namespace"
            )
        # redis.asyncio returns awaitables; supported redis-py stubs also expose
        # the synchronous command annotations inherited by this concrete client.
        raw = await cast(Awaitable[Mapping[bytes, bytes]], client.hgetall(key.decode()))
        if not raw:
            continue
        run = raw.get(b"run")
        if run is None or hashlib.sha256(run).hexdigest() != matched[1]:
            raise StoragePreparationError("Redis run identity does not match its key")
        digest = raw.get(b"request_digest")
        if digest not in (None, b"") and _HEX_DIGEST.fullmatch(digest.decode()) is None:
            raise StoragePreparationError("Existing Redis command digest is invalid")
        checked += 1
        if digest is None:
            if apply:
                changed: object = await cast(
                    Awaitable[int], client.eval(_REDIS_FILL, 1, key, run)
                )
                if changed not in (0, 1):
                    raise StoragePreparationError(
                        "Redis conversion did not return a field count"
                    )
                missing += int(cast(int, changed))
            else:
                missing += 1
    return StorageReport(
        "redis",
        checked,
        missing,
        ("missing_command_bindings",) if missing else (),
        apply,
    )


def _environment(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None or not os.environ.get(
        name
    ):
        raise StoragePreparationError(
            "An explicitly selected connection environment variable is missing"
        )
    return os.environ[name]


async def _execute(args: argparse.Namespace) -> list[StorageReport]:
    sql: list[tuple[Component, AsyncEngine]] = []
    redis: Redis | None = None
    async with AsyncExitStack() as resources:
        for component in _TARGETS:
            variable: str | None = getattr(args, f"{component}_database_env")
            if variable is not None:
                engine = create_async_engine(
                    _environment(variable), hide_parameters=True
                )
                resources.push_async_callback(engine.dispose)
                sql.append((component, engine))
        if args.redis_url_env:
            if not args.messaging_prefix:
                raise StoragePreparationError(
                    "--messaging-prefix is required with Redis storage"
                )
            redis = Redis.from_url(
                _environment(args.redis_url_env),
                socket_connect_timeout=5,
                socket_timeout=30,
            )
            resources.push_async_callback(redis.aclose)
        if not sql and redis is None:
            raise StoragePreparationError("Select at least one storage boundary")
        reports = [
            await prepare_sql_storage(engine, component) for component, engine in sql
        ]
        if redis is not None:
            reports.append(
                await prepare_redis_storage(redis, key_prefix=args.messaging_prefix)
            )
        if args.apply:
            reports = [
                await prepare_sql_storage(engine, component, apply=True)
                for component, engine in sql
            ]
            if redis is not None:
                reports.append(
                    await prepare_redis_storage(
                        redis, key_prefix=args.messaging_prefix, apply=True
                    )
                )
        return reports


def main() -> int:
    """Run explicit preflight or apply without exposing connection strings."""
    parser = argparse.ArgumentParser(description=__doc__)
    for component in _TARGETS:
        parser.add_argument(f"--{component}-database-env", metavar="NAME")
    parser.add_argument("--redis-url-env", metavar="NAME")
    parser.add_argument("--messaging-prefix")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if args.apply and not args.writers_stopped:
        parser.error(
            "--apply requires --writers-stopped after draining writers and making a backup"
        )
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive")

    async def run() -> list[StorageReport]:
        async with asyncio.timeout(args.timeout):
            return await _execute(args)

    try:
        reports = asyncio.run(run())
    except StoragePreparationError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - driver text can contain credentials or payloads
        print(f"Storage preparation failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps([asdict(report) for report in reports], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
