"""Validate Sandbox lease ownership after acquiring the transaction's row lock."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import OpenSandboxStateError
from ._sql_schema import _cleanup, _owners, _warm_slots
from .state import (
    OpenSandboxCleanupClaim,
    OpenSandboxOwnerClaim,
    OpenSandboxReadyWarmClaim,
    OpenSandboxWarmClaim,
)

if TYPE_CHECKING:
    from .sqlalchemy import SQLAlchemyOpenSandboxState


async def current_claim_time(
    state: SQLAlchemyOpenSandboxState,
    connection: AsyncConnection,
    claim: OpenSandboxOwnerClaim
    | OpenSandboxWarmClaim
    | OpenSandboxReadyWarmClaim
    | OpenSandboxCleanupClaim,
) -> datetime | None:
    """Lock the claimed row, then check its current token, generation, and expiry.

    Sampling before a server row-lock wait can authorize an expired lease. SQLite
    already owns the database write lock through BEGIN IMMEDIATE; server databases
    acquire the row lock here. The caller retains it until its mutation commits.

    Args:
        state: State deployment and clock for the active transaction.
        connection: Connection owned by the caller's write transaction.
        claim: Exact lease whose authority is needed for the mutation.

    Returns:
        The time after row acquisition, or None if the lease is no longer current.

    Raises:
        OpenSandboxStateError: The stored lease expiry has an invalid type.
    """

    if isinstance(claim, OpenSandboxOwnerClaim):
        table = _owners
        key = table.c.owner_digest == claim.owner_digest
    elif isinstance(claim, OpenSandboxCleanupClaim):
        table = _cleanup
        key = table.c.sandbox_id == claim.sandbox_id
    else:
        table = _warm_slots
        key = table.c.slot == claim.slot
    statement = select(
        table.c.claim_token, table.c.generation, table.c.lease_expires_at
    ).where(table.c.namespace == state._namespace, key)
    if state._require_capabilities().row_locks:
        statement = statement.with_for_update()
    row = (await connection.execute(statement)).one_or_none()
    now = state._now()
    if (
        row is None
        or row.claim_token != claim.token
        or row.generation != claim.generation
    ):
        return None
    expiry = row.lease_expires_at
    if not isinstance(expiry, datetime) or expiry.tzinfo is not None:
        raise OpenSandboxStateError("A claimed Sandbox row has no valid lease expiry")
    return now if expiry > now else None
