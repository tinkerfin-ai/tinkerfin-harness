"""SQLAlchemy table metadata, schema validation, and deployable DDL."""

from __future__ import annotations

__all__ = ["_initialize_schema", "_validate_schema"]

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import postgresql as postgresql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.engine import Connection
from sqlalchemy.schema import (
    CreateIndex,
    CreateTable,
    DefaultClause,
    SetColumnComment,
    SetTableComment,
)

from tinkerfin_sqlalchemy import SqlDialect

from ..errors import OpenSandboxStateError

if TYPE_CHECKING:
    from .sqlalchemy import SQLAlchemyOpenSandboxStateSchema

_metadata = MetaData()
# Fractional lease durations must survive storage. MySQL DATETIME otherwise drops
# them; test_sql_state_preserves_subsecond_leases checks the persisted deadlines.
_DATABASE_TIMESTAMP = DateTime(timezone=False).with_variant(
    mysql_dialect.DATETIME(fsp=6), "mysql"
)


_owners = Table(
    "tinkerfin_opensandbox_owners",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "owner_digest",
        String(43),
        nullable=False,
        comment="URL-safe SHA-256 digest of the namespace and owner key",
    ),
    Column(
        "sandbox_id",
        String(255),
        nullable=True,
        comment="Currently committed remote OpenSandbox identifier",
    ),
    Column(
        "purpose",
        String(10),
        nullable=True,
        comment="Immutable commands or workspaces capability of the current binding; null when unbound",
    ),
    Column(
        "binding_generation",
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="Fencing generation that committed the current Sandbox binding",
    ),
    Column(
        "generation",
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="Monotonic fencing generation for owner transitions",
    ),
    Column(
        "claim_token",
        String(32),
        nullable=True,
        comment="Opaque token of the worker currently changing this owner",
    ),
    Column(
        "lease_expires_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="UTC expiry of the current owner transition lease",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC time of the latest owner state mutation",
    ),
    PrimaryKeyConstraint("namespace", "owner_digest"),
    CheckConstraint(
        "(sandbox_id IS NULL AND purpose IS NULL) OR "
        "(sandbox_id IS NOT NULL AND purpose IS NOT NULL "
        "AND purpose IN ('commands', 'workspaces'))",
        name="ck_tinkerfin_opensandbox_owners_purpose",
    ),
    comment="Authoritative owner binding and transition fencing state",
)


Index(
    "ix_tinkerfin_opensandbox_owners_lease",
    _owners.c.namespace,
    _owners.c.lease_expires_at,
)


def _type_signature(
    column_type: object, *, dialect_name: str
) -> tuple[str, int | None]:
    """Normalize reflected SQLAlchemy types into the schema contract."""
    if isinstance(column_type, String):
        return ("string", column_type.length)
    if isinstance(column_type, BigInteger):
        return ("bigint", None)
    if isinstance(column_type, Integer):
        return ("integer", None)
    if isinstance(column_type, mysql_dialect.DATETIME):
        return ("datetime", column_type.fsp or 0)
    if isinstance(column_type, postgresql_dialect.TIMESTAMP):
        precision = 6 if column_type.precision is None else column_type.precision
        return ("datetime_timezone" if column_type.timezone else "datetime", precision)
    if isinstance(column_type, DateTime):
        # An unqualified PostgreSQL TIMESTAMP preserves six fractional digits.
        return (
            "datetime_timezone" if column_type.timezone else "datetime",
            6 if dialect_name == "postgresql" else None,
        )
    return (type(column_type).__name__.lower(), None)


def _default_signature(value: object | None) -> str | None:
    """Normalize equivalent SQLAlchemy reflected server defaults."""
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


