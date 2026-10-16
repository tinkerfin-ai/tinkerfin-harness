"""Transactional availability intent and durable holder acknowledgement operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import case, delete, insert, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from ..errors import OpenSandboxStateError, OpenSandboxStateOwnershipError
from ._sql_fencing import current_claim_time
from ._sql_schema import _availability, _holders, _owners
from ._sql_transactions import _read_rows
from .availability import (
    OpenSandboxAvailability,
    OpenSandboxAvailabilityPhase,
    OpenSandboxHolderUpdate,
    _next_availability,
    _validate_holder_id,
)
from .state import OpenSandboxOwnerClaim, _owner_digest

if TYPE_CHECKING:
    from .sqlalchemy import SQLAlchemyOpenSandboxState


def _snapshot(row: RowMapping) -> OpenSandboxAvailability:
    phase = str(row["phase"])
    if phase not in {
        "running",
        "draining",
        "pausing",
        "paused",
        "resuming",
        "uncertain",
    }:
        raise OpenSandboxStateError("Stored Sandbox availability phase is invalid")
    return OpenSandboxAvailability(
        owner_digest=str(row["owner_digest"]),
        sandbox_id=str(row["sandbox_id"]),
        binding_generation=int(row["binding_generation"]),
        sequence=int(row["sequence"]),
        phase=cast(OpenSandboxAvailabilityPhase, phase),
        connection_generation=int(row["connection_generation"]),
    )


def _expected_conditions(
    self: SQLAlchemyOpenSandboxState, expected: OpenSandboxAvailability
) -> tuple[ColumnElement[bool], ...]:
    return (
        _availability.c.namespace == self._namespace,
        _availability.c.owner_digest == expected.owner_digest,
        _availability.c.sandbox_id == expected.sandbox_id,
        _availability.c.binding_generation == expected.binding_generation,
        _availability.c.sequence == expected.sequence,
        _availability.c.phase == expected.phase,
        _availability.c.connection_generation == expected.connection_generation,
    )


def _holder_binding_conditions(
    self: SQLAlchemyOpenSandboxState, expected: OpenSandboxAvailability
) -> tuple[ColumnElement[bool], ...]:
    return (
        _holders.c.namespace == self._namespace,
        _holders.c.owner_digest == expected.owner_digest,
        _holders.c.sandbox_id == expected.sandbox_id,
        _holders.c.binding_generation == expected.binding_generation,
    )


async def _check_claim(
    self: SQLAlchemyOpenSandboxState,
    connection: AsyncConnection,
    claim: OpenSandboxOwnerClaim,
) -> None:
    if await current_claim_time(self, connection, claim) is None:
        raise OpenSandboxStateOwnershipError("Owner claim is no longer current")


async def _read_locked(
    self: SQLAlchemyOpenSandboxState,
    connection: AsyncConnection,
    owner_digest: str,
) -> OpenSandboxAvailability | None:
    statement = select(_availability).where(
        _availability.c.namespace == self._namespace,
        _availability.c.owner_digest == owner_digest,
    )
    if self._require_capabilities().row_locks:
        statement = statement.with_for_update()
    row = (await connection.execute(statement)).mappings().one_or_none()
    return None if row is None else _snapshot(row)


async def initialize_binding(
    self: SQLAlchemyOpenSandboxState,
    connection: AsyncConnection,
    claim: OpenSandboxOwnerClaim,
    sandbox_id: str,
) -> None:
    """Publish running intent inside the transaction that commits its binding."""
    # Owner fencing already serializes changes to this binding. A locking read
    # of a missing availability row would add an InnoDB gap lock and can deadlock
    # unrelated owner inserts under REPEATABLE READ. One primary-key upsert avoids
    # that gap read while retaining an existing binding's exact current intent.
    same_binding = (_availability.c.sandbox_id == sandbox_id) & (
        _availability.c.binding_generation == claim.generation
    )
    retained_sequence = case((same_binding, _availability.c.sequence), else_=0)
    retained_phase = case((same_binding, _availability.c.phase), else_="running")
    retained_connection = case(
        (same_binding, _availability.c.connection_generation), else_=0
    )
    if self._dialect == "mysql":
        statement = mysql_insert(_availability).values(
            namespace=self._namespace,
            owner_digest=claim.owner_digest,
            sandbox_id=sandbox_id,
            binding_generation=claim.generation,
            sequence=0,
            phase="running",
            connection_generation=0,
        )
        # MySQL evaluates assignments in order: inspect the old identity before
        # replacing either binding identity column.
        await connection.execute(
            statement.on_duplicate_key_update(
                [
                    ("sequence", retained_sequence),
                    ("phase", retained_phase),
                    ("connection_generation", retained_connection),
                    ("sandbox_id", sandbox_id),
                    ("binding_generation", claim.generation),
                ]
            )
        )
    else:
        insert_row = (
            postgresql_insert if self._dialect == "postgresql" else sqlite_insert
        )
        statement = insert_row(_availability).values(
            namespace=self._namespace,
            owner_digest=claim.owner_digest,
            sandbox_id=sandbox_id,
            binding_generation=claim.generation,
            sequence=0,
            phase="running",
            connection_generation=0,
        )
        await connection.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    _availability.c.namespace,
                    _availability.c.owner_digest,
                ],
                set_={
                    "sequence": retained_sequence,
                    "phase": retained_phase,
                    "connection_generation": retained_connection,
                    "sandbox_id": sandbox_id,
                    "binding_generation": claim.generation,
                },
            )
        )


async def remove_binding(
    self: SQLAlchemyOpenSandboxState,
    connection: AsyncConnection,
    claim: OpenSandboxOwnerClaim,
) -> None:
    """Retire all holder generations when this owner's binding is removed."""
    current = await _read_locked(self, connection, claim.owner_digest)
    await connection.execute(
        delete(_holders).where(
            _holders.c.namespace == self._namespace,
            _holders.c.owner_digest == claim.owner_digest,
        )
    )
    if current is not None:
        await connection.execute(
            delete(_availability).where(*_expected_conditions(self, current))
        )


