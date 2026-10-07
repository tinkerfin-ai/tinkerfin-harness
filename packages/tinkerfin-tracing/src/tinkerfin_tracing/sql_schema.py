"""One deterministic SQLAlchemy Schema for durable Trace Store implementations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import (
    CreateIndex,
    CreateTable,
    SetColumnComment,
    SetTableComment,
)
from sqlalchemy.types import TypeDecorator

from .errors import TraceStoreProtocolError


class _TraceText(TypeDecorator[str]):
    """Preserve opaque UTF-8 strings, including NUL, in every SQL text column.

    PostgreSQL cannot store a literal NUL in TEXT. A JSON string is the sole stored
    representation; queries bind the same representation and return decoded values.
    Hashes and domain ordering use the original strings. Graph namespace arrays
    already have a canonical JSON representation and do not use this type.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """Encode one text value without changing its identity."""

        return None if value is None else json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        """Reject stored text that is not the current JSON string representation."""

        if value is None:
            return None
        try:
            decoded: object = json.loads(value)
        except ValueError as error:
            raise TraceStoreProtocolError(
                "Trace SQL text is invalid", cause=error
            ) from error
        if (
            not isinstance(decoded, str)
            or json.dumps(decoded, ensure_ascii=False) != value
        ):
            raise TraceStoreProtocolError("Trace SQL text is not canonical")
        return decoded


TRACE_TABLE_NAMES = (
    "tinkerfin_trace_namespaces",
    "tinkerfin_trace_threads",
    "tinkerfin_trace_writers",
    "tinkerfin_trace_events",
    "tinkerfin_trace_projection_checkpoints",
    "tinkerfin_trace_graph_nodes",
)

metadata = MetaData()

# MySQL's generic BLOB is limited to 64 KiB, below TraceLimits.max_event_bytes.
# The variants preserve one logical metadata graph while keeping SQLite portable and
# MySQL capable of storing the framework's bounded event and checkpoint payloads.
_OPAQUE_PAYLOAD = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
_HASH_KEY = LargeBinary(32).with_variant(mysql.BINARY(32), "mysql")
_DATABASE_TIMESTAMP = DateTime(timezone=False).with_variant(
    mysql.DATETIME(fsp=6),
    "mysql",
)

namespaces = Table(
    TRACE_TABLE_NAMES[0],
    metadata,
    Column(
        "namespace_hash",
        _HASH_KEY,
        primary_key=True,
        comment="SHA-256 key for the logical namespace",
    ),
    Column(
        "namespace",
        _TraceText(),
        nullable=False,
        comment="JSON string containing logical Trace Store namespace",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC creation time",
    ),
    comment="Trace namespace ownership",
)

threads = Table(
    TRACE_TABLE_NAMES[1],
    metadata,
    Column(
        "namespace_hash", _HASH_KEY, primary_key=True, comment="SHA-256 namespace key"
    ),
    Column("thread_hash", _HASH_KEY, primary_key=True, comment="SHA-256 thread key"),
    Column(
        "namespace",
        _TraceText(),
        nullable=False,
        comment="JSON string containing logical Trace namespace",
    ),
    Column(
        "thread_id",
        _TraceText(),
        nullable=False,
        comment="JSON string containing canonical semantic thread ID",
    ),
    Column(
        "generation",
        String(64),
        nullable=False,
        comment="Non-reusable current generation ID",
    ),
    Column(
        "next_seq",
        BigInteger,
        nullable=False,
        comment="Next one-based global Trace sequence",
    ),
    Column(
        "persisted_bytes",
        BigInteger,
        nullable=False,
        comment="Canonical encoded event bytes in this generation",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC generation creation time",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC last mutation time",
    ),
    comment="Current Trace generation and sequence allocator",
)
Index(
    "ix_tinkerfin_trace_threads_generation",
    threads.c.namespace_hash,
    threads.c.generation,
    unique=True,
)