def _validate_schema(sync_connection: Connection) -> None:
    """Reject existing tables that do not match the internal State schema."""
    inspector = sa_inspect(sync_connection)
    issues: list[str] = []
    expected_tables = {str(table.name) for table in _metadata.sorted_tables}
    actual_tables = {
        str(name)
        for name in inspector.get_table_names()
        if str(name).startswith("tinkerfin_opensandbox_")
    }
    unexpected_tables = sorted(actual_tables - expected_tables)
    if unexpected_tables:
        issues.append(f"unexpected tables={unexpected_tables}")
    for table in _metadata.sorted_tables:
        if not inspector.has_table(table.name):
            issues.append(f"{table.name}: missing table")
            continue

        if (
            sync_connection.dialect.name != "sqlite"
            and inspector.get_table_comment(table.name).get("text") != table.comment
        ):
            issues.append(f"{table.name}: incompatible comment")
        reflected_columns = {
            str(column["name"]): column for column in inspector.get_columns(table.name)
        }
        expected_names = {column.name for column in table.columns}
        actual_names = set(reflected_columns)
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            unexpected = sorted(actual_names - expected_names)
            issues.append(
                f"{table.name}: columns differ; missing={missing}, "
                f"unexpected={unexpected}"
            )
            continue

        for expected in table.columns:
            actual = reflected_columns[expected.name]
            if (
                sync_connection.dialect.name != "sqlite"
                and actual.get("comment") != expected.comment
            ):
                issues.append(f"{table.name}.{expected.name}: incompatible comment")
            if _type_signature(
                actual["type"], dialect_name=sync_connection.dialect.name
            ) != _type_signature(
                expected.type.dialect_impl(sync_connection.dialect),
                dialect_name=sync_connection.dialect.name,
            ):
                issues.append(f"{table.name}.{expected.name}: incompatible type")
            if bool(actual["nullable"]) != bool(expected.nullable):
                issues.append(f"{table.name}.{expected.name}: incompatible nullable")
            server_default = expected.server_default
            expected_default = _default_signature(
                server_default.arg
                if isinstance(server_default, DefaultClause)
                else server_default
            )
            actual_default = _default_signature(actual.get("default"))
            if actual_default != expected_default:
                issues.append(f"{table.name}.{expected.name}: incompatible default")

        reflected_primary_key = tuple(
            inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
        )
        expected_primary_key = tuple(
            column.name for column in table.primary_key.columns
        )
        if reflected_primary_key != expected_primary_key:
            issues.append(f"{table.name}: incompatible primary key")

        reflected_indexes = {
            str(index["name"]): (
                tuple(index.get("column_names") or ()),
                bool(index.get("unique", False)),
            )
            for index in inspector.get_indexes(table.name)
            if index.get("name") is not None
        }
        expected_indexes: dict[str, tuple[tuple[str, ...], bool]] = {}
        for expected_index in table.indexes:
            expected_name = expected_index.name
            if expected_name is None:
                issues.append(f"{table.name}: expected index has no name")
                continue
            expected_columns = tuple(column.name for column in expected_index.columns)
            expected_indexes[expected_name] = (
                expected_columns,
                bool(expected_index.unique),
            )
        missing_indexes = sorted(expected_indexes.keys() - reflected_indexes.keys())
        unexpected_indexes = sorted(reflected_indexes.keys() - expected_indexes.keys())
        incompatible_indexes = sorted(
            name
            for name in expected_indexes.keys() & reflected_indexes.keys()
            if reflected_indexes[name] != expected_indexes[name]
        )
        if missing_indexes or unexpected_indexes or incompatible_indexes:
            issues.append(
                f"{table.name}: indexes differ; missing={missing_indexes}, "
                f"unexpected={unexpected_indexes}, "
                f"incompatible={incompatible_indexes}"
            )

    if issues:
        raise OpenSandboxStateError(
            "OpenSandbox State schema is incompatible: " + "; ".join(issues)
        )


def _initialize_schema(sync_connection: Connection) -> None:
    """Create an empty current schema or reject any non-current owned structure."""

    inspector = sa_inspect(sync_connection)
    owned_tables = {
        str(name)
        for name in inspector.get_table_names()
        if str(name).startswith("tinkerfin_opensandbox_")
    }
    if not owned_tables:
        _metadata.create_all(sync_connection)
    _validate_schema(sync_connection)


_workers = Table(
    "tinkerfin_opensandbox_workers",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "worker_id",
        String(32),
        nullable=False,
        comment="Opaque identifier of one live State instance",
    ),
    Column(
        "warm_pool_size",
        Integer,
        nullable=False,
        comment="Global ready Sandbox capacity requested by this worker",
    ),
    Column(
        "lease_expires_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC expiry used to ignore workers that exited without cleanup",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC time of the latest Worker registration mutation",
    ),
    PrimaryKeyConstraint("namespace", "worker_id"),
    comment="Live OpenSandbox State workers and shared warm-pool agreement",
)


Index(
    "ix_tinkerfin_opensandbox_workers_lease",
    _workers.c.namespace,
    _workers.c.lease_expires_at,
)


_warm_slots = Table(
    "tinkerfin_opensandbox_warm_slots",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "slot",
        Integer,
        nullable=False,
        comment="Zero-based global warm-pool slot within the namespace",
    ),
    Column(
        "sandbox_id",
        String(255),
        nullable=True,
        comment="Ready remote Sandbox currently held by this global slot",
    ),
    Column(
        "generation",
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="Monotonic fencing generation for this warm slot",
    ),
    Column(
        "claim_token",
        String(32),
        nullable=True,
        comment="Opaque token of the worker currently filling this slot",
    ),
    Column(
        "lease_expires_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="UTC expiry of the current warm-slot fill lease",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC time of the latest warm-slot mutation",
    ),
    PrimaryKeyConstraint("namespace", "slot"),
    comment="Database-global OpenSandbox warm-pool slots",
)


Index(
    "ix_tinkerfin_opensandbox_warm_slots_available",
    _warm_slots.c.namespace,
    _warm_slots.c.sandbox_id,
    _warm_slots.c.lease_expires_at,
)