async def register_holder(
    self: SQLAlchemyOpenSandboxState,
    claim: OpenSandboxOwnerClaim,
    holder_id: str,
) -> OpenSandboxAvailability:
    """Serialize handle publication against the intent that closes admission."""
    self._ensure_open()
    _validate_holder_id(holder_id)

    async def register(connection: AsyncConnection) -> OpenSandboxAvailability:
        await _check_claim(self, connection, claim)
        current = await _read_locked(self, connection, claim.owner_digest)
        if current is None or current.phase != "running":
            raise OpenSandboxStateOwnershipError("Sandbox binding is not running")
        await connection.execute(
            delete(_holders).where(
                _holders.c.namespace == self._namespace,
                _holders.c.holder_id == holder_id,
                _holders.c.owner_digest == current.owner_digest,
            )
        )
        await connection.execute(
            insert(_holders).values(
                namespace=self._namespace,
                holder_id=holder_id,
                owner_digest=current.owner_digest,
                sandbox_id=current.sandbox_id,
                binding_generation=current.binding_generation,
                acknowledged_sequence=None,
            )
        )
        return current

    return await self._run_write_transaction(register)


async def read_availability(
    self: SQLAlchemyOpenSandboxState, owner_key: str
) -> OpenSandboxAvailability | None:
    """Read committed availability even while a remote transition owns a claim."""
    self._ensure_open()
    rows = await _read_rows(
        self,
        select(
            _availability,
            _owners.c.sandbox_id.label("bound_sandbox_id"),
            _owners.c.binding_generation.label("bound_generation"),
        )
        .select_from(
            _owners.outerjoin(
                _availability,
                (_owners.c.namespace == _availability.c.namespace)
                & (_owners.c.owner_digest == _availability.c.owner_digest),
            )
        )
        .where(
            _owners.c.namespace == self._namespace,
            _owners.c.owner_digest == _owner_digest(self._namespace, owner_key),
            _owners.c.sandbox_id.is_not(None),
        ),
    )
    row = rows[0] if rows else None
    if row is None:
        return None
    if (
        row["phase"] is None
        or row["sandbox_id"] != row["bound_sandbox_id"]
        or row["binding_generation"] != row["bound_generation"]
    ):
        raise OpenSandboxStateError("Sandbox binding has inconsistent availability")
    return _snapshot(row)


async def get_holder_updates(
    self: SQLAlchemyOpenSandboxState, holder_id: str
) -> tuple[OpenSandboxHolderUpdate, ...]:
    """Poll all of one manager's handles with one read-only database statement."""
    self._ensure_open()
    statement = (
        select(
            _availability,
            _holders.c.holder_id,
            _holders.c.sandbox_id.label("holder_sandbox_id"),
            _holders.c.binding_generation.label("holder_binding_generation"),
            _holders.c.acknowledged_sequence,
        )
        .select_from(
            _holders.join(
                _availability,
                (_holders.c.namespace == _availability.c.namespace)
                & (_holders.c.owner_digest == _availability.c.owner_digest),
            )
        )
        .where(
            _holders.c.namespace == self._namespace, _holders.c.holder_id == holder_id
        )
        .order_by(_holders.c.owner_digest)
    )
    rows = await _read_rows(self, statement)
    return tuple(
        OpenSandboxHolderUpdate(
            holder_id=str(row["holder_id"]),
            owner_digest=str(row["owner_digest"]),
            sandbox_id=str(row["holder_sandbox_id"]),
            binding_generation=int(row["holder_binding_generation"]),
            acknowledged_sequence=(
                None
                if row["acknowledged_sequence"] is None
                else int(row["acknowledged_sequence"])
            ),
            availability=_snapshot(row),
        )
        for row in rows
    )


