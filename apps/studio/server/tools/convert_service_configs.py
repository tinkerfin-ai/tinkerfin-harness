"""离线将现有生图配置转换为个人服务；默认只读检查，不供应用导入"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    SecretStr,
    TypeAdapter,
    field_validator,
)
from sqlalchemy import DateTime, MetaData, Table, insert, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from tinkerfin_studio.services.entity import ServiceConfig
from tinkerfin_studio.services.schemas import ImageConfig, SearchConfig

_BACKUP_TABLES = (
    "agent_models",
    "model_connections",
    "conversation_run_registrations",
    "attachment_collections",
    "service_configs",
)


class SourceImage(BaseModel):
    """转换入口校验数据库读取的生图记录，不打印凭证"""

    user_id: int
    model_id: str
    model_name: str
    connection_id: str
    enabled: bool
    generation_options: dict[str, JsonValue]
    base_url: str
    api_type: str
    auth_type: str
    api_key: SecretStr
    created_at: datetime
    updated_at: datetime

    @field_validator("generation_options", mode="before")
    @classmethod
    def parse_options(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value


class TableBackup(BaseModel):
    """备份文件中的表结构与原始行；时间按 ISO 8601 写入"""

    model_config = ConfigDict(extra="forbid")
    ddl: str
    rows: list[dict[str, JsonValue | datetime]]


_backup_adapter = TypeAdapter(dict[str, TableBackup | None])


def image_configuration(source: SourceImage) -> ImageConfig:
    """保留单次生成参数与导出格式，未知协议必须先人工明确"""
    if source.api_type != "openai_chat_completions" or source.auth_type != "api_key":
        raise ValueError("生图配置需先明确可转换的接口与认证方式")
    options = dict(source.generation_options)
    formats = options.pop("output_formats", [])
    size = options.pop("size", "")
    return ImageConfig.model_validate(
        {
            "provider_id": "openai",
            "endpoint": source.base_url,
            "model": source.model_name,
            "size": size,
            "output_formats": formats,
            "extra": options,
        }
    )


async def _table(connection: AsyncConnection, name: str) -> Table:
    return await connection.run_sync(
        lambda sync: Table(name, MetaData(), autoload_with=sync)
    )


async def preflight(
    engine: AsyncEngine,
    components: AsyncEngine,
    *,
    choices: dict[int, str],
    search_key: str = "",
    search_owner: int | None = None,
) -> tuple[list[SourceImage], list[str]]:
    """检查全部当前实例边界，报告歧义而不选择或丢弃配置"""
    blockers: list[str] = []
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {c["name"] for c in inspect(sync).get_columns("agent_models")}
        )
        if "purpose" not in columns:
            raise ValueError("模型表已是当前形态，无需再次转换")
        users = set((await connection.execute(text("SELECT id FROM users"))).scalars())
        active = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM conversation_run_registrations WHERE status IN ('preparing','starting','running','waiting')"
            )
        )
        waiting = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM conversation_threads WHERE has_pending_interrupt = 1 OR status = 'running'"
            )
        )
        if active or waiting:
            blockers.append("仍有未结束的会话或审批，不能推断其服务绑定")
        orphan = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM agent_models m LEFT JOIN model_connections c ON m.user_id=c.user_id AND m.connection_id=c.connection_id WHERE m.purpose='image' AND c.id IS NULL"
            )
        )
        if orphan:
            blockers.append("生图配置存在缺失连接")
        rows = (
            await connection.execute(
                text(
                    "SELECT m.user_id,m.model_id,m.model_name,m.connection_id,m.enabled,m.generation_options,m.created_at,m.updated_at,c.base_url,c.api_type,c.auth_type,c.api_key FROM agent_models m JOIN model_connections c ON m.user_id=c.user_id AND m.connection_id=c.connection_id WHERE m.purpose='image' ORDER BY m.user_id,m.id"
                )
            )
        ).mappings()
        candidates = [SourceImage.model_validate(dict(row)) for row in rows]
        has_services = await connection.run_sync(
            lambda sync: inspect(sync).has_table("service_configs")
        )
        if has_services and await connection.scalar(
            text("SELECT COUNT(*) FROM service_configs")
        ):
            blockers.append("目标服务表已有配置，请先核对，转换不会覆盖")
    async with components.connect() as connection:
        exists = await connection.run_sync(
            lambda sync: inspect(sync).has_table("tinkerfin_automation_runs")
        )
        if exists and await connection.scalar(
            text(
                "SELECT COUNT(*) FROM tinkerfin_automation_runs WHERE namespace='studio_automation' AND status NOT IN ('succeeded','failed','timed_out','cancelled')"
            )
        ):
            blockers.append("仍有排队或未结束的自动化执行")
    grouped: dict[int, list[SourceImage]] = defaultdict(list)
    for row in candidates:
        grouped[row.user_id].append(row)
    if choices.keys() - grouped.keys():
        blockers.append("生图选择包含没有生图配置的用户")
    selected: list[SourceImage] = []
    for owner, rows in grouped.items():
        choice = choices.get(owner)
        if len(rows) > 1 and choice is None:
            blockers.append(
                f"用户 {owner} 有多个生图配置，需要明确选择："
                + ", ".join(row.model_id for row in rows)
            )
            continue
        matches = [row for row in rows if choice is None or row.model_id == choice]
        if len(matches) != 1:
            blockers.append(f"用户 {owner} 的生图选择无效")
            continue
        row = matches[0]
        if owner not in users:
            blockers.append(f"生图配置 {row.model_id} 的用户不存在")
            continue
        try:
            image_configuration(row)
        except ValueError:
            blockers.append(f"用户 {owner} 的生图配置 {row.model_id} 无法无损转换")
            continue
        selected.append(row)
    if search_key and search_owner is None:
        blockers.append("全局搜索密钥需通过 --search-owner 明确接收用户")
    if search_owner is not None and (search_owner not in users or not search_key):
        blockers.append("搜索接收用户不存在或未提供全局搜索密钥")
    return selected, blockers


async def backup(engine: AsyncEngine, path: Path) -> None:
    """把受影响表的结构与数据保存为仅当前用户可读的回滚文件"""
    tables: dict[str, TableBackup | None] = {}
    async with engine.connect() as connection:
        for name in _BACKUP_TABLES:
            if not await connection.run_sync(
                lambda sync, name=name: inspect(sync).has_table(name)
            ):
                tables[name] = None
                continue
            table = await _table(connection, name)
            ddl = (await connection.execute(text(f"SHOW CREATE TABLE `{name}`"))).one()[
                1
            ]
            rows = [
                dict(row)
                for row in (await connection.execute(select(table))).mappings()
            ]
            tables[name] = TableBackup.model_validate({"ddl": ddl, "rows": rows})
    payload = _backup_adapter.dump_json(tables, indent=2)
    await asyncio.to_thread(_write_backup, path, payload)


def _write_backup(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)


async def convert(
    engine: AsyncEngine,
    images: list[SourceImage],
    *,
    search_key: str = "",
    search_owner: int | None = None,
) -> None:
    """在已停止写入并完成备份的实例上执行一次性转换"""
    service_table = ServiceConfig.metadata.tables[ServiceConfig.__tablename__]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: service_table.create(sync, checkfirst=True)
        )
        for source in images:
            config = image_configuration(source)
            await connection.execute(
                insert(service_table).values(
                    id=source.model_id,
                    user_id=source.user_id,
                    capability="image_generation",
                    provider_id=config.provider_id,
                    enabled=source.enabled,
                    config=config.model_dump(mode="json"),
                    api_key=source.api_key.get_secret_value(),
                    created_at=source.created_at,
                    updated_at=source.updated_at,
                )
            )
        if search_key and search_owner is not None:
            config = SearchConfig()
            await connection.execute(
                insert(service_table).values(
                    id=str(uuid4()),
                    user_id=search_owner,
                    capability="web_search",
                    provider_id="tavily",
                    enabled=True,
                    config=config.model_dump(mode="json"),
                    api_key=search_key,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                    updated_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        await connection.execute(
            text(
                "DELETE c FROM model_connections c WHERE EXISTS (SELECT 1 FROM agent_models m WHERE m.user_id=c.user_id AND m.connection_id=c.connection_id AND m.purpose='image') AND NOT EXISTS (SELECT 1 FROM agent_models m WHERE m.user_id=c.user_id AND m.connection_id=c.connection_id AND m.purpose='chat')"
            )
        )
        await connection.execute(text("DELETE FROM agent_models WHERE purpose='image'"))
        await connection.execute(
            text(
                "UPDATE attachment_collections SET configuration=JSON_OBJECT('task', configuration, 'services', NULL) WHERE purpose='execution'"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE conversation_run_registrations ADD COLUMN service_bindings JSON NULL COMMENT '本运行搜索与生图服务的稳定 ID 和配置摘要，不含凭证'"
            )
        )
        await connection.execute(
            text(
                "UPDATE conversation_run_registrations SET service_bindings=JSON_OBJECT('web_search', NULL, 'image_generation', NULL)"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE conversation_run_registrations MODIFY COLUMN service_bindings JSON NOT NULL COMMENT '本运行搜索与生图服务的稳定 ID 和配置摘要，不含凭证'"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE agent_models DROP INDEX ix_agent_models_default, DROP COLUMN purpose, DROP COLUMN generation_options, ADD INDEX ix_agent_models_default (user_id,is_default,enabled)"
            )
        )


async def restore(engine: AsyncEngine, path: Path) -> None:
    """显式恢复本工具的备份；调用者必须确保应用与任务执行已停止"""
    payload = _backup_adapter.validate_json(await asyncio.to_thread(path.read_bytes))
    if set(payload) != set(_BACKUP_TABLES):
        raise ValueError("备份表集合不正确")
    async with engine.begin() as connection:
        for name in _BACKUP_TABLES:
            await connection.execute(text(f"DROP TABLE IF EXISTS `{name}`"))
            saved = payload[name]
            if saved is None:
                continue
            await connection.execute(text(saved.ddl))
            table = await _table(connection, name)
            for row in saved.rows:
                for column in table.columns:
                    value = row.get(column.name)
                    if isinstance(column.type, DateTime) and isinstance(value, str):
                        row[column.name] = datetime.fromisoformat(value)
                await connection.execute(insert(table).values(**row))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--offline", action="store_true", help="确认应用及自动化工作进程已停止写入"
    )
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--search-owner", type=int)
    parser.add_argument(
        "--image-choice", action="append", default=[], metavar="USER_ID=MODEL_ID"
    )
    args = parser.parse_args()
    env = await asyncio.to_thread(dotenv_values, args.env_file, interpolate=False)
    business_url = env.get("BUSINESS_DATABASE_URL")
    components_url = env.get("COMPONENTS_DATABASE_URL")
    if not business_url or not components_url:
        raise ValueError(
            "检查文件须明确填写 BUSINESS_DATABASE_URL 与 COMPONENTS_DATABASE_URL"
        )
    engine, components = (
        create_async_engine(business_url, hide_parameters=True),
        create_async_engine(components_url, hide_parameters=True),
    )
    try:
        if engine.dialect.name != "mysql":
            raise ValueError("该一次性转换仅支持 MySQL")
        if args.restore:
            if not args.offline:
                raise ValueError("恢复前须确认应用已停止写入")
            await restore(engine, args.restore)
            print("已恢复转换备份")
            return
        choices = {
            int(item.split("=", 1)[0]): item.split("=", 1)[1]
            for item in args.image_choice
        }
        key = env.get("TAVILY_API_KEY") or ""
        images, blockers = await preflight(
            engine,
            components,
            choices=choices,
            search_key=key,
            search_owner=args.search_owner,
        )
        print(
            json.dumps(
                {
                    "selected_images": [
                        {
                            "user_id": row.user_id,
                            "model_id": row.model_id,
                            "enabled": row.enabled,
                        }
                        for row in images
                    ],
                    "search_owner": args.search_owner,
                    "blockers": blockers,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if blockers:
            raise SystemExit(2)
        if args.apply:
            if not args.offline or args.backup is None:
                raise ValueError("应用前须提供 --offline 和尚不存在的 --backup 文件")
            await backup(engine, args.backup)
            await convert(
                engine, images, search_key=key, search_owner=args.search_owner
            )
            print("转换完成；备份包含凭证，请保留为仅当前用户可读")
    finally:
        await engine.dispose()
        await components.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 - 管理入口不输出可能含凭证的数据库或校验异常正文
        print(
            f"未完成：{type(error).__name__}；请核对输入与数据库，已创建的备份可用于恢复"
        )
        raise SystemExit(1) from None
