"""离线收敛本机资源请求与默认值；应用前须停止新请求并结束运行和待审批任务"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path

import anyio
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from tinkerfin_studio.config.settings import load_settings
from tinkerfin_studio.infrastructure.threads import run_owned_thread

_TABLES = (
    "conversation_threads",
    "conversation_run_registrations",
    "device_execution_profiles",
)
_REMOVED_PATHS = "'$.forwardedProps.localTarget', '$.forwardedProps.local_target'"
_AFFECTED = f"JSON_CONTAINS_PATH(input_json, 'one', {_REMOVED_PATHS})"
_COLUMNS = {
    ("conversation_threads", "last_access_mode"): (
        "varchar(32)",
        "NO",
        "write_approval",
        "最近主运行本机确认模式：full、write_approval",
        "VARCHAR(32) NOT NULL DEFAULT 'write_approval' COMMENT '最近主运行本机确认模式：full、write_approval'",
    ),
    ("conversation_run_registrations", "access_mode"): (
        "varchar(32)",
        "NO",
        "write_approval",
        "本机确认模式：full、write_approval",
        "VARCHAR(32) NOT NULL DEFAULT 'write_approval' COMMENT '本机确认模式：full、write_approval'",
    ),
    ("conversation_run_registrations", "local_profile_id"): (
        "varchar(36)",
        "YES",
        None,
        "本轮固定的本机执行配置 UUID；首次调用本机工具前为空",
        "VARCHAR(36) NULL COMMENT '本轮固定的本机执行配置 UUID；首次调用本机工具前为空'",
    ),
    ("device_execution_profiles", "access_mode"): (
        "varchar(32)",
        "NO",
        None,
        "本机确认选择：full、write_approval",
        "VARCHAR(32) NOT NULL COMMENT '本机确认选择：full、write_approval'",
    ),
}


class _Column(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    table_name: str
    column_name: str
    column_type: str
    is_nullable: str
    column_default: str | None
    column_comment: str


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: int
    input_json: dict[str, JsonValue]

    @field_validator("input_json", mode="before")
    @classmethod
    def decode_json(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value


class ComputerResourcesBackup(BaseModel):
    """受影响请求与原表结构的离线备份；请求中可能包含用户正文及附件引用"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    database: str
    ddl: dict[str, str]
    columns: tuple[_Column, ...]
    requests: tuple[_Request, ...]


_columns: TypeAdapter[tuple[_Column, ...]] = TypeAdapter(tuple[_Column, ...])
_requests: TypeAdapter[tuple[_Request, ...]] = TypeAdapter(tuple[_Request, ...])


async def _snapshot(
    connection: AsyncConnection, database: str
) -> ComputerResourcesBackup:
    if not database or await connection.scalar(text("SELECT DATABASE()")) != database:
        raise ValueError("当前连接不是指定业务库")
    # 审批来源运行保留 waiting 作为历史；只有当前 head 或失去归属时仍须阻断转换
    active = await connection.scalar(
        text(
            "SELECT COUNT(*) FROM conversation_run_registrations "
            "LEFT JOIN conversation_threads ON conversation_threads.id="
            "conversation_run_registrations.conversation_thread_id "
            "WHERE conversation_run_registrations.status IN ('preparing','starting','running') "
            "OR (conversation_run_registrations.status='waiting' AND "
            "(conversation_threads.id IS NULL OR conversation_threads.last_run_id IS NULL "
            "OR conversation_threads.last_run_id=conversation_run_registrations.run_id))"
        )
    )
    pending = await connection.scalar(
        text(
            "SELECT COUNT(*) FROM conversation_threads "
            "WHERE has_pending_interrupt=1 OR status IN ('running','waiting_approval')"
        )
    )
    if active or pending:
        raise ValueError(
            "须先停止新请求，并通过当前应用结束运行与待审批任务；不能直接清除审批记录"
        )
    size = (
        await connection.execute(
            text(
                "SELECT COUNT(*), COALESCE(SUM(OCTET_LENGTH(input_json)),0) "
                f"FROM conversation_run_registrations WHERE {_AFFECTED}"
            )
        )
    ).one()
    if int(size[0]) > 10_000 or int(size[1]) > 64 * 1024 * 1024:
        raise ValueError(
            "受影响请求超过本工具单次备份上限（10000 条、64 MiB），未执行转换"
        )
    columns = await connection.execute(
        text(
            "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
            "COLUMN_TYPE AS column_type, IS_NULLABLE AS is_nullable, "
            "COLUMN_DEFAULT AS column_default, COLUMN_COMMENT AS column_comment "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND "
            "((TABLE_NAME='conversation_threads' AND COLUMN_NAME='last_access_mode') OR "
            "(TABLE_NAME='conversation_run_registrations' AND COLUMN_NAME IN ('access_mode','local_profile_id')) OR "
            "(TABLE_NAME='device_execution_profiles' AND COLUMN_NAME='access_mode')) "
            "ORDER BY TABLE_NAME, COLUMN_NAME"
        )
    )
    parsed = _columns.validate_python([dict(row) for row in columns.mappings()])
    if {(column.table_name, column.column_name) for column in parsed} != set(_COLUMNS):
        raise ValueError("业务表缺少预期字段")
    for column in parsed:
        expected = _COLUMNS[column.table_name, column.column_name]
        if (column.column_type, column.is_nullable) != expected[:2]:
            raise ValueError("待整理字段的类型或可空性不符合预期")
    ddl: dict[str, str] = {}
    for table in _TABLES:
        result = (await connection.execute(text(f"SHOW CREATE TABLE {table}"))).one()
        ddl[table] = TypeAdapter(str).validate_python(result[1])
    rows = await connection.execute(
        text(
            f"SELECT id, input_json FROM conversation_run_registrations WHERE {_AFFECTED} ORDER BY id"
        )
    )
    return ComputerResourcesBackup(
        database=database,
        ddl=ddl,
        columns=parsed,
        requests=_requests.validate_python([dict(row) for row in rows.mappings()]),
    )


