"""Single-current SQLAlchemy Schema for durable Automation storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Never, cast

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    inspect as sa_inspect,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Connection
from sqlalchemy.schema import (
    CreateIndex,
    CreateTable,
    DefaultClause,
    SetColumnComment,
    SetTableComment,
)
from sqlalchemy.sql.type_api import TypeEngine

from .errors import AutomationStoreProtocolError

AUTOMATION_TABLE_NAMES = (
    "tinkerfin_automation_tasks",
    "tinkerfin_automation_runs",
    "tinkerfin_automation_work_items",
    "tinkerfin_automation_scopes",
    "tinkerfin_automation_operations",
)

metadata = MetaData()
_DATABASE_TIMESTAMP = DateTime(timezone=False).with_variant(
    mysql.DATETIME(fsp=6), "mysql"
)
_JSON_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")
_HASH_KEY = LargeBinary(32).with_variant(mysql.BINARY(32), "mysql")

tasks = Table(
    AUTOMATION_TABLE_NAMES[0],
    metadata,
    Column("task_id", String(36), primary_key=True, comment="Framework task identity"),
    Column(
        "namespace",
        String(128),
        nullable=False,
        comment="Host-selected isolation namespace",
    ),
    Column(
        "owner_id",
        String(191),
        nullable=False,
        comment="Host-authorized business owner identity",
    ),
    Column("name", String(255), nullable=False, comment="User-visible task name"),
    Column(
        "search_name",
        String(765),
        nullable=False,
        comment="Unicode case-folded name for literal search",
    ),
    Column(
        "status",
        String(16),
        nullable=False,
        comment="Current enabled or paused task state",
    ),
    Column(
        "revision",
        BigInteger,
        nullable=False,
        comment="Monotonic optimistic-concurrency revision",
    ),
    Column(
        "next_run_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="Next scheduled UTC wakeup or null when exhausted",
    ),
    Column(
        "payload",
        _JSON_TEXT,
        nullable=False,
        comment="Current task configuration and host-supplied JSON input",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC creation time",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC last definition change",
    ),
    CheckConstraint("revision >= 1", name="ck_tinkerfin_automation_tasks_revision"),
    UniqueConstraint(
        "namespace",
        "owner_id",
        "task_id",
        name="uq_tinkerfin_automation_tasks_owner",
    ),
    comment="Current Automation task definitions",
)
Index(
    "ix_tinkerfin_automation_tasks_due",
    tasks.c.namespace,
    tasks.c.status,
    tasks.c.next_run_at,
    tasks.c.task_id,
)


Index(
    "ix_tinkerfin_automation_tasks_owner",
    tasks.c.namespace,
    tasks.c.owner_id,
    tasks.c.created_at,
    tasks.c.task_id,
)

runs = Table(
    AUTOMATION_TABLE_NAMES[1],
    metadata,
    Column(
        "execution_id",
        String(36),
        primary_key=True,
        comment="Execution attempt identity",
    ),
    Column("namespace", String(128), nullable=False, comment="Isolation namespace"),
    Column(
        "task_name",
        String(255),
        nullable=True,
        comment="Task name captured at queue admission; null if unknown or taskless",
    ),
    Column(
        "search_name",
        String(765),
        nullable=True,
        comment="Unicode case-folded captured name for literal search",
    ),
    Column(
        "owner_id", String(191), nullable=False, comment="Authorized owner identity"
    ),
    Column(
        "task_id",
        String(36),
        nullable=True,
        comment="Source task identity; null for a one-time execution",
    ),
    Column(
        "occurrence_key",
        String(64),
        nullable=False,
        comment="Deterministic scheduled or manual occurrence key",
    ),
    Column(
        "identity_digest",
        _HASH_KEY,
        nullable=False,
        comment="SHA-256 key for the complete Runtime identity",
    ),
    Column(
        "thread_id",
        Text,
        nullable=False,
        comment="Canonical Runtime thread identity",
    ),
    Column("run_id", Text, nullable=False, comment="Canonical Runtime run identity"),
    Column(
        "status",
        String(24),
        nullable=False,
        comment="Current framework execution state",
    ),
    Column(
        "attempt",
        Integer,
        nullable=False,
        comment="One-based explicit business attempt number",
    ),
    Column(
        "retry_of",
        String(36),
        nullable=True,
        comment="Prior execution explicitly retried by this attempt",
    ),
    Column(
        "scheduled_for",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="Original scheduled UTC time; null for manual execution",
    ),
    Column(
        "queued_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC time the execution entered the queue",
    ),
    Column(
        "queue_deadline",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC deadline for starting this execution",
    ),
    Column(
        "execution_started_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="UTC time the one-time start authorization was issued",
    ),
    Column(
        "execution_deadline",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="UTC deadline covering preparation, run, interrupt, and callback",
    ),
    Column(
        "finished_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="UTC terminal settlement time",
    ),
    Column(
        "start_authorized_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="Durable one-time target start authorization time",
    ),
    Column(
        "start_token",
        String(64),
        nullable=True,
        comment="Opaque token returned only by the successful authorization call",
    ),
    Column(
        "admitted",
        Boolean,
        nullable=False,
        comment="Whether this run still owns global and task capacity",
    ),
    Column(
        "payload",
        _JSON_TEXT,
        nullable=False,
        comment="Immutable input snapshot and current execution result",
    ),
    Column(
        "created_at", _DATABASE_TIMESTAMP, nullable=False, comment="UTC creation time"
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC last state change",
    ),
    CheckConstraint("attempt >= 1", name="ck_tinkerfin_automation_runs_attempt"),
    UniqueConstraint(
        "namespace",
        "occurrence_key",
        name="uq_tinkerfin_automation_runs_occurrence",
    ),
    UniqueConstraint(
        "namespace",
        "identity_digest",
        name="uq_tinkerfin_automation_runs_identity",
    ),
    comment="Automation execution attempts and immutable Runtime bindings",
)
Index(
    "ix_tinkerfin_automation_runs_history",
    runs.c.namespace,
    runs.c.owner_id,
    runs.c.task_id,
    runs.c.queued_at,
    runs.c.execution_id,
)
Index(
    "ix_tinkerfin_automation_runs_status",
    runs.c.namespace,
    runs.c.status,
    runs.c.updated_at,
    runs.c.execution_id,
)

work_items = Table(
    AUTOMATION_TABLE_NAMES[2],
    metadata,
    Column(
        "work_item_id", String(36), primary_key=True, comment="Claimable work identity"
    ),
    Column("namespace", String(128), nullable=False, comment="Isolation namespace"),
    Column(
        "execution_id",
        String(36),
        nullable=False,
        comment="Execution owned by this work",
    ),
    Column(
        "kind",
        String(32),
        nullable=False,
        comment="Execute or interrupted-deadline action",
    ),
    Column(
        "status",
        String(16),
        nullable=False,
        comment="Pending, claimed, or completed work state",
    ),
    Column(
        "available_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC earliest claim time",
    ),
    Column(
        "worker_id",
        String(191),
        nullable=True,
        comment="Worker owning the current lease",
    ),
    Column(
        "claim_token",
        String(64),
        nullable=True,
        comment="Opaque current claim token",
    ),
    Column(
        "fence",
        BigInteger,
        nullable=False,
        comment="Monotonic fencing value incremented on every claim",
    ),
    Column(
        "lease_until",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="Database UTC lease deadline",
    ),
    Column(
        "created_at", _DATABASE_TIMESTAMP, nullable=False, comment="UTC creation time"
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC last claim change",
    ),
    CheckConstraint("fence >= 0", name="ck_tinkerfin_automation_work_fence"),
    UniqueConstraint(
        "namespace",
        "execution_id",
        "kind",
        name="uq_tinkerfin_automation_work_dedupe",
    ),
    comment="Durable Automation execution intents",
)
Index(
    "ix_tinkerfin_automation_work_available",
    work_items.c.namespace,
    work_items.c.status,
    work_items.c.available_at,
    work_items.c.work_item_id,
)
Index(
    "ix_tinkerfin_automation_work_lease",
    work_items.c.namespace,
    work_items.c.status,
    work_items.c.lease_until,
    work_items.c.work_item_id,
)

scopes = Table(
    AUTOMATION_TABLE_NAMES[3],
    metadata,
    Column("namespace", String(128), primary_key=True, comment="Isolation namespace"),
    Column(
        "kind",
        String(16),
        primary_key=True,
        comment="Global, task, or one-time owner admission scope",
    ),
    Column(
        "scope_key",
        String(191),
        primary_key=True,
        comment="Global sentinel, task identity, or owner identity",
    ),
    Column(
        "allocated",
        Integer,
        nullable=False,
        comment="Current unfinished executions holding capacity",
    ),
    Column(
        "capacity",
        Integer,
        nullable=False,
        comment="Configured maximum unfinished executions",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC last allocation change",
    ),
    CheckConstraint(
        "allocated >= 0 AND capacity >= 1 AND allocated <= capacity",
        name="ck_tinkerfin_automation_scopes_capacity",
    ),
    comment="Cross-worker global, task, and one-time owner admission counters",
)

operations = Table(
    AUTOMATION_TABLE_NAMES[4],
    metadata,
    Column("namespace", String(128), primary_key=True, comment="Isolation namespace"),
    Column(
        "owner_id", String(191), primary_key=True, comment="Authorized owner identity"
    ),
    Column(
        "request_id",
        String(128),
        primary_key=True,
        comment="Caller-supplied command idempotency identity",
    ),
    Column(
        "input_digest",
        String(64),
        nullable=False,
        comment="SHA-256 of the canonical command input",
    ),
    Column(
        "result_kind",
        String(32),
        nullable=False,
        comment="Task, execution, or deleted command result",
    ),
    Column(
        "result_payload",
        _JSON_TEXT,
        nullable=False,
        comment="Canonical snapshot returned for idempotent replay",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC command commit time",
    ),
    comment="Automation command identities and recorded results",
)
Index(
    "ix_tinkerfin_automation_operations_created",
    operations.c.namespace,
    operations.c.owner_id,
    operations.c.created_at,
)


def prepare_schema(connection: Connection) -> None:
    """Create one wholly new Automation Schema or validate the existing shape."""

    table_names = set(sa_inspect(connection).get_table_names())
    owned_names = {
        name for name in table_names if name.startswith("tinkerfin_automation_")
    }
    if not owned_names:
        metadata.create_all(connection)
    _validate_automation_schema(connection)


def _validate_automation_schema(connection: Connection) -> None:
    """Reject every partial, stale, or extended Automation-owned SQL shape."""

    inspector = sa_inspect(connection)
    table_names = set(inspector.get_table_names())
    expected_table_names = set(AUTOMATION_TABLE_NAMES)
    owned_table_names = {
        name for name in table_names if name.startswith("tinkerfin_automation_")
    }
    if owned_table_names - expected_table_names:
        _raise_schema_error("unknown Automation-owned tables exist")
    if not expected_table_names <= table_names:
        _raise_schema_error("Automation-owned tables are incomplete")

    for table in metadata.sorted_tables:
        reflected_columns = {
            reflected["name"]: reflected
            for reflected in inspector.get_columns(table.name)
        }
        expected_column_names = {column.name for column in table.columns}
        if set(reflected_columns) != expected_column_names:
            _raise_schema_error(f"table {table.name!r} has stale columns")

        for column in table.columns:
            reflected = reflected_columns[column.name]
            if bool(reflected["nullable"]) != bool(column.nullable):
                _raise_schema_error(
                    f"column {table.name}.{column.name} has stale nullability"
                )
            reflected_type = reflected.get("type")
            if not isinstance(reflected_type, TypeEngine):
                _raise_schema_error(
                    f"column {table.name}.{column.name} has an invalid reflected type"
                )
            declared_type = column.type.dialect_impl(connection.dialect)
            if (
                connection.dialect.name == "mysql"
                and isinstance(declared_type, String)
                and declared_type.collation is None
                and isinstance(reflected_type, String)
            ):
                # MySQL reflection includes inherited collations when they differ
                # from the server default; the schema leaves this host choice open.
                reflected_type = reflected_type.copy()
                reflected_type.collation = None
            expected_type_name = _normalized_type_name(
                column.type.compile(dialect=connection.dialect)
            )
            reflected_type_name = _normalized_type_name(
                reflected_type.compile(dialect=connection.dialect)
            )
            boolean_equivalent = expected_type_name in {"BOOL", "BOOLEAN"} and (
                reflected_type_name in {"BOOL", "BOOLEAN", "TINYINT(1)"}
            )
            if expected_type_name != reflected_type_name and not boolean_equivalent:
                _raise_schema_error(
                    f"column {table.name}.{column.name} has a stale type"
                )
            server_default = column.server_default
            expected_default = _normalized_default(
                server_default.arg
                if isinstance(server_default, DefaultClause)
                else server_default
            )
            reflected_default = _normalized_default(reflected.get("default"))
            if reflected_default != expected_default:
                _raise_schema_error(
                    f"column {table.name}.{column.name} has a stale default"
                )
            if (
                connection.dialect.name in {"mysql", "postgresql"}
                and reflected.get("comment") != column.comment
            ):
                _raise_schema_error(
                    f"column {table.name}.{column.name} has a stale comment"
                )

        reflected_primary_key = tuple(
            inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
        )
        expected_primary_key = tuple(
            column.name for column in table.primary_key.columns
        )
        if reflected_primary_key != expected_primary_key:
            _raise_schema_error(f"table {table.name!r} has a stale primary key")

        expected_unique_constraints = {
            _constraint_name(constraint.name, table_name=table.name): tuple(
                column.name for column in constraint.columns
            )
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        reflected_unique_constraints = {
            _constraint_name(constraint.get("name"), table_name=table.name): tuple(
                cast(list[str], constraint.get("column_names") or ())
            )
            for constraint in inspector.get_unique_constraints(table.name)
        }
        if reflected_unique_constraints != expected_unique_constraints:
            _raise_schema_error(f"table {table.name!r} has stale unique constraints")

        expected_check_constraints = {
            _constraint_name(constraint.name, table_name=table.name): (
                _normalized_check_expression(str(constraint.sqltext))
            )
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        reflected_check_constraints = {
            _constraint_name(
                constraint.get("name"), table_name=table.name
            ): _normalized_check_expression(constraint.get("sqltext") or "")
            for constraint in inspector.get_check_constraints(table.name)
        }
        if reflected_check_constraints != expected_check_constraints:
            _raise_schema_error(f"table {table.name!r} has stale check constraints")

        reflected_indexes: dict[str, tuple[tuple[str, ...], bool]] = {}
        for index in inspector.get_indexes(table.name):
            index_name = index.get("name")
            if not isinstance(index_name, str):
                _raise_schema_error(f"table {table.name!r} has an unnamed index")
            if index.get("duplicates_constraint") is not None or (
                bool(index.get("unique", False))
                and index_name in expected_unique_constraints
            ):
                continue
            reflected_indexes[index_name] = (
                tuple(cast(list[str], index.get("column_names") or ())),
                bool(index.get("unique", False)),
            )
        expected_indexes = {
            _constraint_name(index.name, table_name=table.name): (
                tuple(column.name for column in index.columns),
                bool(index.unique),
            )
            for index in table.indexes
        }
        if reflected_indexes != expected_indexes:
            _raise_schema_error(f"table {table.name!r} has stale indexes")

        if inspector.get_foreign_keys(table.name):
            _raise_schema_error(f"table {table.name!r} has unexpected foreign keys")
        if connection.dialect.name in {"mysql", "postgresql"}:
            reflected_comment = inspector.get_table_comment(table.name).get("text")
            if reflected_comment != table.comment:
                _raise_schema_error(f"table {table.name!r} has a stale comment")


def _constraint_name(name: object, *, table_name: str) -> str:
    if not isinstance(name, str):
        _raise_schema_error(f"table {table_name!r} has an unnamed constraint")
    return name


def _normalized_type_name(value: object) -> str:
    normalized = re.sub(r"\s+", "", str(value)).upper()
    # PostgreSQL's default timestamp precision is six fractional digits.
    return normalized.replace("TIMESTAMP(6)", "TIMESTAMP")


def _normalized_default(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", "", str(value)).upper()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1]
    return normalized


def _normalized_check_expression(value: str) -> str:
    without_quotes = value.translate(str.maketrans("", "", '`"[]()'))
    return re.sub(r"\s+", "", without_quotes).upper()


def _raise_schema_error(detail: str) -> Never:
    raise AutomationStoreProtocolError(
        f"Automation Store SQL schema is incompatible: {detail}"
    )


@dataclass(frozen=True, slots=True)
class AutomationStoreSchema:
    """Deterministic table names and DDL for supported SQL dialects."""

    dialect: str
    table_names: tuple[str, ...]
    statements: tuple[str, ...]


def get_automation_store_schema(dialect: str) -> AutomationStoreSchema:
    """Compile tables, indexes, and database comments for Automation storage.

    Args:
        dialect: SQLAlchemy name: ``sqlite``, ``mysql``, or ``postgresql``.

    Returns:
        Stable table names and create statements in dependency order.

    Raises:
        ValueError: The requested dialect is unsupported.
    """

    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import dialect as dialect_factory
    elif dialect == "mysql":
        from sqlalchemy.dialects.mysql import dialect as dialect_factory
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import dialect as dialect_factory
    else:
        raise ValueError("dialect must be 'sqlite', 'mysql', or 'postgresql'")
    sql_dialect = dialect_factory()
    statements: list[str] = []
    for table in (tasks, runs, work_items, scopes, operations):
        statements.append(str(CreateTable(table).compile(dialect=sql_dialect)))
        statements.extend(
            str(CreateIndex(index).compile(dialect=sql_dialect))
            for index in sorted(table.indexes, key=lambda item: item.name or "")
        )
        if dialect == "postgresql":
            statements.append(str(SetTableComment(table).compile(dialect=sql_dialect)))
            statements.extend(
                str(SetColumnComment(column).compile(dialect=sql_dialect))
                for column in table.columns
                if column.comment is not None
            )
    return AutomationStoreSchema(
        dialect=dialect,
        table_names=AUTOMATION_TABLE_NAMES,
        statements=tuple(statements),
    )


__all__ = [
    "AUTOMATION_TABLE_NAMES",
    "AutomationStoreSchema",
    "get_automation_store_schema",
    "metadata",
    "operations",
    "runs",
    "scopes",
    "tasks",
    "work_items",
]

Index(
    "ix_tinkerfin_automation_tasks_filter",
    tasks.c.namespace,
    tasks.c.owner_id,
    tasks.c.status,
    tasks.c.created_at,
    tasks.c.task_id,
)
Index(
    "ix_tinkerfin_automation_runs_filter",
    runs.c.namespace,
    runs.c.owner_id,
    runs.c.status,
    runs.c.queued_at,
    runs.c.execution_id,
)
