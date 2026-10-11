"""离线补齐自动化会话入口；默认检查，不重跑任务或改写 Trace、checkpoint"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, JsonValue, TypeAdapter
from sqlalchemy import DateTime, MetaData, Table, insert, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_automation import (
    Automation,
    AutomationExecution,
    SqlAlchemyAutomationStore,
)
from tinkerfin_studio.attachments.entity import AttachmentCollection
from tinkerfin_studio.automation.ownership import parse_automation_owner
from tinkerfin_studio.automation.service import NAMESPACE, task_configuration
from tinkerfin_studio.config.settings import load_settings
from tinkerfin_studio.conversation.models import ConversationThread
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.projects.models import Project
from tinkerfin_studio.services.schemas import ServiceBindings
from tinkerfin_tracing import (
    SqlAlchemyTraceStore,
    Tracer,
    TraceRunNotFound,
    TraceThreadNotFound,
)

_TABLES = ("conversation_threads", "conversation_run_registrations")


class TableBackup(BaseModel):
    """保留本次可能改变的表结构及原始业务行"""

    ddl: str
    rows: list[dict[str, JsonValue | datetime]]


@dataclass(slots=True)
class BackfillReport:
    """只报告身份和缺失事实，不输出提示词、模型配置或凭据"""

    created: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)


async def preflight(database: Database, components: Database) -> bool:
    """拒绝活动实例，返回是否需要补字段；检查本身不修改结构或业务数据"""
    async with database.engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                item["name"] for item in inspect(sync).get_columns(_TABLES[1])
            }
        )
        active = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM conversation_run_registrations "
                "WHERE status IN ('preparing','starting','running')"
            )
        )
        if active:
            raise ValueError("仍有活动会话，请停止写入并完成结算")
    async with components.engine.connect() as connection:
        active = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM tinkerfin_automation_runs "
                "WHERE namespace='studio_automation' AND status IN "
                "('queued','running','interrupted','cancel_requested')"
            )
        )
        if active:
            raise ValueError("仍有活动自动化，请停止写入并完成结算")
    return "automation_execution_id" not in columns


async def backup(engine: AsyncEngine, path: Path) -> None:
    """仅备份本次修改的两张表，文件独占创建且只允许当前用户读取"""
    tables: dict[str, TableBackup] = {}
    async with engine.connect() as connection:
        for name in _TABLES:
            table = await connection.run_sync(
                lambda sync: Table(name, MetaData(), autoload_with=sync)
            )
            ddl = (await connection.execute(text(f"SHOW CREATE TABLE {name}"))).one()[1]
            rows = [
                dict(row)
                for row in (await connection.execute(select(table))).mappings()
            ]
            tables[name] = TableBackup.model_validate({"ddl": ddl, "rows": rows})
    payload = TypeAdapter(dict[str, TableBackup]).dump_json(tables, indent=2)

    def write() -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    # CLI 只创建一个备份写任务并等待完成，不与任何数据库锁共存
    await asyncio.to_thread(write)


async def add_execution_link(engine: AsyncEngine) -> None:
    """已备份且停止写入后，一次性收敛到当前业务表结构"""
    async with engine.begin() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                item["name"] for item in inspect(sync).get_columns(_TABLES[1])
            }
        )
        if "automation_execution_id" not in columns:
            await connection.execute(
                text(
                    "ALTER TABLE conversation_run_registrations "
                    "ADD COLUMN automation_execution_id VARCHAR(36) NULL "
                    "COMMENT '自动化执行 ID；普通会话运行为 NULL', "
                    "ADD CONSTRAINT uq_conversation_run_registrations_automation "
                    "UNIQUE (automation_execution_id)"
                )
            )


async def backfill(
    database: Database, automation: Automation, tracer: Tracer, *, apply: bool
) -> BackfillReport:
    """只从已保存执行、服务快照与精确 Trace 结果重建登记，缺失事实单独报告

    检查不修改业务行；Trace 公共查询可能更新可重建的查询缓存。
    既有会话包括已移除入口均不重建。checkpoint 保持原样，续聊能力须另行实测。
    """
    report = BackfillReport()
    async with database.session() as session:
        projects = list(await session.scalars(select(Project)))
    for project in projects:
        owner = automation.for_owner(f"{project.user_id}:{project.id}")
        cursor = None
        while True:
            page = await owner.list_runs(cursor=cursor, limit=100)
            for handle in page.items:
                await _backfill_run(
                    database, tracer, handle.snapshot, apply=apply, report=report
                )
            cursor = page.next_cursor
            if cursor is None:
                break
    return report


async def _backfill_run(
    database: Database,
    tracer: Tracer,
    execution: AutomationExecution,
    *,
    apply: bool,
    report: BackfillReport,
) -> None:
    execution_id = execution.execution_id
    user_id, project_id = parse_automation_owner(execution.owner_id)
    async with database.session() as session:
        existing = await session.scalar(
            select(ConversationThread).where(
                ConversationThread.thread_id == execution.identity.thread_id
            )
        )
        if existing is not None:
            if existing.user_id != user_id:
                raise ValueError("执行与已有会话归属冲突")
            report.present.append(execution_id)
            return
        collection = await session.get(AttachmentCollection, execution_id)
        if execution.execution_started_at is None:
            report.unavailable[execution_id] = "执行未启动"
            return
        if (
            collection is None
            or collection.user_id != user_id
            or collection.project_id != project_id
        ):
            report.unavailable[execution_id] = "缺少执行输入集合"
            return
        stored = collection.configuration
        config = task_configuration(execution)
        if (
            stored.get("task") != config.model_dump(mode="json", by_alias=True)
            or stored.get("services") is None
        ):
            report.unavailable[execution_id] = "缺少可核对的服务快照"
            return
        if (
            config.project_id != project_id
            or execution.identity.namespace != f"ns_{user_id}"
        ):
            raise ValueError("执行输入与运行身份归属冲突")
        services = ServiceBindings.model_validate(stored["services"])
    try:
        trace = await tracer.get_run(execution.identity)
    except (TraceThreadNotFound, TraceRunNotFound):
        report.unavailable[execution_id] = "未保留运行轨迹"
        return
    if trace.summary.status.execution not in {
        "succeeded",
        "failed",
        "cancelled",
        "abandoned",
    }:
        report.unavailable[execution_id] = "运行轨迹尚无可直接登记的终态"
        return
    messages: list[JsonValue] = [
        {"id": item.source_id, "role": "user", "content": item.content}
        for item in trace.messages
        if item.role == "user"
        and not item.graph_namespace
        and item.source_id is not None
        and not item.content_omitted
    ]
    if not messages:
        report.unavailable[execution_id] = "未保留完整输入消息身份"
        return
    if apply:
        stamp = execution.queued_at.astimezone(ZoneInfo("Asia/Shanghai"))
        async with database.session() as session:
            repository = ConversationRepository(session)
            thread = await repository.create_thread(
                user_id=user_id,
                project_id=project_id,
                thread_id=execution.identity.thread_id,
                title=f"{config.name[:18]} · {stamp:%m-%d %H:%M}",
                model_id=config.model_id,
            )
            thread.title_source = "generated"
            thread.title_generation_status = "succeeded"
            thread.title_seq = 1
            thread.last_run_id = execution.identity.run_id
            thread.last_access_mode = config.access_mode
            thread.created_at = execution.queued_at.astimezone(UTC).replace(tzinfo=None)
            thread.updated_at = trace.summary.last_occurred_at.astimezone(UTC).replace(
                tzinfo=None
            )
            registration = await repository.create_run_registration(
                thread_id=thread.id,
                run_id=execution.identity.run_id,
                parent_run_id=None,
                model_id=config.model_id,
                access_mode=config.access_mode,
                automation_execution_id=execution_id,
                service_bindings=services,
                input_json={
                    "threadId": execution.identity.thread_id,
                    "runId": execution.identity.run_id,
                    "messages": messages,
                    "state": {},
                    "tools": [],
                    "context": [],
                    "forwardedProps": {
                        "projectId": project_id,
                        "model": config.model_id,
                        "accessMode": config.access_mode,
                        "command": {"plan": "off"},
                    },
                },
            )
            registration.status = "starting"
            registration.started_at = execution.execution_started_at.astimezone(
                UTC
            ).replace(tzinfo=None)
            registration.created_at = registration.started_at
            await session.flush()
            await repository.update_trace_summary(
                thread_pk=thread.id,
                run_id=execution.identity.run_id,
                status="error"
                if trace.summary.status.execution == "failed"
                else "idle",
                message_count=trace.summary.message_count,
                tool_call_count=trace.summary.tool_call_count,
                has_pending_interrupt=False,
                pending_interaction_kind=None,
                terminal_outcome=trace.summary.status.execution,
                updated_at=trace.summary.last_occurred_at.astimezone(UTC).replace(
                    tzinfo=None
                ),
                trace_generation=trace.generation,
                trace_as_of_seq=trace.as_of_seq,
                trace_observed_at=trace.observed_at.astimezone(UTC).replace(
                    tzinfo=None
                ),
            )
            await session.commit()
    report.created.append(execution_id)


async def restore(engine: AsyncEngine, path: Path) -> None:
    """仅恢复本工具的两张表；须停止写入并同步回退应用代码"""
    payload = TypeAdapter(dict[str, TableBackup]).validate_json(
        await asyncio.to_thread(path.read_bytes)
    )
    if set(payload) != set(_TABLES):
        raise ValueError("备份表集合不正确")
    async with engine.begin() as connection:
        for name in _TABLES:
            await connection.execute(text(f"DROP TABLE `{name}`"))
            await connection.execute(text(payload[name].ddl))
            table = await connection.run_sync(
                lambda sync: Table(name, MetaData(), autoload_with=sync)
            )
            for saved in payload[name].rows:
                row = dict(saved)
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
        "--offline", action="store_true", help="确认应用和所有工作进程已停止写入"
    )
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--restore", type=Path)
    args = parser.parse_args()
    settings = await asyncio.to_thread(load_settings, env_file=args.env_file)
    async with (
        Database(settings.business_database.url) as database,
        Database(settings.components_database.url) as components,
    ):
        missing = await preflight(database, components)
        if args.restore:
            if (
                not args.offline
                or args.apply
                or database.engine.dialect.name != "mysql"
            ):
                raise ValueError("恢复需要停止写入的 MySQL 实例，且不能同时应用")
            await restore(database.engine, args.restore)
            print("已恢复会话业务表；启动前须同步回退代码")
            return
        if args.apply:
            if (
                not args.offline
                or args.backup is None
                or database.engine.dialect.name != "mysql"
            ):
                raise ValueError("应用需要离线 MySQL 实例和独占备份路径")
            await backup(database.engine, args.backup)
            await add_execution_link(database.engine)
        store = SqlAlchemyAutomationStore(components.engine)
        try:
            async with Automation(namespace=NAMESPACE, store=store) as automation:
                report = await backfill(
                    database,
                    automation,
                    Tracer(store=SqlAlchemyTraceStore(components.engine)),
                    apply=args.apply,
                )
        finally:
            await store.close()
        print(
            json.dumps(
                {"schema_change": missing, "applied": args.apply, **asdict(report)},
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 - 运维输出不得包含 DSN、凭证或用户消息
        print(f"未完成：{type(error).__name__}；请核对活动执行、结构和备份")
        raise SystemExit(1) from None