writers = Table(
    TRACE_TABLE_NAMES[2],
    metadata,
    Column(
        "namespace_hash", _HASH_KEY, primary_key=True, comment="SHA-256 namespace key"
    ),
    Column("thread_hash", _HASH_KEY, primary_key=True, comment="SHA-256 thread key"),
    Column(
        "generation", String(64), primary_key=True, comment="Exact Trace generation ID"
    ),
    Column("run_hash", _HASH_KEY, primary_key=True, comment="SHA-256 Run key"),
    Column(
        "run_id",
        _TraceText(),
        nullable=False,
        comment="JSON string containing canonical semantic Run ID",
    ),
    Column(
        "owner_token",
        String(64),
        nullable=False,
        comment="Opaque current writer ownership token",
    ),
    Column(
        "fence", BigInteger, nullable=False, comment="Monotonic writer fencing token"
    ),
    Column(
        "lease_expires_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database-clock writer lease deadline",
    ),
    Column(
        "active", Boolean, nullable=False, comment="Whether the writer may still append"
    ),
    Column(
        "terminal_committed",
        Boolean,
        nullable=False,
        comment="Whether the exactly-once Run terminal fact is committed",
    ),
    Column(
        "closed_committed",
        Boolean,
        nullable=False,
        comment="Whether the Runtime cleanup completion fact is committed",
    ),
    Column(
        "committed_events",
        BigInteger,
        nullable=False,
        comment="Events committed by this Run writer",
    ),
    Column(
        "remaining_event_reserve",
        BigInteger,
        nullable=False,
        comment="Reserved terminal event capacity",
    ),
    Column(
        "remaining_byte_reserve",
        BigInteger,
        nullable=False,
        comment="Reserved terminal canonical bytes",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC first writer creation time",
    ),
    Column(
        "updated_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC last ownership mutation time",
    ),
    comment="Exclusive Run writer ownership, lease, fence, and terminal reserve",
)
Index(
    "ix_tinkerfin_trace_writers_active_lease",
    writers.c.namespace_hash,
    writers.c.thread_hash,
    writers.c.generation,
    writers.c.active,
    writers.c.lease_expires_at,
)

events = Table(
    TRACE_TABLE_NAMES[3],
    metadata,
    Column(
        "namespace_hash", _HASH_KEY, primary_key=True, comment="SHA-256 namespace key"
    ),
    Column("thread_hash", _HASH_KEY, primary_key=True, comment="SHA-256 thread key"),
    Column(
        "generation", String(64), primary_key=True, comment="Exact Trace generation ID"
    ),
    Column(
        "trace_seq",
        BigInteger,
        primary_key=True,
        comment="One-based global sequence in the generation",
    ),
    Column(
        "event_id",
        String(64),
        nullable=False,
        comment="Idempotent Store event identity",
    ),
    Column("run_hash", _HASH_KEY, nullable=False, comment="SHA-256 Run key"),
    Column(
        "run_id",
        _TraceText(),
        nullable=False,
        comment="JSON string containing semantic Run owning this fact",
    ),
    Column(
        "fact_kind",
        String(64),
        nullable=False,
        comment="Queryable current semantic fact discriminator",
    ),
    Column(
        "occurred_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Source UTC fact timestamp",
    ),
    Column(
        "payload",
        _OPAQUE_PAYLOAD,
        nullable=False,
        comment="Opaque canonical fact payload bytes",
    ),
    Column(
        "payload_digest",
        String(64),
        nullable=False,
        comment="SHA-256 of canonical pre-storage payload",
    ),
    Column(
        "persisted_bytes",
        BigInteger,
        nullable=False,
        comment="Measured current TraceEvent encoded bytes",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC commit time",
    ),
    comment="Authoritative semantic Trace Ledger events",
    mysql_engine="InnoDB",
    mysql_row_format="COMPRESSED",
    mysql_key_block_size="8",
)
Index(
    "uq_tinkerfin_trace_events_id",
    events.c.namespace_hash,
    events.c.event_id,
    unique=True,
)
Index(
    "ix_tinkerfin_trace_events_run",
    events.c.namespace_hash,
    events.c.thread_hash,
    events.c.generation,
    events.c.run_hash,
    events.c.trace_seq,
)

