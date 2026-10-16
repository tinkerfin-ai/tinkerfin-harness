"""Database capabilities and driver error facts without retry policy."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

SqlDialect = Literal["sqlite", "mysql", "postgresql"]


def engine_dialect(engine: AsyncEngine) -> SqlDialect:
    """Validate an async Engine and identify its SQL dialect without database I/O.

    Args:
        engine: Caller-owned asynchronous SQLAlchemy Engine.

    Returns:
        The supported SQL dialect; no driver or server connection is opened.

    Raises:
        TypeError: engine is not an AsyncEngine.
        ValueError: The Engine uses an unsupported SQL dialect.
    """

    if not isinstance(engine, AsyncEngine):
        raise TypeError("engine must be an AsyncEngine")
    name = engine.dialect.name
    if name == "sqlite" or name == "mysql" or name == "postgresql":
        return name
    raise ValueError("Supported SQL dialects are SQLite, MySQL, and PostgreSQL")


@dataclass(frozen=True, slots=True)
class DatabaseCapabilities:
    """Locking capabilities of a connected database server.

    Attributes:
        dialect: Database family selected by the borrowed Engine.
        server_version: Actual initialized SQLAlchemy server version.
        row_locks: Whether SELECT FOR UPDATE acquires row locks.
        skip_locked: Whether locked rows can be skipped when claiming work.
    """

    dialect: SqlDialect
    server_version: tuple[int, ...]
    row_locks: bool
    skip_locked: bool


def database_capabilities(connection: AsyncConnection) -> DatabaseCapabilities:
    """Read initialized server features from an already opened connection.

    Args:
        connection: Live connection whose dialect completed server initialization.

    Returns:
        Verified locking features; SQLite never advertises row locking.

    Raises:
        ValueError: Server version or database family is unsupported.
    """

    dialect = connection.dialect
    name = engine_dialect(connection.engine)
    version = dialect.server_version_info
    if not version or not all(isinstance(part, int) for part in version):
        raise ValueError("The database server version is unavailable")
    if name == "mysql" and bool(getattr(dialect, "is_mariadb", False)):
        raise ValueError("MariaDB is not a supported SQL database")
    if name == "mysql" and version < (5, 7):
        raise ValueError("MySQL 5.7 or newer is required")
    if name == "postgresql" and version < (9, 5):
        raise ValueError("PostgreSQL 9.5 or newer is required")
    return DatabaseCapabilities(
        dialect=name,
        server_version=tuple(version),
        row_locks=name != "sqlite",
        skip_locked=name == "postgresql" or (name == "mysql" and version >= (8,)),
    )


def sqlite_lock_error(error: BaseException) -> bool:
    """Identify SQLite BUSY/LOCKED, including extended codes, without text matching."""

    original = error.orig if isinstance(error, DBAPIError) else error
    if not isinstance(original, sqlite3.Error):
        return False
    code = getattr(original, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in {5, 6}


def mysql_error_code(error: BaseException) -> int | None:
    """Return a numeric MySQL driver code without deciding whether to retry."""

    original = error.orig if isinstance(error, DBAPIError) else error
    args = () if original is None else original.args
    return args[0] if args and isinstance(args[0], int) else None


def postgresql_error_code(error: BaseException) -> str | None:
    """Return SQLSTATE from asyncpg or psycopg's SQLAlchemy driver boundary."""

    original = error.orig if isinstance(error, DBAPIError) else error
    code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return code if isinstance(code, str) else None
