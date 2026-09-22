from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)

from tinkerfin_automation import SqlAlchemyAutomationStore
from tinkerfin_automation.sql_schema import AUTOMATION_TABLE_NAMES
from tinkerfin_langgraph_store import SqlAlchemyStore
from tinkerfin_sandbox import get_sqlalchemy_opensandbox_state_schema
from tinkerfin_studio.attachments.entity import (
    AttachmentCollection,
    AttachmentFile,
    AttachmentReference,
)
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.passwords import verify_password
from tinkerfin_studio.conversation.models import (
    ConversationInterruptClaim,
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.infrastructure.database import Base
from tinkerfin_studio.models.entity import AgentModel, ModelConnection
from tinkerfin_tracing import SqlAlchemyTraceStore

_SCHEMA_PATH = Path(__file__).parents[1] / "database" / "mysql" / "schema.sql"
_DATABASE_NAME_PATTERN = re.compile(r"\Atinkerfin_schema_[a-f0-9]{16}_(sql|runtime)\Z")
_EXPECTED_TABLES = frozenset(
    {
        "users",
        "attachment_collections",
        "attachment_references",
        *AUTOMATION_TABLE_NAMES,
        "conversation_attachments",
        "agent_models",
        "model_connections",
        "conversation_threads",
        "conversation_run_registrations",
        "conversation_interrupt_claims",
        "tinkerfin_store_namespaces",
        "tinkerfin_store_paths",
        "tinkerfin_store_documents",
        "tinkerfin_store_fields",
        "tinkerfin_opensandbox_owners",
        "tinkerfin_opensandbox_workers",
        "tinkerfin_opensandbox_warm_slots",
        "tinkerfin_opensandbox_cleanup",
        "tinkerfin_opensandbox_availability",
        "tinkerfin_opensandbox_holders",
        "tinkerfin_trace_events",
        "tinkerfin_trace_graph_nodes",
        "tinkerfin_trace_namespaces",
        "tinkerfin_trace_projection_checkpoints",
        "tinkerfin_trace_threads",
        "tinkerfin_trace_writers",
    }
)
_BUSINESS_MODELS = (
    User,
    AttachmentFile,
    AttachmentCollection,
    AttachmentReference,
    AgentModel,
    ModelConnection,
    ConversationThread,
    ConversationRunRegistration,
    ConversationInterruptClaim,
)
_BUSINESS_TABLES = frozenset(model.__tablename__ for model in _BUSINESS_MODELS)


def test_studio_schema_contains_only_business_tables() -> None:
    """Studio SQL 不得复制由框架维护的表结构"""

    ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
    declared_tables = frozenset(
        re.findall(r"^CREATE TABLE ([^ (]+)", ddl, flags=re.MULTILINE)
    )

    assert declared_tables == _BUSINESS_TABLES


async def test_business_sql_initializes_the_documented_login(
    session: AsyncSession,
) -> None:
    """初始化 SQL 提供可登录的默认账号，不保存明文密码"""

    statements = _SCHEMA_PATH.read_text(encoding="utf-8").split(";")
    seeds = [
        statement
        for statement in statements
        if statement.strip().startswith("INSERT INTO users")
    ]
    assert len(seeds) == 1
    await session.execute(text(seeds[0]))
    user = (await session.scalars(select(User))).one()
    assert user.username == "tinkerfin"
    assert user.display_name == "TinkerFin"
    assert user.roles == []
    assert user.disabled is False
    assert user.password_hash != "123456"
    assert await verify_password("123456", user.password_hash)
    assert not await verify_password("wrong-password", user.password_hash)


class _ReflectedColumn(TypedDict):
    name: str
    type: object
    nullable: bool
    default: object | None
    autoincrement: NotRequired[bool]
    comment: NotRequired[str | None]


@dataclass(frozen=True, slots=True)
class _ColumnSignature:
    type_name: str
    length: int | None
    precision: int | None
    scale: int | None
    unsigned: bool
    nullable: bool
    default: str | None
    autoincrement: bool
    character_set: str | None
    collation: str | None


@dataclass(frozen=True, slots=True)
class _TableSignature:
    engine: str
    collation: str
    columns: dict[str, _ColumnSignature]
    primary_key: tuple[str, ...]
    indexes: dict[str, tuple[tuple[str, ...], bool]]
    unique_constraints: dict[str, tuple[str, ...]]
    check_constraints: dict[str, str]
    foreign_keys: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class _SchemaReflection:
    tables: dict[str, _TableSignature]
    table_comments: dict[str, str]
    column_comments: dict[str, dict[str, str]]


def _database_url(admin_url: URL, database_name: str) -> URL:
    if _DATABASE_NAME_PATTERN.fullmatch(database_name) is None:
        raise ValueError("临时数据库名称不符合安全约束")
    return admin_url.set(database=database_name, drivername="mysql+asyncmy")


def _ddl_statements(ddl: str) -> tuple[str, ...]:
    return tuple(statement.strip() for statement in ddl.split(";") if statement.strip())


def _normalize_default(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while len(normalized) >= 2 and normalized[0] == "(" and normalized[-1] == ")":
        normalized = normalized[1:-1].strip()
    if (
        len(normalized) >= 2
        and normalized[0] in {"'", '"'}
        and normalized[-1] == normalized[0]
    ):
        normalized = normalized[1:-1]
    return normalized.casefold()


def _column_signature(
    column: _ReflectedColumn,
    character_set: str | None,
    collation: str | None,
) -> _ColumnSignature:
    column_type = column["type"]
    return _ColumnSignature(
        type_name=type(column_type).__name__.casefold(),
        length=cast(int | None, getattr(column_type, "length", None)),
        precision=cast(int | None, getattr(column_type, "precision", None)),
        scale=cast(int | None, getattr(column_type, "scale", None)),
        unsigned=bool(getattr(column_type, "unsigned", False)),
        nullable=bool(column["nullable"]),
        default=_normalize_default(column.get("default")),
        autoincrement=column.get("autoincrement") is True,
        character_set=character_set,
        collation=collation,
    )


def _normalize_check_sql(value: object) -> str:
    return re.sub(r"[\s`()]+", "", str(value)).casefold()


def _reflect_schema(connection: Connection) -> _SchemaReflection:
    inspector = inspect(connection)
    table_names = sorted(inspector.get_table_names())
    tables: dict[str, _TableSignature] = {}
    table_comments: dict[str, str] = {}
    column_comments: dict[str, dict[str, str]] = {}
    table_options = {
        str(row[0]): (str(row[1]), str(row[2]))
        for row in connection.execute(
            text(
                "SELECT TABLE_NAME, ENGINE, TABLE_COLLATION "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            )
        )
    }
    column_options = {
        (str(row[0]), str(row[1])): (
            cast(str | None, row[2]),
            cast(str | None, row[3]),
        )
        for row in connection.execute(
            text(
                "SELECT TABLE_NAME, COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
            )
        )
    }
    for table_name in table_names:
        columns = cast(list[_ReflectedColumn], inspector.get_columns(table_name))
        tables[table_name] = _TableSignature(
            engine=table_options[table_name][0],
            collation=table_options[table_name][1],
            columns={
                str(column["name"]): _column_signature(
                    column, *column_options[table_name, column["name"]]
                )
                for column in columns
            },
            primary_key=tuple(
                cast(
                    list[str],
                    inspector.get_pk_constraint(table_name).get(
                        "constrained_columns", []
                    ),
                )
            ),
            indexes={
                str(index["name"]): (
                    tuple(cast(list[str], index.get("column_names", []))),
                    bool(index.get("unique", False)),
                )
                for index in inspector.get_indexes(table_name)
                if index.get("name") is not None
            },
            unique_constraints={
                str(constraint["name"]): tuple(
                    cast(list[str], constraint.get("column_names", []))
                )
                for constraint in inspector.get_unique_constraints(table_name)
                if constraint.get("name") is not None
            },
            check_constraints={
                str(constraint["name"]): _normalize_check_sql(
                    constraint.get("sqltext", "")
                )
                for constraint in inspector.get_check_constraints(table_name)
                if constraint.get("name") is not None
            },
            foreign_keys=tuple(
                tuple(
                    cast(
                        list[str],
                        foreign_key.get("constrained_columns", []),
                    )
                )
                for foreign_key in inspector.get_foreign_keys(table_name)
            ),
        )
        table_comments[table_name] = str(
            inspector.get_table_comment(table_name).get("text") or ""
        )
        column_comments[table_name] = {
            str(column["name"]): str(column.get("comment") or "") for column in columns
        }
    return _SchemaReflection(
        tables=tables,
        table_comments=table_comments,
        column_comments=column_comments,
    )


async def _execute_ddl(engine: AsyncEngine, ddl: str) -> None:
    async with engine.begin() as connection:
        for statement in _ddl_statements(ddl):
            await connection.exec_driver_sql(statement)


async def _create_runtime_schema(engine: AsyncEngine) -> None:
    assert {model.__tablename__ for model in _BUSINESS_MODELS} == set(
        Base.metadata.tables
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with SqlAlchemyStore(engine):
        pass
    sandbox_schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
    await _execute_ddl(engine, sandbox_schema.ddl)
    await SqlAlchemyTraceStore(engine).setup()
    automation = SqlAlchemyAutomationStore(engine)
    try:
        await automation.setup()
    finally:
        await automation.close()


async def _create_database(admin_engine: AsyncEngine, database_name: str) -> None:
    if _DATABASE_NAME_PATTERN.fullmatch(database_name) is None:
        raise ValueError("临时数据库名称不符合安全约束")
    async with admin_engine.begin() as connection:
        await connection.exec_driver_sql(
            f"CREATE DATABASE `{database_name}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
        )


async def _drop_database(admin_engine: AsyncEngine, database_name: str) -> None:
    if _DATABASE_NAME_PATTERN.fullmatch(database_name) is None:
        raise ValueError("临时数据库名称不符合安全约束")
    async with admin_engine.begin() as connection:
        await connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database_name}`")


@pytest.mark.studio_mysql_integration
async def test_business_sql_and_framework_setups_compose_the_current_mysql_schema(
    mysql_admin_url: str,
) -> None:
    """业务 SQL 与框架初始化入口组合后必须得到唯一当前 Schema"""

    admin_url = make_url(mysql_admin_url)
    token = secrets.token_hex(8)
    sql_database = f"tinkerfin_schema_{token}_sql"
    runtime_database = f"tinkerfin_schema_{token}_runtime"
    sql_url = _database_url(admin_url, sql_database)
    runtime_url = _database_url(admin_url, runtime_database)
    admin_engine = create_async_engine(admin_url)
    sql_engine: AsyncEngine | None = None
    runtime_engine: AsyncEngine | None = None
    created_databases: list[str] = []
    try:
        for database_name in (sql_database, runtime_database):
            await _create_database(admin_engine, database_name)
            created_databases.append(database_name)
        sql_engine = create_async_engine(sql_url)
        runtime_engine = create_async_engine(runtime_url)
        # 外部库默认值不能改变业务表的文本比较规则
        for engine in (sql_engine, runtime_engine):
            await _execute_ddl(engine, "ALTER DATABASE COLLATE utf8mb4_unicode_ci")
        await _execute_ddl(sql_engine, _SCHEMA_PATH.read_text(encoding="utf-8"))
        async with runtime_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(sql_engine) as session:
            initial_user = (await session.scalars(select(User))).one()
            assert initial_user.username == "tinkerfin"
            assert initial_user.display_name == "TinkerFin"
            assert initial_user.roles == []
            assert initial_user.disabled is False
            assert await verify_password("123456", initial_user.password_hash)
        async with sql_engine.connect() as connection:
            business_schema = await connection.run_sync(_reflect_schema)
        assert set(business_schema.tables) == _BUSINESS_TABLES
        for table in business_schema.tables.values():
            assert table.engine == "InnoDB"
            assert table.collation == "utf8mb4_0900_ai_ci"
            for column in table.columns.values():
                if column.character_set is not None:
                    assert column.character_set == "utf8mb4"
                    assert column.collation == "utf8mb4_0900_ai_ci"

        for engine in (sql_engine, runtime_engine):
            await _execute_ddl(engine, "ALTER DATABASE COLLATE utf8mb4_0900_ai_ci")
        await _create_runtime_schema(sql_engine)
        await _create_runtime_schema(runtime_engine)

        async with sql_engine.connect() as connection:
            sql_schema = await connection.run_sync(_reflect_schema)
        async with runtime_engine.connect() as connection:
            runtime_schema = await connection.run_sync(_reflect_schema)

        assert set(sql_schema.tables) == _EXPECTED_TABLES
        assert set(runtime_schema.tables) == _EXPECTED_TABLES
        assert sql_schema.tables == runtime_schema.tables
        assert sql_schema.tables["conversation_threads"].check_constraints == {}
        assert (
            sql_schema.tables["conversation_run_registrations"].check_constraints == {}
        )
        assert {
            table_name: sql_schema.table_comments[table_name]
            for table_name in _BUSINESS_TABLES
        } == {
            table_name: runtime_schema.table_comments[table_name]
            for table_name in _BUSINESS_TABLES
        }
        assert {
            table_name: sql_schema.column_comments[table_name]
            for table_name in _BUSINESS_TABLES
        } == {
            table_name: runtime_schema.column_comments[table_name]
            for table_name in _BUSINESS_TABLES
        }
        assert (
            sql_schema.table_comments["tinkerfin_store_documents"]
            == (runtime_schema.table_comments["tinkerfin_store_documents"])
        )
        assert (
            sql_schema.column_comments["tinkerfin_store_documents"]
            == (runtime_schema.column_comments["tinkerfin_store_documents"])
        )
        assert all(not table.foreign_keys for table in sql_schema.tables.values())
        assert all(not table.foreign_keys for table in runtime_schema.tables.values())
        assert all(sql_schema.table_comments.values())
        missing_column_comments = sorted(
            f"{table_name}.{column_name}"
            for table_name, comments in sql_schema.column_comments.items()
            for column_name, comment in comments.items()
            if not comment
        )
        assert missing_column_comments == []
    finally:
        if sql_engine is not None:
            await sql_engine.dispose()
        if runtime_engine is not None:
            await runtime_engine.dispose()
        for database_name in reversed(created_databases):
            await _drop_database(admin_engine, database_name)
        await admin_engine.dispose()