_cleanup = Table(
    "tinkerfin_opensandbox_cleanup",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "sandbox_id",
        String(255),
        nullable=False,
        comment="Orphaned remote Sandbox awaiting confirmed destruction",
    ),
    Column(
        "generation",
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="Monotonic fencing generation for cleanup attempts",
    ),
    Column(
        "claim_token",
        String(32),
        nullable=True,
        comment="Opaque token of the worker currently destroying this Sandbox",
    ),
    Column(
        "lease_expires_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="UTC expiry of the current cleanup lease",
    ),
    Column(
        "attempts",
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="Number of times this cleanup target has been claimed",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC time when this cleanup target was first enqueued",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="UTC time of the latest cleanup mutation",
    ),
    PrimaryKeyConstraint("namespace", "sandbox_id"),
    comment="Durable retry queue for failed remote Sandbox destruction",
)


Index(
    "ix_tinkerfin_opensandbox_cleanup_lease",
    _cleanup.c.namespace,
    _cleanup.c.lease_expires_at,
)


_availability = Table(
    "tinkerfin_opensandbox_availability",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "owner_digest",
        String(43),
        nullable=False,
        comment="URL-safe SHA-256 digest of the namespace and owner key",
    ),
    Column(
        "sandbox_id",
        String(255),
        nullable=False,
        comment="Remote OpenSandbox identifier of the registered binding",
    ),
    Column(
        "binding_generation",
        BigInteger,
        nullable=False,
        comment="Owner fencing generation that committed this binding",
    ),
    Column(
        "sequence",
        BigInteger,
        nullable=False,
        comment="Monotonic availability intent sequence within this binding",
    ),
    Column(
        "phase",
        String(16),
        nullable=False,
        comment="Current intent: running, draining, pausing, paused, resuming, or uncertain",
    ),
    Column(
        "connection_generation",
        BigInteger,
        nullable=False,
        comment="Connection refresh counter required before admitting operations",
    ),
    PrimaryKeyConstraint("namespace", "owner_digest"),
    comment="Authoritative availability intent used to coordinate all handle holders",
)

_holders = Table(
    "tinkerfin_opensandbox_holders",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "owner_digest",
        String(43),
        nullable=False,
        comment="URL-safe SHA-256 digest of the namespace and owner key",
    ),
    Column(
        "sandbox_id",
        String(255),
        nullable=False,
        comment="Remote OpenSandbox identifier of the registered binding",
    ),
    Column(
        "binding_generation",
        BigInteger,
        nullable=False,
        comment="Owner fencing generation that committed this binding",
    ),
    Column(
        "holder_id",
        String(36),
        nullable=False,
        comment="Unique manager lifetime identity never reused after shutdown",
    ),
    Column(
        "acknowledged_sequence",
        BigInteger,
        nullable=True,
        comment="Explicit current drain sequence acknowledged after local operations settle",
    ),
    PrimaryKeyConstraint("namespace", "holder_id", "owner_digest"),
    comment="Durable handle registrations requiring explicit idle evidence",
)

Index(
    "ix_tinkerfin_opensandbox_holders_binding",
    _holders.c.namespace,
    _holders.c.owner_digest,
    _holders.c.binding_generation,
)


def get_sqlalchemy_opensandbox_state_schema(
    *,
    dialect: SqlDialect,
) -> SQLAlchemyOpenSandboxStateSchema:
    """Build the complete OpenSandbox State schema without database I/O.

    Args:
        dialect: Deployment SQL dialect. MySQL output targets the common MySQL
            5.7 and 8.x DDL subset.

    Returns:
        An immutable descriptor containing deterministic full-database DDL.

    Raises:
        ValueError: The requested dialect is unsupported.
    """

    from .sqlalchemy import SQLAlchemyOpenSandboxStateSchema

    if dialect == "mysql":
        compiler = mysql_dialect.dialect()
        compiler.server_version_info = (5, 7, 0)
    elif dialect == "sqlite":
        compiler = sqlite_dialect.dialect()
    elif dialect == "postgresql":
        compiler = postgresql_dialect.dialect()
    else:
        raise ValueError("dialect must be 'mysql', 'sqlite', or 'postgresql'")

    tables = tuple(_metadata.sorted_tables)
    statements = [
        str(CreateTable(table).compile(dialect=compiler)).strip() for table in tables
    ]
    indexes = sorted(
        (index for table in tables for index in table.indexes),
        key=lambda index: str(index.name),
    )
    statements.extend(
        str(CreateIndex(index).compile(dialect=compiler)).strip() for index in indexes
    )
    if dialect == "postgresql":
        statements.extend(
            str(SetTableComment(table).compile(dialect=compiler)).strip()
            for table in tables
            if table.comment
        )
        statements.extend(
            str(SetColumnComment(column).compile(dialect=compiler)).strip()
            for table in tables
            for column in table.columns
            if column.comment
        )
    normalized = tuple(
        "\n".join(line.rstrip() for line in statement.splitlines())
        for statement in statements
    )
    return SQLAlchemyOpenSandboxStateSchema(
        dialect=dialect,
        table_names=tuple(str(table.name) for table in tables),
        ddl=";\n\n".join(normalized) + ";\n",
    )
