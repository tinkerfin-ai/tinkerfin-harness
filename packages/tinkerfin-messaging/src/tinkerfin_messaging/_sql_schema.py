"""Own the current SQL records for ordered messages and producer control."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    inspect,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Connection

from .errors import MessagingBackendProtocolError

_HASH = LargeBinary(32).with_variant(mysql.BINARY(32), "mysql")
_BYTES = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")
_TIME = DateTime(timezone=False).with_variant(mysql.DATETIME(fsp=6), "mysql")
metadata = MetaData()


def _channel_id() -> Column[bytes]:
    return Column(
        "channel_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the full UTF-8 channel name",
    )


def _thread_id() -> Column[bytes]:
    return Column(
        "thread_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the full serialized namespace and thread identity",
    )


def _generation() -> Column[int]:
    return Column(
        "generation",
        BigInteger,
        primary_key=True,
        comment="Positive generation; retained after deletion to reject stale references",
    )


capacity = Table(
    "tinkerfin_messaging_capacity",
    metadata,
    Column(
        "id",
        Integer,
        primary_key=True,
        autoincrement=False,
        comment="Single deployment record, always 1",
    ),
    Column(
        "settings",
        _TEXT,
        nullable=False,
        comment="Canonical JSON limits, retention, producer and wait settings",
    ),
    Column(
        "total_bytes",
        BigInteger,
        nullable=False,
        comment="Retained payload and checkpoint evidence bytes",
    ),
    Column(
        "total_records",
        BigInteger,
        nullable=False,
        comment="Channels, threads, generations, runs and messages, including tombstones",
    ),
    comment="Deployment settings, atomic total capacity and write exclusion",
    mysql_engine="InnoDB",
)

channels = Table(
    "tinkerfin_messaging_channels",
    metadata,
    _channel_id(),
    Column(
        "channel",
        _TEXT,
        nullable=False,
        comment="ASCII JSON string of the complete channel name, verified against channel_id",
    ),
    Column(
        "codec",
        _TEXT,
        nullable=False,
        comment="ASCII JSON string of the required stable message codec identifier",
    ),
    comment="One codec per logical channel under deployment-wide settings",
    mysql_engine="InnoDB",
)

threads = Table(
    "tinkerfin_messaging_threads",
    metadata,
    _channel_id(),
    _thread_id(),
    Column(
        "thread",
        _TEXT,
        nullable=False,
        comment="Complete ThreadIdentity JSON with namespace and threadId",
    ),
    Column(
        "current_generation",
        BigInteger,
        nullable=False,
        comment="Latest allocated generation, including a terminal tombstone",
    ),
    comment="Current generation of each namespace-scoped channel thread",
    mysql_engine="InnoDB",
)

generations = Table(
    "tinkerfin_messaging_generations",
    metadata,
    _channel_id(),
    _thread_id(),
    _generation(),
    Column(
        "disposition",
        String(16),
        nullable=False,
        comment="active, deleting, deleted, expiring or expired",
    ),
    Column(
        "latest_sequence",
        BigInteger,
        nullable=False,
        comment="Greatest committed message sequence, or zero",
    ),
    Column(
        "payload_bytes",
        BigInteger,
        nullable=False,
        comment="Committed message payload bytes in this generation",
    ),
    Column(
        "next_producer_fence",
        BigInteger,
        nullable=False,
        comment="Positive fence for the next producer owner",
    ),
    Column(
        "active_run_id",
        _TEXT,
        nullable=True,
        comment="ASCII JSON string of the full current producer run ID; null when inactive",
    ),
    Column(
        "retention_deadline",
        _TIME,
        nullable=True,
        comment="Database UTC expiry time after settlement; null during production",
    ),
    Column(
        "message_sequence",
        BigInteger,
        nullable=False,
        comment="Committed message change cursor",
    ),
    Column(
        "control_sequence",
        BigInteger,
        nullable=False,
        comment="Monotonic producer and cleanup change cursor",
    ),
    comment="Generation metadata, indexed terminal retention and permanent unavailability evidence",
    mysql_engine="InnoDB",
)
Index("ix_tfmsg_generations_expiry", generations.c.retention_deadline)
Index("ix_tfmsg_generations_cleanup", generations.c.disposition)

runs = Table(
    "tinkerfin_messaging_runs",
    metadata,
    _channel_id(),
    _thread_id(),
    _generation(),
    Column(
        "run_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the full semantic run identifier",
    ),
    Column(
        "identity",
        _TEXT,
        nullable=False,
        comment="Complete RunIdentity JSON, verified against the channel thread and run index",
    ),
    Column(
        "start_sequence",
        BigInteger,
        nullable=False,
        comment="Thread tail observed before this run started",
    ),
    Column(
        "request_digest",
        String(64),
        nullable=True,
        comment="Immutable lowercase SHA-256 command binding; NULL identifies an ordinary object stream",
    ),
    Column(
        "end_sequence",
        BigInteger,
        nullable=False,
        comment="Greatest sequence committed by this run",
    ),
    Column(
        "status",
        String(24),
        nullable=False,
        comment="running, cancel_requested, completed, cancelled, failed or owner_lost",
    ),
    Column(
        "settlement_started",
        Boolean,
        nullable=False,
        comment="Whether the producer claimed terminal settlement",
    ),
    Column(
        "cancellable",
        Boolean,
        nullable=False,
        comment="Whether the source accepts a cancellation request",
    ),
    Column(
        "recoverable",
        Boolean,
        nullable=False,
        comment="Whether a lost producer can resume from its checkpoint",
    ),
    Column(
        "producer_token",
        _TEXT,
        nullable=True,
        comment="ASCII JSON string of the opaque producer token; null after settlement",
    ),
    Column(
        "producer_fence",
        BigInteger,
        nullable=False,
        comment="Monotonic producer fence retained after ownership loss",
    ),
    Column(
        "lease_deadline",
        _TIME,
        nullable=True,
        comment="Database UTC producer expiry time, null after ownership release",
    ),
    Column(
        "publication_closed",
        Boolean,
        nullable=False,
        comment="Whether a source terminal message forbids further publication",
    ),
    Column(
        "publication_ready",
        Boolean,
        nullable=False,
        comment="Whether the required source start permits external publication",
    ),
    Column(
        "checkpoint_position",
        _BYTES,
        nullable=True,
        comment="Opaque latest recovery position, committed with its message",
    ),
    Column(
        "checkpoint_message_id",
        _TEXT,
        nullable=True,
        comment="ASCII JSON string of the message ID associated with the latest recovery position",
    ),
    Column(
        "failure_class",
        _TEXT,
        nullable=False,
        comment="ASCII JSON string of the bounded trusted failure class for remote diagnostics",
    ),
    Column(
        "failure_message",
        _TEXT,
        nullable=False,
        comment="ASCII JSON string of bounded trusted failure detail; not client-safe output",
    ),
    comment="Bounded producer state and latest checkpoint for each exact generation and run",
    mysql_engine="InnoDB",
)

messages = Table(
    "tinkerfin_messaging_messages",
    metadata,
    _channel_id(),
    _thread_id(),
    _generation(),
    Column(
        "sequence",
        BigInteger,
        primary_key=True,
        comment="One-based ascending sequence within this generation",
    ),
    Column(
        "message_id_hash",
        _HASH,
        nullable=False,
        comment="SHA-256 index of the full idempotency key",
    ),
    Column(
        "message_id",
        _TEXT,
        nullable=False,
        comment="ASCII JSON string of the full idempotency key, checked with its hash and evidence",
    ),
    Column(
        "identity",
        _TEXT,
        nullable=False,
        comment="Complete namespace, thread and source run identity",
    ),
    Column(
        "codec",
        _TEXT,
        nullable=False,
        comment="ASCII JSON string of the stable codec identifier matching the channel",
    ),
    Column("payload", _BYTES, nullable=False, comment="Encoded message payload bytes"),
    Column(
        "created_at",
        _TIME,
        nullable=False,
        comment="Database UTC time allocated by the committed transition",
    ),
    Column(
        "signature",
        String(64),
        nullable=False,
        comment="SHA-256 of source run, codec, payload and checkpoint evidence within this thread",
    ),
    Column(
        "checkpoint_position",
        _BYTES,
        nullable=True,
        comment="Opaque recovery position atomically committed with this message",
    ),
    Column(
        "checkpoint_message_id",
        _TEXT,
        nullable=True,
        comment="ASCII JSON string of the message identifier retained with this recovery position",
    ),
    comment="Individual ordered messages and immutable complete idempotency evidence",
    mysql_engine="InnoDB",
)
Index(
    "ix_tfmsg_messages_identity",
    messages.c.channel_id,
    messages.c.thread_id,
    messages.c.generation,
    messages.c.message_id_hash,
    unique=True,
)


def prepare_schema(connection: Connection) -> None:
    """Create empty Messaging tables or reject a different existing structure."""

    inspector = inspect(connection)
    owned = {
        name
        for name in inspector.get_table_names()
        if name.startswith("tinkerfin_messaging_")
    }
    if not owned:
        metadata.create_all(connection)
        inspector = inspect(connection)
    elif owned != set(metadata.tables):
        raise MessagingBackendProtocolError(
            "Messaging SQL tables do not match the required structure"
        )
    for table in metadata.sorted_tables:
        reflected = {
            column["name"]: column for column in inspector.get_columns(table.name)
        }
        if set(reflected) != set(table.c.keys()):
            raise MessagingBackendProtocolError(
                "Messaging SQL columns do not match the required structure"
            )
        for column in table.c:
            actual = reflected[column.name]
            expected_type = str(column.type.compile(dialect=connection.dialect)).upper()
            actual_type = str(
                actual["type"].compile(dialect=connection.dialect)
            ).upper()
            # MySQL reflects BOOLEAN as TINYINT(1), its actual storage type.
            if expected_type == "BOOL" and connection.dialect.name == "mysql":
                expected_type = "TINYINT(1)"
            if (
                actual_type != expected_type
                or bool(actual["nullable"]) != column.nullable
                or actual.get("default") is not None
            ):
                raise MessagingBackendProtocolError(
                    "Messaging SQL column type, nullability or default is invalid"
                )
            if (
                connection.dialect.name != "sqlite"
                and actual.get("comment") != column.comment
            ):
                raise MessagingBackendProtocolError(
                    "Messaging SQL column comments do not match the required structure"
                )
        if tuple(
            inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
        ) != tuple(column.name for column in table.primary_key):
            raise MessagingBackendProtocolError("Messaging SQL primary key is invalid")
        actual_indexes = {
            item["name"]: (tuple(item["column_names"]), bool(item["unique"]))
            for item in inspector.get_indexes(table.name)
        }
        expected_indexes: dict[str | None, tuple[tuple[str, ...], bool]] = {
            index.name: (
                tuple(column.name for column in index.columns),
                bool(index.unique),
            )
            for index in table.indexes
        }
        unique_constraints = inspector.get_unique_constraints(table.name)
        # MySQLDialect.get_unique_constraints reflects each UNIQUE INDEX a second
        # time with duplicates_index. Accept only the exact already-validated index.
        extra_constraints = [
            constraint
            for constraint in unique_constraints
            if not (
                connection.dialect.name == "mysql"
                and constraint.get("duplicates_index") == constraint["name"]
                and expected_indexes.get(constraint["name"])
                == (
                    tuple(constraint["column_names"]),
                    True,
                )
            )
        ]
        if actual_indexes != expected_indexes or extra_constraints:
            raise MessagingBackendProtocolError(
                "Messaging SQL indexes or unique constraints are invalid"
            )
        if inspector.get_foreign_keys(table.name) or inspector.get_check_constraints(
            table.name
        ):
            raise MessagingBackendProtocolError(
                "Messaging SQL tables contain unexpected constraints"
            )
        if (
            connection.dialect.name != "sqlite"
            and inspector.get_table_comment(table.name).get("text") != table.comment
        ):
            raise MessagingBackendProtocolError(
                "Messaging SQL table comments do not match the required structure"
            )
        if (
            connection.dialect.name == "mysql"
            and inspector.get_table_options(table.name).get("mysql_engine", "").lower()
            != "innodb"
        ):
            raise MessagingBackendProtocolError(
                "Messaging SQL tables require InnoDB storage"
            )
