"""Persist exact operation identities, ordered output, and durable peer receipts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal, TypeVar

from pydantic import BaseModel
from sqlalchemy import Table, delete, func, insert, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from tinkerfin_contracts.identity import validate_namespace
from tinkerfin_sqlalchemy import SqlTransaction, SqlTransactionError

from ._schema import items, operations, prepare_schema, scopes, sessions, transfers
from ._tasks import owned_io, settle
from ._wire import chunk_bytes, digest, encode, item_key, key
from .errors import RemoteError, RemoteErrorCode
from .limits import RemoteLimits
from .models import (
    TERMINAL_EVENTS,
    BinaryChunk,
    CancelOperation,
    CloseWorkspace,
    CommandEnvelope,
    Dispatch,
    OpenWorkspace,
    OperationEvent,
    OperationRequest,
    Receipt,
    ResolvePath,
    UploadFiles,
    WorkspaceEvent,
    WorkspaceRequest,
)

ValueT = TypeVar("ValueT", bound=BaseModel)
ItemKind = Literal["command", "chunk", "event", "workspace"]


def text_field(row: RowMapping, field: str) -> str:
    value: object = row[field]
    if not isinstance(value, str):
        raise RemoteError(RemoteErrorCode.PROTOCOL, "Remote storage text is invalid")
    return value


def number_field(row: RowMapping, field: str) -> int:
    value: object = row[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise RemoteError(RemoteErrorCode.PROTOCOL, "Remote storage count is invalid")
    return value


def operation_key(request: OperationRequest) -> str:
    return key(
        request.workspace_scope_id,
        request.origin.model_dump(mode="json"),
        request.invocation_id,
        request.steps,
        request.observation_session,
    )


def workspace_key(request: WorkspaceRequest) -> str:
    return key(
        request.identity.namespace,
        request.identity.thread_id,
        request.target,
        request.authorization,
    )


class Ledger:
    """Serialize one role's bounded endpoint records using borrowed SQL storage."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        role: Literal["coordinator", "worker"],
        namespace: str,
        target: str,
        limits: RemoteLimits,
    ) -> None:
        SqlTransaction(engine)
        self.engine = engine
        self.namespace = validate_namespace(namespace)
        if not target or target != target.strip() or "\0" in target:
            raise ValueError("target must be nonempty canonical text")
        self.target = target
        self.limits = limits
        self.scope = key(role, namespace, target)
        self._identity = json.dumps(
            [role, namespace, target, asdict(limits)], sort_keys=True
        )
        self._io_tasks: set[asyncio.Task[object]] = set()
        self._io_closed = False

    async def aclose(self) -> None:
        """Reject new storage calls and join already accepted SQL operations."""
        self._io_closed = True
        await self.wait_idle()

    async def wait_idle(self) -> None:
        """Join storage operations already admitted by this endpoint."""
        await settle(asyncio.gather(*self._io_tasks, return_exceptions=True))

    @owned_io
    async def prepare(self) -> None:
        async with self._database(schema_lock="tinkerfin-remote") as conn:
            await conn.run_sync(prepare_schema)
            row = (
                (
                    await conn.execute(
                        select(scopes).where(scopes.c.scope_key == self.scope)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                await conn.execute(
                    insert(scopes).values(
                        scope_key=self.scope,
                        identity=self._identity,
                        bytes=0,
                        operations=0,
                        next_item=1,
                        revoked=False,
                        reserved_bytes=0,
                    )
                )
            elif text_field(row, "identity") != self._identity:
                raise RemoteError(
                    RemoteErrorCode.CONFLICT, "Remote endpoint storage settings changed"
                )

    @asynccontextmanager
    async def _database(
        self, *, read: bool = False, schema_lock: str | None = None
    ) -> AsyncGenerator[AsyncConnection]:
        try:
            async with SqlTransaction(
                self.engine, read_only=read, schema_lock=schema_lock
            ) as conn:
                yield conn
        except (SQLAlchemyError, SqlTransactionError) as error:
            raise RemoteError(
                RemoteErrorCode.STORAGE,
                "Remote operation storage is unavailable",
                cause=error,
            ) from error
        except TimeoutError as error:
            raise RemoteError(
                RemoteErrorCode.TIMEOUT,
                "Remote operation storage exceeded its deadline",
                cause=error,
            ) from error

    @asynccontextmanager
    async def transaction(
        self, *, read: bool = False
    ) -> AsyncGenerator[AsyncConnection]:
        async with self._database(read=read) as conn:
            if not read:
                row = (
                    (
                        await conn.execute(
                            select(scopes)
                            .where(scopes.c.scope_key == self.scope)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                if text_field(row, "identity") != self._identity:
                    raise RemoteError(
                        RemoteErrorCode.CONFLICT, "Remote endpoint ownership changed"
                    )
            yield conn

    def match(self, table: Table) -> ColumnElement[bool]:
        return table.c.scope_key == self.scope

    async def _charge(
        self,
        conn: AsyncConnection,
        size: int,
        *,
        operation: bool = False,
        reserve: int = 0,
    ) -> int:
        row = (
            (await conn.execute(select(scopes).where(self.match(scopes))))
            .mappings()
            .one()
        )
        retained = number_field(row, "bytes") + size
        count = number_field(row, "operations") + int(operation)
        sequence = number_field(row, "next_item")
        reserved = (
            number_field(row, "reserved_bytes") + (8192 if operation else 0) + reserve
        )
        if (
            retained + reserved > self.limits.retained_bytes
            or count > self.limits.operations
        ):
            raise RemoteError(
                RemoteErrorCode.CAPACITY, "Remote endpoint storage capacity is full"
            )
        await conn.execute(
            update(scopes)
            .where(self.match(scopes))
            .values(
                bytes=retained,
                operations=count,
                next_item=sequence + 1,
                reserved_bytes=reserved,
            )
        )
        return sequence

    async def _item(
        self,
        conn: AsyncConnection,
        kind: ItemKind,
        value: BaseModel,
        *,
        group: str,
        sequence: int = 0,
        existing_only: bool = False,
    ) -> bool:
        identifier = item_key(value)
        condition = (
            self.match(items)
            & (items.c.kind == kind)
            & (items.c.item_key == identifier)
        )
        row = (await conn.execute(select(items).where(condition))).mappings().first()
        payload = encode(value)
        if (
            len(payload.encode())
            > self.limits.frame_bytes // 2 + self.limits.frame_bytes // 8
        ):
            raise RemoteError(
                RemoteErrorCode.CAPACITY,
                "Remote item exceeds its single-frame delivery limit",
            )
        fingerprint = digest(value)
        if row is not None:
            if text_field(row, "digest") != fingerprint or (
                row["payload"] is not None and text_field(row, "payload") != payload
            ):
                raise RemoteError(
                    RemoteErrorCode.CONFLICT,
                    "Remote receipt changed its immutable content",
                )
            return False
        if existing_only:
            raise RemoteError(
                RemoteErrorCode.UNCERTAIN,
                "Remote payload is retired; unrecognized delivery cannot recreate it",
            )
        evidence_bytes = (
            len(self.scope)
            + len(kind)
            + len(identifier)
            + len(group)
            + len(fingerprint)
            + 9
        )
        position = await self._charge(conn, len(payload.encode()) + evidence_bytes)
        await conn.execute(
            insert(items).values(
                scope_key=self.scope,
                kind=kind,
                item_key=identifier,
                group_id=group,
                sequence=position if kind == "command" else sequence,
                payload=payload,
                digest=fingerprint,
                acknowledged=False,
            )
        )
        return True

    async def _session(self, conn: AsyncConnection, identifier: str) -> RowMapping:
        row = (
            (
                await conn.execute(
                    select(sessions).where(
                        self.match(sessions) & (sessions.c.session_id == identifier)
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RemoteError(
                RemoteErrorCode.REJECTED, "Workspace session is not authorized"
            )
        return row

    async def _operation(self, conn: AsyncConnection, identifier: str) -> RowMapping:
        row = (
            (
                await conn.execute(
                    select(operations).where(
                        self.match(operations)
                        & (operations.c.operation_id == identifier)
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RemoteError(
                RemoteErrorCode.REJECTED, "Operation is not owned by this endpoint"
            )
        return row

    @owned_io
    async def command(self, envelope: CommandEnvelope) -> None:
        async with self.transaction() as conn:
            command = envelope.command
            if envelope.command_id != digest(command):
                raise RemoteError(
                    RemoteErrorCode.CONFLICT,
                    "Command identity does not match its content",
                )
            revoked = (
                await conn.execute(select(scopes.c.revoked).where(self.match(scopes)))
            ).scalar_one()
            if revoked and isinstance(command, (OpenWorkspace, Dispatch)):
                raise RemoteError(
                    RemoteErrorCode.REJECTED,
                    "Remote endpoint authorization was revoked",
                )
            if isinstance(command, OpenWorkspace):
                request = command.request
                if (
                    request.identity.namespace != self.namespace
                    or request.target != self.target
                ):
                    raise RemoteError(
                        RemoteErrorCode.REJECTED,
                        "Workspace crossed its authenticated endpoint",
                    )
                if workspace_key(request) != request.workspace_scope_id:
                    raise RemoteError(
                        RemoteErrorCode.CONFLICT,
                        "Workspace scope does not match authorization",
                    )
                row = (
                    (
                        await conn.execute(
                            select(sessions).where(
                                self.match(sessions)
                                & (sessions.c.session_id == request.session_id)
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    opened = (
                        await conn.execute(
                            select(sessions.c.session_id).where(
                                self.match(sessions) & (sessions.c.closed.is_(False))
                            )
                        )
                    ).all()
                    if len(opened) >= self.limits.sessions:
                        raise RemoteError(
                            RemoteErrorCode.CAPACITY,
                            "Remote workspace session capacity is full",
                        )
                    await self._charge(
                        conn, len(encode(request).encode()) + 64 * 3 + 11, reserve=4096
                    )
                    await conn.execute(
                        insert(sessions).values(
                            scope_key=self.scope,
                            session_id=request.session_id,
                            request=encode(request),
                            digest=digest(request),
                            reserved_bytes=4096,
                            closed=False,
                            settled=False,
                            compacted=False,
                        )
                    )
                elif text_field(row, "digest") != digest(request):
                    raise RemoteError(
                        RemoteErrorCode.CONFLICT,
                        "Workspace session changed its authorization",
                    )
                group = request.session_id
            elif isinstance(command, CloseWorkspace):
                session = await self._session(conn, command.session_id)
                if number_field(session, "reserved_bytes") == 4096:
                    await conn.execute(
                        update(sessions)
                        .where(
                            self.match(sessions)
                            & (sessions.c.session_id == command.session_id)
                        )
                        .values(reserved_bytes=2048)
                    )
                    await conn.execute(
                        update(scopes)
                        .where(self.match(scopes))
                        .values(reserved_bytes=scopes.c.reserved_bytes - 2048)
                    )
                await conn.execute(
                    update(sessions)
                    .where(
                        self.match(sessions)
                        & (sessions.c.session_id == command.session_id)
                    )
                    .values(closed=True)
                )
                await conn.execute(
                    update(operations)
                    .where(
                        self.match(operations)
                        & (operations.c.session_id == command.session_id)
                        & operations.c.state.not_in(TERMINAL_EVENTS)
                    )
                    .values(cancelled=True)
                )
                group = command.session_id
            elif isinstance(command, Dispatch):
                session = await self._session(conn, command.session_id)
                workspace = WorkspaceRequest.model_validate_json(
                    text_field(session, "request")
                )
                request = command.request
                if (
                    request.origin.thread != workspace.identity.thread
                    or request.workspace_scope_id != workspace.workspace_scope_id
                ):
                    raise RemoteError(
                        RemoteErrorCode.REJECTED,
                        "Operation crossed its workspace authorization",
                    )
                if operation_key(request) != request.operation_id:
                    raise RemoteError(
                        RemoteErrorCode.CONFLICT,
                        "Operation identity changed its invocation",
                    )
                if (request.observation_session is not None) != isinstance(
                    request.operation, ResolvePath
                ):
                    raise RemoteError(
                        RemoteErrorCode.PROTOCOL,
                        "Only path safety observations bind a delivery session",
                    )
                if (
                    request.observation_session is not None
                    and request.observation_session != command.session_id
                ):
                    raise RemoteError(
                        RemoteErrorCode.REJECTED,
                        "Path observation belongs to another session",
                    )
                row = (
                    (
                        await conn.execute(
                            select(operations).where(
                                self.match(operations)
                                & (operations.c.operation_id == request.operation_id)
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    if session["closed"]:
                        raise RemoteError(
                            RemoteErrorCode.CLOSED, "Workspace session is closed"
                        )
                    payload = encode(request)
                    if len(payload.encode()) > self.limits.request_bytes:
                        raise RemoteError(
                            RemoteErrorCode.CAPACITY,
                            "Operation arguments exceed their byte limit",
                        )
                    if (
                        isinstance(request.operation, UploadFiles)
                        and sum(f.size for f in request.operation.files)
                        > self.limits.file_bytes
                    ):
                        raise RemoteError(
                            RemoteErrorCode.CAPACITY,
                            "Binary upload exceeds its total byte limit",
                        )
                    await self._charge(
                        conn,
                        len(payload.encode()) + 64 * 4 + 16 + 8 * 5 + 3,
                        operation=True,
                    )
                    await conn.execute(
                        insert(operations).values(
                            scope_key=self.scope,
                            operation_id=request.operation_id,
                            session_id=command.session_id,
                            request=payload,
                            digest=digest(request),
                            state="pending",
                            last_sequence=0,
                            cancelled=False,
                            reserved_bytes=8192,
                            retired=False,
                            result_available=True,
                            output_bytes=0,
                            result_bytes=0,
                            binary_bytes=0,
                        )
                    )
                elif text_field(row, "digest") != digest(request):
                    raise RemoteError(
                        RemoteErrorCode.CONFLICT,
                        "Operation replay changed its arguments",
                    )
                elif text_field(row, "state") == "pending" and not session["closed"]:
                    await conn.execute(
                        update(operations)
                        .where(
                            self.match(operations)
                            & (operations.c.operation_id == request.operation_id)
                        )
                        .values(session_id=command.session_id)
                    )
                group = request.operation_id
            else:
                assert isinstance(command, CancelOperation)
                operation_row = await self._operation(conn, command.operation_id)
                if number_field(operation_row, "reserved_bytes") == 8192:
                    await conn.execute(
                        update(operations)
                        .where(
                            self.match(operations)
                            & (operations.c.operation_id == command.operation_id)
                        )
                        .values(reserved_bytes=6144)
                    )
                    await conn.execute(
                        update(scopes)
                        .where(self.match(scopes))
                        .values(reserved_bytes=scopes.c.reserved_bytes - 2048)
                    )
                await conn.execute(
                    update(operations)
                    .where(
                        self.match(operations)
                        & (operations.c.operation_id == command.operation_id)
                    )
                    .values(cancelled=True)
                )
                group = command.operation_id
            await self._item(conn, "command", envelope, group=group)

    @owned_io
    async def upload_chunk(self, chunk: BinaryChunk) -> None:
        data = chunk_bytes(chunk, self.limits.chunk_bytes)
        async with self.transaction() as conn:
            row = await self._operation(conn, chunk.operation_id)
            if row["retired"]:
                await self._item(
                    conn,
                    "chunk",
                    chunk,
                    group=chunk.operation_id,
                    sequence=chunk.offset,
                    existing_only=True,
                )
                return
            request = OperationRequest.model_validate_json(text_field(row, "request"))
            operation = request.operation
            if (
                not isinstance(operation, UploadFiles)
                or chunk.channel != "file"
                or chunk.file_index >= len(operation.files)
            ):
                raise RemoteError(
                    RemoteErrorCode.REJECTED, "Binary upload has no matching operation"
                )
            if chunk.offset + len(data) > operation.files[chunk.file_index].size:
                raise RemoteError(
                    RemoteErrorCode.CAPACITY, "Binary upload exceeds its declared size"
                )
            created = await self._item(
                conn, "chunk", chunk, group=chunk.operation_id, sequence=chunk.offset
            )
            if created:
                condition = (
                    self.match(transfers)
                    & (transfers.c.operation_id == chunk.operation_id)
                    & (transfers.c.channel == "upload")
                    & (transfers.c.file_index == chunk.file_index)
                )
                previous = (
                    await conn.execute(select(transfers.c.bytes).where(condition))
                ).scalar_one_or_none()
                if chunk.offset != (previous or 0) or not data:
                    raise RemoteError(
                        RemoteErrorCode.PROTOCOL, "Upload offset is not contiguous"
                    )
                if previous is None:
                    await self._charge(
                        conn,
                        len(self.scope) + len(chunk.operation_id) + len("upload") + 16,
                    )
                    await conn.execute(
                        insert(transfers).values(
                            scope_key=self.scope,
                            operation_id=chunk.operation_id,
                            channel="upload",
                            file_index=chunk.file_index,
                            bytes=len(data),
                        )
                    )
                else:
                    await conn.execute(
                        update(transfers)
                        .where(condition)
                        .values(bytes=previous + len(data))
                    )

    @owned_io
    async def receive_event(self, event: OperationEvent) -> None:
        async with self.transaction() as conn:
            await self._receive_event(conn, event)

    async def _receive_event(
        self, conn: AsyncConnection, event: OperationEvent
    ) -> None:
        row = await self._operation(conn, event.operation_id)
        if event.sequence <= number_field(row, "last_sequence"):
            if await self._item(
                conn, "event", event, group=event.operation_id, sequence=event.sequence
            ):
                raise RemoteError(
                    RemoteErrorCode.PROTOCOL, "Operation event history has a gap"
                )
            return
        if event.sequence != number_field(row, "last_sequence") + 1:
            raise RemoteError(
                RemoteErrorCode.PROTOCOL, "Operation event is out of sequence"
            )
        state = text_field(row, "state")
        if state in TERMINAL_EVENTS:
            raise RemoteError(
                RemoteErrorCode.CONFLICT, "Operation already has a terminal result"
            )
        if event.kind == "started" and state != "pending":
            raise RemoteError(
                RemoteErrorCode.CONFLICT, "Operation execution already started"
            )
        if state == "pending" and event.kind not in {
            "started",
            "failed",
            "cancelled",
            "uncertain",
        }:
            raise RemoteError(
                RemoteErrorCode.PROTOCOL, "Operation output precedes execution"
            )
        if event.chunk is not None:
            chunk = event.chunk
            data = chunk_bytes(chunk, self.limits.chunk_bytes)
            condition = (
                self.match(transfers)
                & (transfers.c.operation_id == event.operation_id)
                & (transfers.c.channel == chunk.channel)
                & (transfers.c.file_index == chunk.file_index)
            )
            previous = (
                await conn.execute(select(transfers.c.bytes).where(condition))
            ).scalar_one_or_none()
            if chunk.offset != (previous or 0) or not data:
                raise RemoteError(
                    RemoteErrorCode.PROTOCOL, "Binary result offset is not contiguous"
                )
            if previous is None:
                await self._charge(
                    conn,
                    len(self.scope) + len(event.operation_id) + len(chunk.channel) + 16,
                )
                await conn.execute(
                    insert(transfers).values(
                        scope_key=self.scope,
                        operation_id=event.operation_id,
                        channel=chunk.channel,
                        file_index=chunk.file_index,
                        bytes=len(data),
                    )
                )
            else:
                await conn.execute(
                    update(transfers)
                    .where(condition)
                    .values(bytes=previous + len(data))
                )
            column = "result_bytes" if chunk.channel == "result" else "binary_bytes"
            total = number_field(row, column) + len(data)
            limit = (
                self.limits.output_bytes
                if chunk.channel == "result"
                else self.limits.file_bytes
            )
            if total > limit or (chunk.channel == "result" and chunk.file_index != 0):
                raise RemoteError(
                    RemoteErrorCode.CAPACITY,
                    "Binary result exceeds its operation limit",
                )
            await conn.execute(
                update(operations)
                .where(
                    self.match(operations)
                    & (operations.c.operation_id == event.operation_id)
                )
                .values({column: total})
            )
        if event.kind == "output":
            total = number_field(row, "output_bytes") + len(
                json.dumps(event.output, ensure_ascii=True).encode()
            )
            if total > self.limits.output_bytes:
                raise RemoteError(
                    RemoteErrorCode.CAPACITY,
                    "Operation progress exceeds its byte limit",
                )
            await conn.execute(
                update(operations)
                .where(
                    self.match(operations)
                    & (operations.c.operation_id == event.operation_id)
                )
                .values(output_bytes=total)
            )
        if (
            event.result is not None
            and len(encode(event.result).encode()) > self.limits.output_bytes
        ):
            raise RemoteError(
                RemoteErrorCode.CAPACITY, "Operation result exceeds its byte limit"
            )
        if (
            event.result_transfer is not None
            and event.result_transfer.size != number_field(row, "result_bytes")
        ):
            raise RemoteError(
                RemoteErrorCode.PROTOCOL, "Operation result transfer is incomplete"
            )
        if event.kind in TERMINAL_EVENTS:
            await conn.execute(
                update(scopes)
                .where(self.match(scopes))
                .values(
                    reserved_bytes=scopes.c.reserved_bytes
                    - number_field(row, "reserved_bytes"),
                    operations=scopes.c.operations - 1,
                )
            )
        await self._item(
            conn, "event", event, group=event.operation_id, sequence=event.sequence
        )
        await conn.execute(
            update(operations)
            .where(
                self.match(operations)
                & (operations.c.operation_id == event.operation_id)
            )
            .values(
                state=event.kind
                if event.kind in TERMINAL_EVENTS or event.kind == "started"
                else state,
                last_sequence=event.sequence,
                reserved_bytes=0
                if event.kind in TERMINAL_EVENTS
                else number_field(row, "reserved_bytes"),
            )
        )

    @owned_io
    async def append(self, identifier: str, **values: object) -> OperationEvent:
        async with self.transaction() as conn:
            row = await self._operation(conn, identifier)
            event = OperationEvent.model_validate(
                {
                    "operation_id": identifier,
                    "sequence": number_field(row, "last_sequence") + 1,
                    **values,
                }
            )
            await self._receive_event(conn, event)
            return event

    @owned_io
    async def finish(self, identifier: str, kind: str, message: str) -> None:
        async with self.transaction() as conn:
            row = await self._operation(conn, identifier)
            if text_field(row, "state") in TERMINAL_EVENTS:
                return
            event = OperationEvent.model_validate(
                {
                    "operation_id": identifier,
                    "sequence": number_field(row, "last_sequence") + 1,
                    "kind": kind,
                    "message": message,
                }
            )
            await self._receive_event(conn, event)

    @owned_io
    async def workspace_event(self, value: WorkspaceEvent) -> None:
        async with self.transaction() as conn:
            session = await self._session(conn, value.session_id)
            request = WorkspaceRequest.model_validate_json(
                text_field(session, "request")
            )
            expected = (
                digest(CloseWorkspace(session_id=value.session_id))
                if value.closed
                else digest(OpenWorkspace(request=request))
            )
            if value.command_id != expected:
                raise RemoteError(
                    RemoteErrorCode.REJECTED,
                    "Workspace reply belongs to another control request",
                )
            if value.closed:
                await conn.execute(
                    update(scopes)
                    .where(self.match(scopes))
                    .values(
                        reserved_bytes=scopes.c.reserved_bytes
                        - number_field(session, "reserved_bytes")
                    )
                )
                await conn.execute(
                    update(sessions)
                    .where(
                        self.match(sessions)
                        & (sessions.c.session_id == value.session_id)
                    )
                    .values(reserved_bytes=0)
                )
            await self._item(conn, "workspace", value, group=value.session_id)
            if value.closed:
                await conn.execute(
                    update(sessions)
                    .where(
                        self.match(sessions)
                        & (sessions.c.session_id == value.session_id)
                    )
                    .values(settled=True)
                )

    @owned_io
    async def closing_sessions(self) -> tuple[str, ...]:
        async with self.transaction(read=True) as conn:
            rows = (
                (
                    await conn.execute(
                        select(sessions.c.session_id).where(
                            self.match(sessions)
                            & sessions.c.closed.is_(True)
                            & sessions.c.settled.is_(False)
                        )
                    )
                )
                .scalars()
                .all()
            )
        return tuple(rows)

    @owned_io
    async def session_active(self, identifier: str) -> bool:
        async with self.transaction(read=True) as conn:
            return (
                await conn.execute(
                    select(operations.c.operation_id)
                    .where(
                        self.match(operations)
                        & (operations.c.session_id == identifier)
                        & operations.c.state.not_in(TERMINAL_EVENTS)
                    )
                    .limit(1)
                )
            ).first() is not None

    @owned_io
    async def has_session(self, identifier: str) -> bool:
        async with self.transaction(read=True) as conn:
            return (
                await conn.execute(
                    select(sessions.c.session_id).where(
                        self.match(sessions) & (sessions.c.session_id == identifier)
                    )
                )
            ).first() is not None

    @owned_io
    async def has_operation(self, identifier: str) -> bool:
        async with self.transaction(read=True) as conn:
            return (
                await conn.execute(
                    select(operations.c.operation_id).where(
                        self.match(operations)
                        & (operations.c.operation_id == identifier)
                    )
                )
            ).first() is not None

    @owned_io
    async def acknowledge(self, kind: ItemKind, receipts: Sequence[Receipt]) -> None:
        if not receipts:
            return
        async with self.transaction() as conn:
            for acknowledged in receipts:
                condition = (
                    self.match(items)
                    & (items.c.kind == kind)
                    & (items.c.item_key == acknowledged.key)
                )
                row = (
                    (await conn.execute(select(items).where(condition)))
                    .mappings()
                    .first()
                )
                if row is None or text_field(row, "digest") != acknowledged.digest:
                    raise RemoteError(
                        RemoteErrorCode.CONFLICT,
                        "Peer receipt does not match stored evidence",
                    )
                await conn.execute(
                    update(items).where(condition).values(acknowledged=True)
                )

    @owned_io
    async def pending(
        self, kind: ItemKind, model: type[ValueT], *, budget: int | None = None
    ) -> tuple[ValueT, ...]:
        async with self.transaction(read=True) as conn:
            rows = (
                (
                    await conn.execute(
                        select(items)
                        .where(
                            self.match(items)
                            & (items.c.kind == kind)
                            & (items.c.acknowledged.is_(False))
                            & (items.c.payload.is_not(None))
                        )
                        .order_by(items.c.sequence, items.c.item_key)
                        .limit(8)
                    )
                )
                .mappings()
                .all()
            )
        remaining = (
            self.limits.frame_bytes - min(16_384, self.limits.frame_bytes // 8)
            if budget is None
            else budget
        )
        values: list[ValueT] = []
        for row in rows:
            payload = text_field(row, "payload")
            size = len(payload.encode())
            if size > remaining:
                break
            remaining -= size
            values.append(model.model_validate_json(payload))
        return tuple(values)

    @owned_io
    async def event_page(
        self, identifier: str, *, after: int = 0, limit: int = 64
    ) -> tuple[OperationEvent, ...]:
        async with self.transaction(read=True) as conn:
            await self._operation(conn, identifier)
            rows = (
                (
                    await conn.execute(
                        select(items.c.payload)
                        .where(
                            self.match(items)
                            & (items.c.kind == "event")
                            & (items.c.group_id == identifier)
                            & (items.c.sequence > after)
                            & (items.c.payload.is_not(None))
                        )
                        .order_by(items.c.sequence)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return tuple(OperationEvent.model_validate_json(value) for value in rows)

    @owned_io
    async def get_operation(
        self, identifier: str
    ) -> tuple[OperationRequest | None, str, bool]:
        async with self.transaction(read=True) as conn:
            row = await self._operation(conn, identifier)
            return (
                None
                if row["request"] is None
                else OperationRequest.model_validate_json(text_field(row, "request")),
                text_field(row, "state"),
                bool(row["cancelled"]),
            )

    @owned_io
    async def get_workspace_reply(self, identifier: str) -> WorkspaceEvent | None:
        async with self.transaction(read=True) as conn:
            payload = (
                await conn.execute(
                    select(items.c.payload).where(
                        self.match(items)
                        & (items.c.kind == "workspace")
                        & (items.c.item_key == identifier)
                    )
                )
            ).scalar_one_or_none()
            return (
                None if payload is None else WorkspaceEvent.model_validate_json(payload)
            )

    @owned_io
    async def open_sessions(self) -> tuple[WorkspaceRequest, ...]:
        async with self.transaction(read=True) as conn:
            values = (
                (
                    await conn.execute(
                        select(sessions.c.request).where(
                            self.match(sessions) & (sessions.c.closed.is_(False))
                        )
                    )
                )
                .scalars()
                .all()
            )
        return tuple(WorkspaceRequest.model_validate_json(value) for value in values)

    @owned_io
    async def dispatches(self) -> tuple[Dispatch, ...]:
        async with self.transaction(read=True) as conn:
            rows = (
                (
                    await conn.execute(
                        select(operations.c.session_id, operations.c.request).where(
                            self.match(operations)
                            & operations.c.state.not_in(TERMINAL_EVENTS)
                        )
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            Dispatch(
                session_id=text_field(row, "session_id"),
                request=OperationRequest.model_validate_json(
                    text_field(row, "request")
                ),
            )
            for row in rows
        )

    @owned_io
    async def session(self, identifier: str) -> tuple[WorkspaceRequest, bool]:
        async with self.transaction(read=True) as conn:
            row = await self._session(conn, identifier)
            return WorkspaceRequest.model_validate_json(
                text_field(row, "request")
            ), bool(row["closed"])

    @owned_io
    async def is_revoked(self) -> bool:
        async with self.transaction(read=True) as conn:
            return bool(
                (
                    await conn.execute(
                        select(scopes.c.revoked).where(self.match(scopes))
                    )
                ).scalar_one()
            )

    @owned_io
    async def revoke(self) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                update(scopes).where(self.match(scopes)).values(revoked=True)
            )
            await conn.execute(
                update(operations)
                .where(
                    self.match(operations) & operations.c.state.not_in(TERMINAL_EVENTS)
                )
                .values(cancelled=True)
            )
            await conn.execute(
                update(sessions).where(self.match(sessions)).values(closed=True)
            )

    @owned_io
    async def result_available(self, identifier: str) -> bool:
        async with self.transaction(read=True) as conn:
            return bool((await self._operation(conn, identifier))["result_available"])

    @owned_io
    async def completed(self, request: OperationRequest) -> bool:
        async with self.transaction(read=True) as conn:
            row = (
                (
                    await conn.execute(
                        select(operations.c.digest, operations.c.state).where(
                            self.match(operations)
                            & (operations.c.operation_id == request.operation_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            if text_field(row, "digest") != digest(request):
                raise RemoteError(
                    RemoteErrorCode.CONFLICT, "Operation replay changed its arguments"
                )
            return text_field(row, "state") in TERMINAL_EVENTS

    @owned_io
    async def session_settled(self, identifier: str) -> bool:
        async with self.transaction(read=True) as conn:
            return bool((await self._session(conn, identifier))["settled"])

    @owned_io
    async def reclaim(self) -> None:
        """Compact closed, acknowledged work without forgetting immutable identities.

        Small results remain replayable across approval and runtime reconstruction.
        Large completed results may expire; their effect and digest tombstones
        never become a new pending operation. Receipt digests also survive lost
        acknowledgments, so delayed frames cannot recreate reclaimed payloads.
        """
        async with self.transaction() as conn:
            owned_operations = (
                select(operations.c.operation_id)
                .where(
                    self.match(operations)
                    & (operations.c.session_id == sessions.c.session_id)
                )
                .correlate(sessions)
            )
            unsettled = (
                select(operations.c.operation_id)
                .where(
                    self.match(operations)
                    & (operations.c.session_id == sessions.c.session_id)
                    & operations.c.state.not_in(TERMINAL_EVENTS)
                )
                .correlate(sessions)
                .exists()
            )
            unacknowledged = (
                select(items.c.item_key)
                .where(
                    self.match(items)
                    & items.c.acknowledged.is_(False)
                    & or_(
                        items.c.group_id == sessions.c.session_id,
                        items.c.group_id.in_(owned_operations),
                    )
                )
                .correlate(sessions)
                .exists()
            )
            selected = (
                (
                    await conn.execute(
                        select(sessions.c.session_id)
                        .where(
                            self.match(sessions)
                            & sessions.c.closed.is_(True)
                            & sessions.c.settled.is_(True)
                            & sessions.c.compacted.is_(False)
                            & ~unsettled
                            & ~unacknowledged
                        )
                        .limit(8)
                    )
                )
                .scalars()
                .all()
            )
            refunded = 0
            for session_id in selected:
                rows = (
                    (
                        await conn.execute(
                            select(operations).where(
                                self.match(operations)
                                & (operations.c.session_id == session_id)
                                & operations.c.retired.is_(False)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    identifier = text_field(row, "operation_id")
                    terminal_key = f"{identifier}:{number_field(row, 'last_sequence')}"
                    terminal_payload = (
                        await conn.execute(
                            select(items.c.payload).where(
                                self.match(items)
                                & (items.c.kind == "event")
                                & (items.c.item_key == terminal_key)
                            )
                        )
                    ).scalar_one()
                    if not isinstance(terminal_payload, str):
                        raise RemoteError(
                            RemoteErrorCode.PROTOCOL,
                            "Operation terminal evidence is missing",
                        )
                    terminal = OperationEvent.model_validate_json(terminal_payload)
                    available = terminal.kind != "completed" or (
                        number_field(row, "binary_bytes") == 0
                        and (
                            (
                                terminal.result is not None
                                and len(encode(terminal.result).encode())
                                <= self.limits.replay_result_bytes
                            )
                            or (
                                terminal.result_transfer is not None
                                and terminal.result_transfer.size
                                <= self.limits.replay_result_bytes
                            )
                        )
                    )
                    retained_keys = [terminal_key] if available else []
                    if available and terminal.result_transfer is not None:
                        event_rows = (
                            (
                                await conn.execute(
                                    select(items.c.item_key, items.c.payload).where(
                                        self.match(items)
                                        & (items.c.kind == "event")
                                        & (items.c.group_id == identifier)
                                    )
                                )
                            )
                            .mappings()
                            .all()
                        )
                        for event_row in event_rows:
                            event = OperationEvent.model_validate_json(
                                text_field(event_row, "payload")
                            )
                            if (
                                event.chunk is not None
                                and event.chunk.channel == "result"
                            ):
                                retained_keys.append(text_field(event_row, "item_key"))
                    payloads = (
                        self.match(items)
                        & (items.c.group_id == identifier)
                        & items.c.payload.is_not(None)
                    )
                    if retained_keys:
                        payloads = payloads & items.c.item_key.not_in(retained_keys)
                    refunded += int(
                        (
                            await conn.execute(
                                select(
                                    func.coalesce(
                                        func.sum(func.length(items.c.payload)), 0
                                    )
                                ).where(payloads)
                            )
                        ).scalar_one()
                    )
                    await conn.execute(
                        update(items).where(payloads).values(payload=None)
                    )
                    request = row["request"]
                    if (
                        isinstance(request, str)
                        and len(request.encode()) > self.limits.replay_result_bytes
                    ):
                        refunded += len(request.encode())
                        request = None
                    await conn.execute(
                        update(operations)
                        .where(
                            self.match(operations)
                            & (operations.c.operation_id == identifier)
                        )
                        .values(
                            request=request, retired=True, result_available=available
                        )
                    )
                    transfer_rows = self.match(transfers) & (
                        transfers.c.operation_id == identifier
                    )
                    refunded += int(
                        (
                            await conn.execute(
                                select(
                                    func.coalesce(
                                        func.sum(
                                            func.length(transfers.c.scope_key)
                                            + func.length(transfers.c.operation_id)
                                            + func.length(transfers.c.channel)
                                            + 16
                                        ),
                                        0,
                                    )
                                ).where(transfer_rows)
                            )
                        ).scalar_one()
                    )
                    await conn.execute(delete(transfers).where(transfer_rows))
                controls = (
                    self.match(items)
                    & (items.c.group_id == session_id)
                    & items.c.payload.is_not(None)
                )
                refunded += int(
                    (
                        await conn.execute(
                            select(
                                func.coalesce(func.sum(func.length(items.c.payload)), 0)
                            ).where(controls)
                        )
                    ).scalar_one()
                )
                await conn.execute(update(items).where(controls).values(payload=None))
                await conn.execute(
                    update(sessions)
                    .where(self.match(sessions) & (sessions.c.session_id == session_id))
                    .values(compacted=True)
                )
            if refunded:
                await conn.execute(
                    update(scopes)
                    .where(self.match(scopes))
                    .values(bytes=scopes.c.bytes - refunded)
                )

    @owned_io
    async def upload_ready(self, identifier: str) -> bool:
        request, _state, _cancelled = await self.get_operation(identifier)
        if request is None:
            return True
        if not isinstance(request.operation, UploadFiles):
            return True
        async with self.transaction(read=True) as conn:
            rows = (
                (
                    await conn.execute(
                        select(transfers.c.file_index, transfers.c.bytes).where(
                            self.match(transfers)
                            & (transfers.c.operation_id == identifier)
                            & (transfers.c.channel == "upload")
                        )
                    )
                )
                .mappings()
                .all()
            )
        sizes = {
            number_field(row, "file_index"): number_field(row, "bytes") for row in rows
        }
        return all(
            sizes.get(index, 0) == item.size
            for index, item in enumerate(request.operation.files)
        )

    @owned_io
    async def upload_content(self, identifier: str) -> tuple[bytes, ...] | None:
        request, _state, _cancelled = await self.get_operation(identifier)
        if request is None:
            raise RemoteError(
                RemoteErrorCode.UNCERTAIN,
                "Operation payload was retired and cannot be reexecuted",
            )
        if not isinstance(request.operation, UploadFiles):
            return ()
        if not await self.upload_ready(identifier):
            return None
        async with self.transaction(read=True) as conn:
            rows = (
                (
                    await conn.execute(
                        select(items.c.payload)
                        .where(
                            self.match(items)
                            & (items.c.kind == "chunk")
                            & (items.c.group_id == identifier)
                        )
                        .order_by(items.c.sequence)
                    )
                )
                .scalars()
                .all()
            )
        chunks = [BinaryChunk.model_validate_json(value) for value in rows]
        values: list[bytes] = []
        from ._wire import digest_bytes

        for index, expected in enumerate(request.operation.files):
            content = bytearray()
            for part in sorted(
                (c for c in chunks if c.file_index == index), key=lambda c: c.offset
            ):
                if part.offset != len(content):
                    raise RemoteError(
                        RemoteErrorCode.PROTOCOL,
                        "Binary upload has a noncontiguous offset",
                    )
                content.extend(chunk_bytes(part, self.limits.chunk_bytes))
            if len(content) < expected.size:
                return None
            if (
                len(content) != expected.size
                or digest_bytes(bytes(content)) != expected.digest
            ):
                raise RemoteError(
                    RemoteErrorCode.CONFLICT,
                    "Binary upload does not match its complete digest",
                )
            values.append(bytes(content))
        return tuple(values)
