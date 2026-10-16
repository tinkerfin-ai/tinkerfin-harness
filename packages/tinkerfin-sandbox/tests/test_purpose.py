"""Capability ownership across State, provider metadata, and raw lifecycle APIs."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_sandbox import (
    OpenSandboxPurpose,
    OpenSandboxPurposeError,
    SQLAlchemyOpenSandboxState,
)


@pytest.mark.parametrize("purpose", ["commands", "workspaces"])
async def test_purpose_survives_worker_restart_and_blocks_reclassification(
    sandbox_sql_engine: AsyncEngine, purpose: OpenSandboxPurpose
) -> None:
    first = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine, namespace="purpose")
    await first.start(warm_pool_size=1)
    owner = await first.acquire_owner("owner")
    try:
        original = await first.bind_owner(owner, "original", purpose=purpose)
    finally:
        await first.release_owner(owner)
        await first.aclose()

    successor = SQLAlchemyOpenSandboxState(
        engine=sandbox_sql_engine, namespace="purpose"
    )
    await successor.start(warm_pool_size=1)
    try:
        assert await successor.read_binding("owner") == original
        claim = await successor.acquire_owner("owner")
        try:
            assert claim.binding == original
            with pytest.raises(OpenSandboxPurposeError):
                await successor.bind_owner(
                    claim,
                    "different-capability",
                    purpose="commands" if purpose == "workspaces" else "workspaces",
                )
        finally:
            await successor.release_owner(claim)
        assert await successor.read_binding("owner") == original
    finally:
        await successor.aclose()


async def test_sql_owner_row_rejects_inconsistent_binding_purpose(
    sandbox_sql_engine: AsyncEngine,
) -> None:
    state = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine, namespace="purpose")
    await state.start(warm_pool_size=0)
    try:
        claim = await state.acquire_owner("owner")
        await state.release_owner(claim)
        async with sandbox_sql_engine.begin() as connection:
            row = (
                await connection.execute(
                    text("SELECT sandbox_id, purpose FROM tinkerfin_opensandbox_owners")
                )
            ).one()
            assert tuple(row) == (None, None)
        with pytest.raises(DBAPIError, match="ck_tinkerfin_opensandbox_owners_purpose"):
            async with sandbox_sql_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE tinkerfin_opensandbox_owners SET purpose = 'workspaces'"
                    )
                )
        owner = await state.acquire_owner("owner")
        try:
            await state.bind_owner(owner, "sandbox", purpose="workspaces")
        finally:
            await state.release_owner(owner)
        with pytest.raises(DBAPIError, match="ck_tinkerfin_opensandbox_owners_purpose"):
            async with sandbox_sql_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE tinkerfin_opensandbox_owners SET purpose = NULL")
                )
    finally:
        await state.aclose()
