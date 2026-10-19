"""Validate current MySQL storage through the public Messaging backend."""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.support.docker_services import owned_database

from tinkerfin_messaging import MessagingBackendProtocolError, SqlAlchemyBackend

pytestmark = pytest.mark.docker_integration


async def test_mysql_inherited_collation_is_accepted_and_changed_types_are_rejected(
    tmp_path: Path,
) -> None:
    async with owned_database(tmp_path, "mysql") as url:
        engine = create_async_engine(url)
        try:
            await SqlAlchemyBackend(engine).prepare_messaging_storage()
            async with engine.connect() as connection:
                inherited = await connection.scalar(
                    text(
                        "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() "
                        "AND TABLE_NAME = 'tinkerfin_messaging_generations' "
                        "AND COLUMN_NAME = 'disposition'"
                    )
                )
            assert inherited is not None
            await SqlAlchemyBackend(engine).prepare_messaging_storage()
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "ALTER TABLE tinkerfin_messaging_generations "
                    "MODIFY disposition VARCHAR(8) NOT NULL "
                    "COMMENT 'active, deleting, deleted, expiring or expired'"
                )
            with pytest.raises(MessagingBackendProtocolError):
                await SqlAlchemyBackend(engine).prepare_messaging_storage()
            async with engine.connect() as connection:
                unchanged = await connection.scalar(
                    text(
                        "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() "
                        "AND TABLE_NAME = 'tinkerfin_messaging_generations' "
                        "AND COLUMN_NAME = 'disposition'"
                    )
                )
            assert unchanged == 8
        finally:
            await engine.dispose()
