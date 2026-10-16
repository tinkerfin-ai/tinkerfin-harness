"""Stable diagnostic boundaries and factual driver classification."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from tinkerfin_sqlalchemy import (
    SqlTransactionError,
    mysql_error_code,
    postgresql_error_code,
    sqlite_lock_error,
)


def test_transaction_error_keeps_safe_context_and_original_cause() -> None:
    context: dict[str, str | int | bool | None] = {"scope": "schema"}
    cause = OSError("driver diagnostic")
    error = SqlTransactionError("schema operation failed", context=context, cause=cause)
    context["scope"] = "changed"
    assert isinstance(error, SQLAlchemyError)
    assert (
        error.code == "sqlalchemy.transaction"
        and str(error) == "schema operation failed"
    )
    assert error.context == {"scope": "schema"}
    assert error.cause is cause and error.__cause__ is cause


@pytest.mark.parametrize("code", [5, 6, 261, 262, 517, 773])
def test_sqlite_extended_lock_codes_remain_facts_without_a_retry_policy(
    code: int,
) -> None:
    original = sqlite3.OperationalError("diagnostic text is not classified")
    original.sqlite_errorcode = code
    error = OperationalError("query", None, original)
    assert sqlite_lock_error(error)
    assert not sqlite_lock_error(RuntimeError("database is locked"))


def test_mysql_and_postgresql_codes_do_not_depend_on_exception_text() -> None:
    class PostgresFailure(Exception):
        sqlstate = "40001"

    assert (
        postgresql_error_code(
            OperationalError("query", None, PostgresFailure("details"))
        )
        == "40001"
    )
    assert (
        mysql_error_code(OperationalError("query", None, RuntimeError(1213, "details")))
        == 1213
    )
    assert mysql_error_code(RuntimeError("1213")) is None
    assert postgresql_error_code(RuntimeError("40001")) is None
