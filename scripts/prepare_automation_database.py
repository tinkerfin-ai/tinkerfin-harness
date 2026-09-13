"""Copy a stopped Studio database into an empty database using the current schema.

The source is never modified. URLs are supplied explicitly through environment
variables; the tool does not load project .env files. Keep the matching Redis
checkpoint database and attachment volume when switching the application.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from pydantic import JsonValue, TypeAdapter
from sqlalchemy import MetaData, Table, func, insert, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin_automation._codec import (
    decode_execution,
    decode_task,
    encode_execution,
    encode_task,
)
from tinkerfin_automation.sql_schema import metadata as automation_metadata
from tinkerfin_studio.application import create_application
from tinkerfin_studio.infrastructure.database import Base

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def _object(value: object) -> dict[str, JsonValue]:
    return (
        _JSON_OBJECT.validate_json(value)
        if isinstance(value, str)
        else _JSON_OBJECT.validate_python(value)
    )


def _payload(kind: str, value: object) -> str:
    payload = _object(value)
    if kind == "task":
        schedule = payload.get("schedule")
        if not isinstance(schedule, dict):
            raise ValueError("Stored task has no schedule object")
        schedule.setdefault("active_from", None)
        schedule.setdefault("active_until", None)
        return encode_task(decode_task(json.dumps(payload, allow_nan=False)))
    payload.setdefault("task_name", None)
    return encode_execution(decode_execution(json.dumps(payload, allow_nan=False)))


def _convert_row(name: str, original: dict[str, object]) -> dict[str, object]:
    row = dict(original)
    if name == "conversation_threads":
        row.setdefault(
            "last_access_mode", "write_approval" if row.get("last_run_id") else "full"
        )
    elif name == "conversation_run_registrations":
        row.setdefault("access_mode", "write_approval")
        payload = _object(row["input_json"])
        forwarded = payload.get("forwardedProps")
        if not isinstance(forwarded, dict):
            raise ValueError("Stored run has no forwardedProps object")
        access_mode = row["access_mode"]
        if access_mode not in {"full", "write_approval"}:
            raise ValueError("Stored run has an invalid access mode")
        assert isinstance(access_mode, str)
        forwarded["accessMode"] = access_mode
        row["input_json"] = payload
    elif name == "tinkerfin_automation_tasks":
        row["payload"] = _payload("task", row["payload"])
        name_value = row["name"]
        if not isinstance(name_value, str):
            raise ValueError("Stored task has no name")
        row["search_name"] = name_value.casefold()
    elif name == "tinkerfin_automation_runs":
        payload = _payload("execution", row["payload"])
        row["payload"] = payload
        task_name = decode_execution(payload).task_name
        row["task_name"] = task_name
        row["search_name"] = task_name.casefold() if task_name else None
    elif name == "tinkerfin_automation_operations" and row["result_kind"] in {
        "task",
        "execution",
    }:
        kind = row["result_kind"]
        assert isinstance(kind, str)
        row["result_payload"] = _payload(kind, row["result_payload"])
    return row


def _distinct_databases(source: AsyncEngine, destination: AsyncEngine) -> None:
    family = source.dialect.name
    if family != destination.dialect.name or family not in {"mysql", "sqlite"}:
        raise ValueError("Use two MySQL databases or two file-backed SQLite databases")
    if family == "sqlite":
        first, second = source.url.database, destination.url.database
        if (
            not first
            or not second
            or ":memory:" in {first, second}
            or Path(first).resolve() == Path(second).resolve()
        ):
            raise ValueError("Source and destination must be distinct SQLite files")
    elif (
        not destination.url.database or source.url.database == destination.url.database
    ):
        raise ValueError("Source and destination must have different database names")


async def prepare_database(
    source: AsyncEngine, destination: AsyncEngine, *, apply: bool = False
) -> dict[str, int]:
    """Validate or copy data into an empty destination without modifying the source.

    Args:
        source: Borrowed engine for a stopped application's source database.
        destination: Borrowed engine for a distinct, disposable empty database.
        apply: Write only to the destination when explicitly enabled.

    Returns:
        Source row counts after validating every transformed row.

    Raises:
        ValueError: Databases overlap, the destination is occupied, work is active,
            or a stored record cannot be converted without losing information.
        Exception: Database access fails. Discard a partially populated destination.
    """
    _distinct_databases(source, destination)
    create_application(lifespan=None)
    source_schema = MetaData()
    target_schema = MetaData()
    async with destination.connect() as connection:
        if await connection.run_sync(lambda sync: inspect(sync).get_table_names()):
            raise ValueError("Destination database must be empty")
    async with source.connect() as reader:
        await reader.run_sync(source_schema.reflect)
        known = {**Base.metadata.tables, **automation_metadata.tables}
        for name, old in source_schema.tables.items():
            current = known.get(name)
            if current is not None and not set(old.c.keys()).issubset(current.c.keys()):
                raise ValueError(f"Unsupported source columns in {name}")
            (current if current is not None else old).to_metadata(target_schema)
        for name, table in known.items():
            if name not in target_schema.tables:
                table.to_metadata(target_schema)
        for name, states in {
            "conversation_run_registrations": ("preparing", "starting", "running"),
            "tinkerfin_automation_runs": ("running", "cancel_requested"),
        }.items():
            table = source_schema.tables.get(name)
            if table is not None:
                active = await reader.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.status.in_(states))
                )
                if active:
                    raise ValueError(f"Settle active work in {name} before copying")
        counts: dict[str, int] = {}
        # Validate every row before creating destination tables.
        for table in source_schema.sorted_tables:
            counts[table.name] = 0
            async with reader.stream(select(table)) as rows:
                async for row in rows.mappings():
                    _convert_row(table.name, dict(row))
                    counts[table.name] += 1
        if not apply:
            return counts
        async with destination.begin() as writer:
            await writer.run_sync(target_schema.create_all)
            for table in source_schema.sorted_tables:
                target: Table = target_schema.tables[table.name]
                copied = 0
                async with reader.stream(select(table)) as result:
                    rows = result.mappings()
                    while batch := await rows.fetchmany(200):
                        await writer.execute(
                            insert(target),
                            [_convert_row(table.name, dict(row)) for row in batch],
                        )
                        copied += len(batch)
                if copied != counts[table.name]:
                    raise ValueError(
                        "Source data changed during copying; keep the source stopped"
                    )
                actual = await writer.scalar(select(func.count()).select_from(target))
                if actual != copied:
                    raise ValueError(f"Destination count mismatch for {table.name}")
        return counts


async def _main(apply: bool) -> None:
    source = create_async_engine(os.environ["AUTOMATION_SOURCE_DATABASE_URL"])
    destination = create_async_engine(os.environ["AUTOMATION_TARGET_DATABASE_URL"])
    try:
        counts = await prepare_database(source, destination, apply=apply)
        print(json.dumps({"applied": apply, "rows": counts}, sort_keys=True))
    finally:
        try:
            await destination.dispose()
        finally:
            await source.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Copy validated data into the empty destination",
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(_main(arguments.apply))
    except SQLAlchemyError:
        raise SystemExit(
            "Database conversion failed; source is unchanged. Check database access "
            "and destination state before retrying."
        ) from None