projection_checkpoints = Table(
    TRACE_TABLE_NAMES[4],
    metadata,
    Column(
        "namespace_hash", _HASH_KEY, primary_key=True, comment="SHA-256 namespace key"
    ),
    Column("thread_hash", _HASH_KEY, primary_key=True, comment="SHA-256 thread key"),
    Column(
        "generation", String(64), primary_key=True, comment="Exact Trace generation ID"
    ),
    Column(
        "projection_hash",
        _HASH_KEY,
        primary_key=True,
        comment="SHA-256 Projection key",
    ),
    Column(
        "run_scope_hash", _HASH_KEY, primary_key=True, comment="SHA-256 Run scope key"
    ),
    Column(
        "projection_name",
        _TraceText(),
        nullable=False,
        comment="JSON string containing canonical Projection identity",
    ),
    Column(
        "run_scope",
        _TraceText(),
        nullable=False,
        comment="JSON string containing run ID or empty thread-wide scope",
    ),
    Column(
        "as_of_seq",
        BigInteger,
        primary_key=True,
        comment="Fixed Ledger prefix represented by state",
    ),
    Column(
        "state_payload",
        _OPAQUE_PAYLOAD,
        nullable=False,
        comment="Opaque canonical Projection state bytes",
    ),
    Column(
        "state_digest",
        String(64),
        nullable=False,
        comment="SHA-256 of canonical pre-storage state",
    ),
    Column(
        "created_at",
        _DATABASE_TIMESTAMP,
        nullable=False,
        comment="Database UTC checkpoint commit time",
    ),
    comment="Disposable Projection checkpoint history",
    mysql_engine="InnoDB",
    mysql_row_format="COMPRESSED",
    mysql_key_block_size="8",
)
graph_nodes = Table(
    TRACE_TABLE_NAMES[5],
    metadata,
    Column(
        "namespace_hash", _HASH_KEY, primary_key=True, comment="SHA-256 namespace key"
    ),
    Column("thread_hash", _HASH_KEY, primary_key=True, comment="SHA-256 thread key"),
    Column(
        "generation", String(64), primary_key=True, comment="Exact Trace generation ID"
    ),
    Column("node_hash", _HASH_KEY, primary_key=True, comment="SHA-256 Graph node key"),
    Column(
        "node_id",
        _TraceText(),
        nullable=False,
        comment="JSON string containing canonical Graph node identity",
    ),
    Column(
        "parent_subagent_hash",
        _HASH_KEY,
        nullable=True,
        comment="SHA-256 owning Subagent key when present",
    ),
    Column(
        "parent_subagent_id",
        _TraceText(),
        nullable=True,
        comment="JSON string containing nearest owning Subagent, null for the Turn root scope",
    ),
    Column(
        "model_call_hash",
        _HASH_KEY,
        nullable=True,
        comment="SHA-256 emitting model-call key when present",
    ),
    Column(
        "model_call_id",
        _TraceText(),
        nullable=True,
        comment="JSON string containing model call that emitted this Assistant, Tool, or Subagent",
    ),
    Column(
        "model_call_seq",
        BigInteger,
        nullable=True,
        comment="Ledger sequence proving the emitting Model relationship",
    ),
    Column(
        "kind",
        String(32),
        nullable=True,
        comment="Graph node kind, null only on a removal revision",
    ),
    Column(
        "status",
        String(32),
        nullable=True,
        comment="Graph node status, null only on a removal revision",
    ),
    Column(
        "name",
        _TraceText(),
        nullable=True,
        comment="JSON string containing graph node display name, null on a removal revision",
    ),
    Column(
        "run_hash",
        _HASH_KEY,
        primary_key=True,
        comment="SHA-256 Run revision key",
    ),
    Column(
        "run_id",
        _TraceText(),
        nullable=False,
        comment="JSON string containing semantic Run owning the node",
    ),
    Column(
        "removed",
        Boolean,
        nullable=False,
        comment="Whether this Run revision hides an inherited node",
    ),
    Column(
        "graph_namespace_hash",
        _HASH_KEY,
        nullable=True,
        comment="SHA-256 graph namespace key, null on a removal revision",
    ),
    Column(
        "graph_namespace",
        Text,
        nullable=True,
        comment="Canonical JSON graph namespace, null on a removal revision",
    ),
    Column(
        "agent_hash",
        _HASH_KEY,
        nullable=True,
        comment="SHA-256 Agent name key when present",
    ),
    Column(
        "agent_name",
        _TraceText(),
        nullable=True,
        comment="JSON string containing named Agent when present",
    ),
    Column(
        "provider_hash",
        _HASH_KEY,
        nullable=True,
        comment="SHA-256 model provider key when present",
    ),
    Column(
        "provider",
        _TraceText(),
        nullable=True,
        comment="JSON string containing model provider when present",
    ),
    Column(
        "model_hash",
        _HASH_KEY,
        nullable=True,
        comment="SHA-256 model name key when present",
    ),
    Column(
        "model",
        _TraceText(),
        nullable=True,
        comment="JSON string containing model name when present",
    ),
    Column(
        "started_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="Source UTC node start time, null on a removal revision",
    ),
    Column(
        "first_output_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="Source UTC first model output time",
    ),
    Column(
        "completed_at",
        _DATABASE_TIMESTAMP,
        nullable=True,
        comment="Source UTC node completion time",
    ),
    Column(
        "started_seq",
        BigInteger,
        nullable=True,
        comment="Ledger node-creation sequence, null on a removal revision",
    ),
    Column(
        "updated_seq",
        BigInteger,
        nullable=False,
        comment="Ledger sequence containing the current lifecycle update",
    ),
    Column(
        "request_seq",
        BigInteger,
        nullable=True,
        comment="Ledger sequence containing current request details",
    ),
    Column(
        "result_seq",
        BigInteger,
        nullable=True,
        comment="Ledger sequence containing current result details",
    ),
    Column(
        "failure_seq",
        BigInteger,
        nullable=True,
        comment="Ledger sequence containing authoritative failure details",
    ),
    Column(
        "link_issue",
        String(64),
        nullable=True,
        comment="Missing evidence that prevented one exact relationship",
    ),
    comment="Disposable payload-free canonical Trace Graph index",
)
Index(
    "ix_tinkerfin_trace_graph_run",
    graph_nodes.c.namespace_hash,
    graph_nodes.c.thread_hash,
    graph_nodes.c.generation,
    graph_nodes.c.run_hash,
    graph_nodes.c.node_hash,
)