async def _all_holders_idle(
    self: SQLAlchemyOpenSandboxState,
    connection: AsyncConnection,
    expected: OpenSandboxAvailability,
) -> bool:
    # Hold the availability row lock before this current read. Registration and
    # ACKs use that same lock, so dispatch cannot race an unseen holder or ACK.
    statement = (
        select(_holders.c.holder_id)
        .where(
            *_holder_binding_conditions(self, expected),
            or_(
                _holders.c.acknowledged_sequence.is_(None),
                _holders.c.acknowledged_sequence != expected.sequence,
            ),
        )
        .limit(1)
    )
    if self._require_capabilities().row_locks:
        statement = statement.with_for_update()
    return (await connection.execute(statement)).one_or_none() is None


async def change_availability(
    self: SQLAlchemyOpenSandboxState,
    claim: OpenSandboxOwnerClaim,
    expected: OpenSandboxAvailability,
    *,
    phase: OpenSandboxAvailabilityPhase,
    refresh_connection: bool = False,
) -> OpenSandboxAvailability:
    """Commit one exact fenced intent; never await remote I/O inside the transaction."""
    self._ensure_open()

    async def change(connection: AsyncConnection) -> OpenSandboxAvailability:
        await _check_claim(self, connection, claim)
        current = await _read_locked(self, connection, claim.owner_digest)
        if current != expected:
            raise OpenSandboxStateOwnershipError(
                "Sandbox availability is no longer current"
            )
        updated = _next_availability(
            expected, phase=phase, refresh_connection=refresh_connection
        )
        if phase == "pausing" and not await _all_holders_idle(
            self, connection, expected
        ):
            raise OpenSandboxStateOwnershipError(
                "Sandbox holders have not all acknowledged idle"
            )
        result = await connection.execute(
            update(_availability)
            .where(*_expected_conditions(self, expected))
            .values(
                sequence=updated.sequence,
                phase=updated.phase,
                connection_generation=updated.connection_generation,
            )
        )
        if result.rowcount != 1:
            raise OpenSandboxStateOwnershipError(
                "Sandbox availability is no longer current"
            )
        return updated

    return await self._run_write_transaction(change)


async def acknowledge_idle(
    self: SQLAlchemyOpenSandboxState,
    holder_id: str,
    availability: OpenSandboxAvailability,
) -> bool:
    """Record exact drain evidence independently of the owner's lifecycle lease."""
    self._ensure_open()
    if availability.phase != "draining":
        return False

    async def acknowledge(connection: AsyncConnection) -> bool:
        current = await _read_locked(self, connection, availability.owner_digest)
        if current != availability:
            return False
        result = await connection.execute(
            update(_holders)
            .where(
                *_holder_binding_conditions(self, availability),
                _holders.c.holder_id == holder_id,
            )
            .values(acknowledged_sequence=availability.sequence)
        )
        return result.rowcount == 1

    return await self._run_write_transaction(acknowledge)


async def holders_are_idle(
    self: SQLAlchemyOpenSandboxState,
    claim: OpenSandboxOwnerClaim,
    availability: OpenSandboxAvailability,
) -> bool:
    """Require explicit idle evidence from every holder, including lost workers."""
    self._ensure_open()

    async def check(connection: AsyncConnection) -> bool:
        await _check_claim(self, connection, claim)
        current = await _read_locked(self, connection, claim.owner_digest)
        if current != availability or availability.phase != "draining":
            raise OpenSandboxStateOwnershipError("Sandbox drain is no longer current")
        return await _all_holders_idle(self, connection, availability)

    return await self._run_write_transaction(check)


async def unregister_holder(
    self: SQLAlchemyOpenSandboxState,
    holder_id: str,
    availability: OpenSandboxAvailability,
) -> None:
    """Release only an exact binding registration after the caller proves local idle."""
    self._ensure_open()

    async def unregister(connection: AsyncConnection) -> None:
        await connection.execute(
            delete(_holders).where(
                *_holder_binding_conditions(self, availability),
                _holders.c.holder_id == holder_id,
            )
        )

    await self._run_write_transaction(unregister)
