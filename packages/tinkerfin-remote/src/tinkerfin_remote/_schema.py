"""Current durable operations and transport receipts for authenticated endpoints."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Index,
    MetaData,
    String,
    Table,
    Text,
    inspect,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import Connection

from .errors import RemoteError, RemoteErrorCode

metadata = MetaData()
_text = Text().with_variant(LONGTEXT(), "mysql")

scopes = Table(
    "tinkerfin_remote_scopes",
    metadata,
    Column(
        "scope_key",
        String(64),
        primary_key=True,
        comment="Hash of role, namespace and endpoint",
    ),
    Column(
        "identity",
        _text,
        nullable=False,
        comment="Exact endpoint identity and capacity settings",
    ),
    Column(
        "bytes",
        BigInteger,
        nullable=False,
        comment="Retained encoded payloads and compact receipt evidence bytes",
    ),
    Column(
        "operations",
        BigInteger,
        nullable=False,
        comment="Admitted operations without a terminal result",
    ),
    Column(
        "next_item",
        BigInteger,
        nullable=False,
        comment="Monotonic command admission order",
    ),
    Column(
        "revoked",
        Boolean,
        nullable=False,
        comment="Permanent authenticated endpoint revocation",
    ),
    Column(
        "reserved_bytes",
        BigInteger,
        nullable=False,
        comment="Terminal storage reserved by nonterminal operations",
    ),
    comment="Serialize capacity and receipts within one authenticated endpoint",
    mysql_engine="InnoDB",
)

operations = Table(
    "tinkerfin_remote_operations",
    metadata,
    Column(
        "reserved_bytes",
        BigInteger,
        nullable=False,
        comment="Bytes reserved for cancellation and terminal evidence",
    ),
    Column(
        "scope_key",
        String(64),
        primary_key=True,
        comment="Authenticated endpoint storage scope",
    ),
    Column(
        "operation_id",
        String(64),
        primary_key=True,
        comment="Stable invocation and substep identity",
    ),
    Column(
        "session_id",
        String(64),
        nullable=False,
        comment="Current authorized resource borrow for pending execution",
    ),
    Column(
        "request",
        _text,
        nullable=True,
        comment="Immutable operation JSON retained until acknowledged close; digest persists",
    ),
    Column(
        "digest",
        String(64),
        nullable=False,
        comment="SHA-256 of canonical operation request",
    ),
    Column(
        "state",
        String(16),
        nullable=False,
        comment="Pending, started or exact terminal outcome",
    ),
    Column(
        "retired",
        Boolean,
        nullable=False,
        comment="Acknowledged closed-session payloads were reclaimed",
    ),
    Column(
        "result_available",
        Boolean,
        nullable=False,
        comment="Complete result bytes remain replayable",
    ),
    Column(
        "last_sequence",
        BigInteger,
        nullable=False,
        comment="Last contiguous persisted operation event",
    ),
    Column(
        "cancelled",
        Boolean,
        nullable=False,
        comment="Durable cancellation intent, not proof of termination",
    ),
    Column(
        "output_bytes",
        BigInteger,
        nullable=False,
        comment="Encoded progress bytes retained for this operation",
    ),
    Column(
        "result_bytes",
        BigInteger,
        nullable=False,
        comment="Raw JSON result bytes transferred in chunks",
    ),
    Column(
        "binary_bytes",
        BigInteger,
        nullable=False,
        comment="Raw downloaded file bytes across all result files",
    ),
    comment="Concrete operations without models or Agent instructions",
    mysql_engine="InnoDB",
)

sessions = Table(
    "tinkerfin_remote_sessions",
    metadata,
    Column(
        "reserved_bytes",
        BigInteger,
        nullable=False,
        comment="Bytes reserved for close intent and confirmation",
    ),
    Column(
        "scope_key",
        String(64),
        primary_key=True,
        comment="Authenticated endpoint storage scope",
    ),
    Column(
        "session_id",
        String(64),
        primary_key=True,
        comment="Run-scoped workspace resource borrow",
    ),
    Column(
        "request",
        _text,
        nullable=False,
        comment="Exact workspace authorization and Run identity",
    ),
    Column(
        "digest",
        String(64),
        nullable=False,
        comment="SHA-256 of canonical workspace request",
    ),
    Column(
        "closed",
        Boolean,
        nullable=False,
        comment="Confirmed close request prevents new operation admission",
    ),
    Column(
        "settled",
        Boolean,
        nullable=False,
        comment="Worker confirmed workspace resources released",
    ),
    Column(
        "compacted",
        Boolean,
        nullable=False,
        comment="Acknowledged closed-session payloads were reclaimed",
    ),
    comment="Workspace borrows distinct from persistent authorization scope",
    mysql_engine="InnoDB",
)

items = Table(
    "tinkerfin_remote_items",
    metadata,
    Column(
        "scope_key",
        String(64),
        primary_key=True,
        comment="Authenticated endpoint storage scope",
    ),
    Column(
        "kind",
        String(16),
        primary_key=True,
        comment="Command, chunk, workspace reply or operation event",
    ),
    Column(
        "item_key", String(256), primary_key=True, comment="Exact immutable receipt key"
    ),
    Column(
        "group_id", String(64), nullable=False, comment="Owning operation or session"
    ),
    Column(
        "sequence",
        BigInteger,
        nullable=False,
        comment="Operation event sequence or binary offset",
    ),
    Column(
        "payload",
        _text,
        nullable=True,
        comment="Canonical JSON; null retains only acknowledged immutable receipt evidence",
    ),
    Column(
        "digest", String(64), nullable=False, comment="SHA-256 checked by both peers"
    ),
    Column(
        "acknowledged",
        Boolean,
        nullable=False,
        comment="Peer confirmed durable receipt",
    ),
    comment="Bounded immutable transport evidence; acknowledgments never replace results",
    mysql_engine="InnoDB",
)
Index("ix_tfremote_pending", items.c.scope_key, items.c.kind, items.c.acknowledged)
Index(
    "ix_tfremote_compact",
    sessions.c.scope_key,
    sessions.c.closed,
    sessions.c.settled,
    sessions.c.compacted,
)
Index(
    "ix_tfremote_events",
    items.c.scope_key,
    items.c.kind,
    items.c.group_id,
    items.c.sequence,
)
Index(
    "ix_tfremote_operations",
    operations.c.scope_key,
    operations.c.state,
    operations.c.session_id,
)

transfers = Table(
    "tinkerfin_remote_transfers",
    metadata,
    Column(
        "scope_key",
        String(64),
        primary_key=True,
        comment="Authenticated endpoint storage scope",
    ),
    Column("operation_id", String(64), primary_key=True, comment="Owning operation"),
    Column(
        "channel",
        String(16),
        primary_key=True,
        comment="File bytes or encoded JSON result",
    ),
    Column(
        "file_index",
        BigInteger,
        primary_key=True,
        comment="Position in the operation's file list",
    ),
    Column(
        "bytes", BigInteger, nullable=False, comment="Contiguous received raw bytes"
    ),
    comment="Bound contiguous binary results without loading earlier chunks",
    mysql_engine="InnoDB",
)


def prepare_schema(connection: Connection) -> None:
    metadata.create_all(connection)
    inspector = inspect(connection)
    for table in metadata.sorted_tables:
        if {column["name"] for column in inspector.get_columns(table.name)} != set(
            table.c.keys()
        ):
            raise RemoteError(
                RemoteErrorCode.PROTOCOL,
                f"Current remote storage schema is required: {table.name}",
            )