def _changes(snapshot: ComputerResourcesBackup) -> dict[str, list[str]]:
    changes: dict[str, list[str]] = defaultdict(list)
    for column in snapshot.columns:
        expected = _COLUMNS[column.table_name, column.column_name]
        if (column.column_default, column.column_comment) != expected[2:4]:
            changes[column.table_name].append(
                f"MODIFY COLUMN {column.column_name} {expected[4]}"
            )
    return dict(changes)


def _write_backup(path: Path, payload: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


async def convert_computer_resources(
    engine: AsyncEngine,
    *,
    expected_database: str,
    apply: bool = False,
    backup: Path | None = None,
    no_backup: bool = False,
) -> dict[str, JsonValue]:
    """核对离线业务库并按选择备份，只移除请求目标字段与更新列默认值、说明

    已保存的确认选择、设备授权、会话内容和框架回执不改。读事务在备份写盘前
    释放；同步写盘由容量为 1 的受控线程完成，取消等待写盘结束后传播。
    表锁内不进行文件或网络服务调用，数据库等待受会话超时限制。
    MySQL DDL 隐式提交，失败时可能已完成部分转换，须核对后再继续；
    未备份时不能恢复已移除的请求字段。

    Args:
        engine: 调用方拥有的 MySQL 异步连接池，需配置驱动连接和读取超时
        expected_database: 必须与实际连接一致的业务库名称
        apply: 是否写入；默认只读检查
        backup: 应用前保存的备份路径，必须尚不存在
        no_backup: 操作者明确放弃备份；不能与 backup 同时使用

    Returns:
        不含用户正文或连接凭证的请求数、列变化数及应用结果

    Raises:
        ValueError: 目标或结构不符、存在活动任务或备份参数不完整
        RuntimeError: 预检查后发生变化，或转换后的契约未收敛
    """
    if engine.dialect.name != "mysql":
        raise ValueError("仅支持 MySQL 业务库")
    if no_backup and backup is not None:
        raise ValueError("不能同时指定备份路径与放弃备份")
    if apply and backup is None and not no_backup:
        raise ValueError("应用时必须指定备份路径或明确选择不备份")
    async with engine.connect() as connection:
        saved = await _snapshot(connection, expected_database)
    changes = _changes(saved)
    report: dict[str, JsonValue] = {
        "database": expected_database,
        "requests": len(saved.requests),
        "columns": sum(map(len, changes.values())),
        "applied": False,
    }
    if not apply or not (changes or saved.requests):
        return report
    if backup is not None:
        await run_owned_thread(
            _write_backup,
            backup,
            saved.model_dump_json(indent=2),
            limiter=anyio.CapacityLimiter(1),
        )
    async with engine.connect() as connection:
        await connection.execute(text("SET SESSION lock_wait_timeout=10"))
        await connection.execute(text("SET SESSION innodb_lock_wait_timeout=10"))
        locked = False
        try:
            await connection.execute(
                text("LOCK TABLES " + ", ".join(f"{table} WRITE" for table in _TABLES))
            )
            locked = True
            if await _snapshot(connection, expected_database) != saved:
                raise RuntimeError("预检查后业务数据或结构发生变化，未执行转换")
            if saved.requests:
                await connection.execute(
                    text(
                        f"UPDATE conversation_run_registrations SET input_json=JSON_REMOVE(input_json, {_REMOVED_PATHS}) WHERE {_AFFECTED}"
                    )
                )
                await connection.commit()
            for table, clauses in changes.items():
                await connection.execute(
                    text(f"ALTER TABLE {table} " + ", ".join(clauses))
                )
            current = await _snapshot(connection, expected_database)
            if current.requests or _changes(current):
                raise RuntimeError("转换后的请求或列定义不符合当前契约")
            report["applied"] = True
            return report
        finally:
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
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--backup", type=Path)
    recovery.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    if not args.env_file.is_file():
        raise ValueError("配置文件不存在")
    settings = await run_owned_thread(
        lambda: load_settings(env_file=args.env_file), limiter=anyio.CapacityLimiter(1)
    )
    engine = create_async_engine(
        settings.business_database.url,
        hide_parameters=True,
        connect_args={"connect_timeout": 10, "read_timeout": 30},
    )
    try:
        report = await convert_computer_resources(
            engine,
            expected_database=args.database,
            apply=args.apply,
            backup=args.backup,
            no_backup=args.no_backup,
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 - 管理入口只报告类别，避免输出用户请求或连接凭证
        print(f"未完成：{type(error).__name__}；请核对目标、任务状态与已执行的转换")
        raise SystemExit(1) from None
