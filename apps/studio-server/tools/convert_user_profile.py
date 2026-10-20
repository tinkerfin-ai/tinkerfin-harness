"""一次性整理用户资料表；默认只读，显式应用前保存仅当前用户可读的备份"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import anyio
from anyio import to_thread
from pydantic import BaseModel, JsonValue, TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from tinkerfin_studio.config.settings import load_settings
from tinkerfin_studio.infrastructure.threads import run_owned_thread

_RETAINED = {"id", "username", "avatar_url", "password_hash", "roles", "disabled"}
_json_rows = TypeAdapter(list[dict[str, JsonValue]])


class UserProfileBackup(BaseModel):
    """用户表结构、索引和完整资料的离线备份，包含密码哈希等敏感数据"""

    database: str
    ddl: str
    rows: list[dict[str, JsonValue]]
    indexes: list[dict[str, JsonValue]]


async def _columns(connection: AsyncConnection) -> dict[str, str]:
    rows = await connection.execute(
        text(
            "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='users'"
        )
    )
    return TypeAdapter(dict[str, str]).validate_python(dict(rows.tuples().all()))


async def _indexes(connection: AsyncConnection) -> list[dict[str, JsonValue]]:
    rows = await connection.execute(
        text(
            "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME='users' ORDER BY INDEX_NAME, SEQ_IN_INDEX"
        )
    )
    result = _json_rows.validate_python([dict(row) for row in rows.mappings()])
    if any(row["COLUMN_NAME"] in {"display_name", "avatar_content"} for row in result):
        raise ValueError("待删除字段参与索引，不能直接删除")
    return result


async def _snapshot(
    connection: AsyncConnection, database: str
) -> tuple[dict[str, str], UserProfileBackup]:
    rows = await connection.execute(text("SELECT * FROM users ORDER BY id"))
    columns = await _columns(connection)
    if not _RETAINED <= columns.keys() or columns.keys() - _RETAINED - {
        "display_name",
        "avatar_content",
    }:
        raise ValueError("用户表字段集合不符合预期")
    if "display_name" in columns and columns["display_name"] != "varchar(128)":
        raise ValueError("昵称字段类型不符合预期")
    if "avatar_content" in columns and columns["avatar_content"] != "mediumblob":
        raise ValueError("头像内容字段类型不符合预期")
    records = [dict(row) for row in rows.mappings()]
    if any(row.get("avatar_content") is not None for row in records):
        raise ValueError("头像二进制列仍有数据，必须先保存到对象存储，不能直接删除")
    ddl_row = (await connection.execute(text("SHOW CREATE TABLE users"))).one()
    return columns, UserProfileBackup(
        database=database,
        ddl=TypeAdapter(str).validate_python(ddl_row[1]),
        rows=_json_rows.validate_python(records),
        indexes=await _indexes(connection),
    )


def _retained_rows(saved: UserProfileBackup) -> list[dict[str, JsonValue]]:
    return [{key: row[key] for key in _RETAINED} for row in saved.rows]


def _write_backup(path: Path, payload: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


async def convert_user_profile(
    engine: AsyncEngine,
    *,
    expected_database: str,
    apply: bool = False,
    backup: Path | None = None,
) -> dict[str, JsonValue]:
    """先完整备份指定业务库的用户表，再锁表核对备份并整理字段

    备份写入前释放读事务和连接，文件落盘期间不占用用户表锁。同步文件写入
    由本次调用等待完成后传播取消；备份期间数据或结构变化会终止整理。

    Args:
        engine: 调用方拥有并负责关闭的 MySQL 连接池
        expected_database: 必须与当前连接实际数据库一致
        apply: 是否执行一次性整理；默认仅检查
        backup: 应用时必需且不能覆盖的备份文件

    Returns:
        不含用户名、昵称、密码哈希或连接凭证的执行摘要

    Raises:
        ValueError: 目标、结构或备份参数不符合要求
        RuntimeError: 备份与锁定后的用户表不一致，或操作后其他资料、索引发生变化
    """

    if engine.dialect.name != "mysql":
        raise ValueError("仅支持 MySQL 业务库")
    if apply and backup is None:
        raise ValueError("应用时必须提供备份路径")
    async with engine.connect() as connection:
        actual = await connection.scalar(text("SELECT DATABASE()"))
        if not expected_database or actual != expected_database:
            raise ValueError("当前连接不是指定业务库")
        columns, saved = await _snapshot(connection, expected_database)
    changes = [
        f"DROP COLUMN {column}"
        for column in ("display_name", "avatar_content")
        if column in columns
    ]
    report: dict[str, JsonValue] = {
        "database": expected_database,
        "users": len(saved.rows),
        "changes": len(changes),
        "applied": False,
    }
    if not apply or not changes:
        return report
    assert backup is not None
    await run_owned_thread(
        _write_backup,
        backup,
        saved.model_dump_json(indent=2),
        limiter=anyio.CapacityLimiter(1),
    )
    async with engine.connect() as connection:
        if await connection.scalar(text("SELECT DATABASE()")) != expected_database:
            raise ValueError("当前连接不是指定业务库")
        await connection.execute(text("SET SESSION lock_wait_timeout=10"))
        locked = False
        try:
            await connection.execute(text("LOCK TABLES users WRITE"))
            locked = True
            _, current = await _snapshot(connection, expected_database)
            if current != saved:
                raise RuntimeError("备份期间用户资料、表结构或索引发生变化，未执行整理")
            changes.append(
                "MODIFY COLUMN avatar_url VARCHAR(2048) NULL COMMENT '头像的长期对象存储地址，空值由前端显示默认头像'"
            )
            await connection.execute(text("ALTER TABLE users " + ", ".join(changes)))
            columns, converted = await _snapshot(connection, expected_database)
            if (
                _retained_rows(converted) != _retained_rows(saved)
                or converted.indexes != saved.indexes
            ):
                raise RuntimeError("其他用户资料或索引发生变化，请保留备份核验")
            if columns.keys() != _RETAINED:
                raise RuntimeError("整理后的用户表字段不符合预期")
            report["applied"] = True
            return report
        finally:
            # 驱动取消 SQL 时会断开并失效连接；不能在该连接上再发送解锁 SQL
            if locked and not connection.invalidated:
                with anyio.CancelScope(shield=True):
                    try:
                        await connection.execute(text("UNLOCK TABLES"))
                    except BaseException:
                        await connection.invalidate()
                        raise


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if not args.env_file.is_file():
        raise ValueError("配置文件不存在")
    settings = await to_thread.run_sync(lambda: load_settings(env_file=args.env_file))
    engine = create_async_engine(
        settings.business_database.url,
        hide_parameters=True,
        connect_args={"connect_timeout": 10},
    )
    try:
        report = await convert_user_profile(
            engine,
            expected_database=args.database,
            apply=args.apply,
            backup=args.backup,
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 - 管理入口不输出可能含凭证或用户资料的异常正文
        print(f"未完成：{type(error).__name__}；请核对目标与备份后重试")
        raise SystemExit(1) from None
