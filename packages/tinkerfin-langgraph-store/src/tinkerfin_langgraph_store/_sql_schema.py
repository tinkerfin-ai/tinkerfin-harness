"""Document and query indexes owned by the SQL LangGraph Store."""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Double,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Table,
    Text,
    inspect,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Connection

from .errors import StoreSchemaError

# Binary identities retain full sorting keys. MySQL 8.4 filesort limits PAD SPACE
# text collations by max_sort_length; binary strings use NO PAD and retain the
# complete value. See the long-identity Store contract tests on real MySQL.
_SORT_KEY = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
_HASH = LargeBinary(32).with_variant(mysql.BINARY(32), "mysql")
_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")
_TIME = DateTime(timezone=False).with_variant(mysql.DATETIME(fsp=6), "mysql")
metadata = MetaData()

namespaces = Table(
    "tinkerfin_store_namespaces",
    metadata,
    Column(
        "namespace_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the complete encoded namespace",
    ),
    Column(
        "namespace",
        _SORT_KEY,
        nullable=False,
        comment="Dot-separated UTF-8 hex labels in tuple sort order",
    ),
    Column(
        "depth",
        Integer,
        nullable=False,
        comment="Number of labels in the complete namespace",
    ),
    comment="Document namespace identities and per-namespace write exclusion",
    mysql_engine="InnoDB",
)

paths = Table(
    "tinkerfin_store_paths",
    metadata,
    Column(
        "namespace_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the complete encoded namespace",
    ),
    Column(
        "depth",
        Integer,
        primary_key=True,
        comment="Prefix label count; zero represents the empty tuple",
    ),
    Column(
        "prefix",
        _SORT_KEY,
        nullable=False,
        comment="Complete encoded prefix at this depth, used for listing",
    ),
    Column(
        "label",
        _TEXT,
        nullable=False,
        comment="UTF-8 hex label at this depth; empty at depth zero",
    ),
    comment="Complete-label namespace matching and bounded-depth listing",
    mysql_engine="InnoDB",
)

documents = Table(
    "tinkerfin_store_documents",
    metadata,
    Column(
        "namespace_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the complete encoded namespace",
    ),
    Column(
        "key_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the complete document key",
    ),
    Column(
        "key",
        _SORT_KEY,
        nullable=False,
        comment="UTF-8 hex document key, retaining case and trailing spaces",
    ),
    Column(
        "value",
        _TEXT,
        nullable=False,
        comment="Finite JSON object preserving integer precision and Unicode",
    ),
    Column(
        "created_at",
        _TIME,
        nullable=False,
        comment="Database UTC time of the first insertion",
    ),
    Column(
        "updated_at",
        _TIME,
        nullable=False,
        comment="Database UTC time of the latest committed update",
    ),
    comment="Atomic LangGraph memory documents",
    mysql_engine="InnoDB",
)
Index(
    "ix_tinkerfin_store_documents_updated",
    documents.c.namespace_id,
    documents.c.updated_at,
)

fields = Table(
    "tinkerfin_store_fields",
    metadata,
    Column(
        "namespace_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the complete encoded namespace",
    ),
    Column(
        "key_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the complete document key",
    ),
    Column(
        "field_id",
        _HASH,
        primary_key=True,
        comment="SHA-256 index of the literal top-level field name",
    ),
    Column(
        "field",
        _TEXT,
        nullable=False,
        comment="UTF-8 hex field name; dots and dollar signs stay literal",
    ),
    Column(
        "value_id",
        _HASH,
        nullable=False,
        comment="SHA-256 index of the exact typed equality representation",
    ),
    Column(
        "value",
        _TEXT,
        nullable=False,
        comment="ASCII hex typed equality representation, verified with its digest",
    ),
    Column(
        "number_range",
        SmallInteger,
        nullable=True,
        comment="Numeric range: -1 below finite binary64, 0 finite, 1 above; null for nonnumbers",
    ),
    Column(
        "number",
        Double[float](asdecimal=False),
        nullable=True,
        comment="Finite binary64 comparison value when number_range is zero",
    ),
    comment="Portable exact-value and numeric filters for top-level JSON fields",
    mysql_engine="InnoDB",
)
Index(
    "ix_tinkerfin_store_fields_equality",
    fields.c.field_id,
    fields.c.value_id,
    fields.c.namespace_id,
)


def prepare_schema(connection: Connection) -> None:
    """Create a wholly empty Store or validate its complete current structure."""

    inspector = inspect(connection)
    owned = {
        name
        for name in inspector.get_table_names()
        if name.startswith("tinkerfin_store_")
    }
    expected = set(metadata.tables)
    if not owned:
        metadata.create_all(connection)
        inspector = inspect(connection)
    elif owned != expected:
        raise StoreSchemaError(
            "Store tables are incomplete or contain unknown structures"
        )
    for table in metadata.sorted_tables:
        reflected = {
            column["name"]: column for column in inspector.get_columns(table.name)
        }
        if set(reflected) != set(table.c.keys()):
            raise StoreSchemaError(
                "Store table columns do not match the required structure",
                context={"table": table.name},
            )
        for column in table.c:
            actual = reflected[column.name]
            required_type = str(column.type.compile(dialect=connection.dialect)).upper()
            actual_type = str(
                actual["type"].compile(dialect=connection.dialect)
            ).upper()
            if (
                actual_type != required_type
                or bool(actual["nullable"]) != column.nullable
                or actual.get("default") is not None
            ):
                raise StoreSchemaError(
                    "Store column type, nullability, or default is invalid",
                    context={"table": table.name, "column": column.name},
                )
            if (
                connection.dialect.name != "sqlite"
                and actual.get("comment") != column.comment
            ):
                raise StoreSchemaError(
                    "Store column comments do not match the required structure",
                    context={"table": table.name, "column": column.name},
                )
        if tuple(
            inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
        ) != tuple(column.name for column in table.primary_key):
            raise StoreSchemaError(
                "Store primary key is invalid", context={"table": table.name}
            )
        actual_indexes = {
            item["name"]: (tuple(item["column_names"]), bool(item["unique"]))
            for item in inspector.get_indexes(table.name)
        }
        expected_indexes = {
            index.name: (
                tuple(column.name for column in index.columns),
                bool(index.unique),
            )
            for index in table.indexes
        }
        if actual_indexes != expected_indexes or inspector.get_unique_constraints(
            table.name
        ):
            raise StoreSchemaError(
                "Store indexes or unique constraints are invalid",
                context={"table": table.name},
            )
        if inspector.get_foreign_keys(table.name) or inspector.get_check_constraints(
            table.name
        ):
            raise StoreSchemaError(
                "Store tables contain unexpected constraints",
                context={"table": table.name},
            )
        if (
            connection.dialect.name != "sqlite"
            and inspector.get_table_comment(table.name).get("text") != table.comment
        ):
            raise StoreSchemaError(
                "Store table comments do not match the required structure",
                context={"table": table.name},
            )
        if (
            connection.dialect.name == "mysql"
            and inspector.get_table_options(table.name).get("mysql_engine", "").lower()
            != "innodb"
        ):
            raise StoreSchemaError(
                "Store tables require transactional InnoDB storage",
                context={"table": table.name},
            )