@dataclass(frozen=True, slots=True)
class TraceStoreSchema:
    """Describe deterministic full empty-database DDL for one supported dialect.

    Attributes:
        dialect: SQLite, MySQL, or PostgreSQL compilation target.
        table_names: Exact tables owned by the Trace framework.
        ddl: Complete deterministic tables, indexes, and supported database comments.
    """

    dialect: Literal["mysql", "sqlite", "postgresql"]
    table_names: tuple[str, ...]
    ddl: str


def get_trace_store_schema(
    *, dialect: Literal["mysql", "sqlite", "postgresql"]
) -> TraceStoreSchema:
    """Compile the exact metadata used by automatic Store setup.

    Args:
        dialect: Supported deployment dialect.

    Returns:
        Immutable table ownership and deterministic DDL text.

    Raises:
        ValueError: ``dialect`` is not SQLite, MySQL, or PostgreSQL.
    """

    if dialect == "sqlite":
        selected = sqlite.dialect()
    elif dialect == "mysql":
        selected = mysql.dialect()
    elif dialect == "postgresql":
        selected = postgresql.dialect()
    else:
        raise ValueError("dialect must be 'sqlite', 'mysql', or 'postgresql'")
    statements = [
        str(CreateTable(table).compile(dialect=selected)).strip()
        for table in metadata.sorted_tables
    ]
    statements.extend(
        str(CreateIndex(index).compile(dialect=selected)).strip()
        for table in metadata.sorted_tables
        for index in sorted(table.indexes, key=lambda item: item.name or "")
    )
    if dialect == "postgresql":
        # PostgreSQL stores comments separately from CREATE TABLE. Match the DDL
        # emitted by SQLAlchemy MetaData.create_all for empty database setup.
        for table in metadata.sorted_tables:
            if table.comment is not None:
                statements.append(str(SetTableComment(table).compile(dialect=selected)))
            statements.extend(
                str(SetColumnComment(column).compile(dialect=selected))
                for column in table.columns
                if column.comment is not None
            )
    return TraceStoreSchema(
        dialect=dialect,
        table_names=TRACE_TABLE_NAMES,
        ddl=";\n\n".join(statements) + ";\n",
    )


__all__ = [
    "TRACE_TABLE_NAMES",
    "TraceStoreSchema",
    "get_trace_store_schema",
    "metadata",
]
