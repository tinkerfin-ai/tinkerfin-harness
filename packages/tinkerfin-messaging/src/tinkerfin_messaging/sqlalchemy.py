"""Ordered message storage on a caller-owned asynchronous SQLAlchemy Engine."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import TypeVar
from uuid import uuid4

from sqlalchemy import delete, func, literal, literal_column, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_sqlalchemy import SqlTransaction, engine_dialect

from ._capacity import checkpoint_bytes
from ._identity import required_identifier, required_identity, thread_key
from ._messaging_transition import resolve_messaging_transition
from ._sql_records import (
    Scope,
    binary,
    checkpoint_values,
    decode_text,
    digest,
    encode_text,
    integer,
    live_disposition,
    message_evidence,
    optional_decoded_text,
    stored_run,
    text_value,
    utc_time,
)
from ._sql_schema import (
    capacity,
    channels,
    generations,
    messages,
    prepare_schema,
    runs,
    threads,
)
from ._tasks import Cancellation, capture, join_owned_task, select_failure
from .backend import is_final_run_status
from .backend_contract import (
    CommittedMessagePage,
    CommittedMessageQuery,
    MessagingBackendSettings,
    MessagingChangeCursor,
    MessagingChangeWait,
    MessagingCleanupReason,
    MessagingStateQuery,
    MessagingStateSnapshot,
    MessagingStorageEffect,
    MessagingTransition,
    MessagingTransitionResult,
    StoredMessagingChannel,
    StoredMessagingRun,
    StoredMessagingStream,
    StreamGenerationPurge,
    StreamGenerationPurgeResult,
)
from .errors import (
    InvalidCursor,
    MessagingBackendProtocolError,
    MessagingBackendUnavailable,
    MessagingError,
    MessagingQuotaExceeded,
    RunNotFound,
    StreamDeleteConflict,
    StreamDeleted,
    StreamExpired,
)
from .limits import DEFAULT_MESSAGING_LIMITS, MessagingLimits
from .retention import MessagingRetentionPolicy

_T = TypeVar("_T")


class _RejectedTransition(Exception):
    """Transport a pure parameter rejection separately from provider failures."""

    def __init__(self, error: TypeError | ValueError) -> None:
        self.error = error
        super().__init__("Messaging transition parameters are invalid")


def _resolve_requested_transition(
    transition: MessagingTransition, state: MessagingStateSnapshot
) -> MessagingStorageEffect:
    try:
        return resolve_messaging_transition(transition, state)
    except (TypeError, ValueError) as error:
        raise _RejectedTransition(error) from None


def _positive(name: str, value: int, *, zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < (0 if zero else 1):
        raise ValueError(f"{name} must be {'non-negative' if zero else 'positive'}")


def _query_identity(
    channel: str, identity: RunIdentity, generation: int | None
) -> None:
    required_identifier("channel", channel)
    required_identity(identity)
    if generation is not None:
        _positive("generation", generation)


def _check_cancelled(cancellation: Cancellation) -> None:
    if cancellation.error is not None:
        raise cancellation.error


class SqlAlchemyBackend:
    """Persist replayable messages on asynchronous SQLite, MySQL or PostgreSQL.

    All channels in the database share one capacity and settings contract. Namespace
    and thread identities isolate messages, ownership, cancellation and cleanup.
    Messages and runs are stored individually; replay pages contain at most 1000
    messages. Terminal retention is optional and reclaimed during later activity.

    The Engine is borrowed. Each call settles its accepted database work and releases
    its connection before returning or propagating cancellation. No backend shutdown
    step is required. Host connection and statement timeouts bound I/O; schema setup
    alone uses a 30-second setup-lock wait. An uncertain commit is never replayed.
    Global capacity admission serializes writes, including writes to different channels.

    Args:
        engine: Borrowed AsyncEngine with exclusive connection checkouts. Install
            the asynchronous database driver selected by your application.
        producer_lease_seconds: Producer ownership duration on the database clock.
        poll_interval_seconds: Maximum interval before reloading durable changes.
            Waiting holds no database connection or message buffer.
        limits: Individual, thread and database-wide logical capacity limits.
        retention_policy: Terminal replay deadline; disabled by default.

    Raises:
        TypeError: An Engine, policy or timing value has the wrong type.
        ValueError: A dialect or timing value is unsupported.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        producer_lease_seconds: float = 15.0,
        poll_interval_seconds: float = 0.1,
        limits: MessagingLimits = DEFAULT_MESSAGING_LIMITS,
        retention_policy: MessagingRetentionPolicy = MessagingRetentionPolicy(),
    ) -> None:
        """Validate borrowed storage and settings without opening a connection."""

        if isinstance(producer_lease_seconds, bool) or not isinstance(
            producer_lease_seconds, int | float
        ):
            raise TypeError("producer_lease_seconds must be numeric")
        self._settings = MessagingBackendSettings(
            limits=limits,
            retention_policy=retention_policy,
            producer_renew_interval_seconds=producer_lease_seconds / 3,
            producer_lease_seconds=producer_lease_seconds,
            change_wait_timeout_seconds=poll_interval_seconds,
        )
        self._settings = replace(
            self._settings,
            producer_lease_seconds=float(producer_lease_seconds),
            change_wait_timeout_seconds=float(poll_interval_seconds),
        )
        SqlTransaction(engine)
        self._engine = engine
        self._dialect = engine_dialect(engine)
        self._settings_json = json.dumps(
            asdict(self._settings), sort_keys=True, separators=(",", ":")
        )
        self._setup_lock = asyncio.Lock()
        self._ready = False

    @property
    def messaging_settings(self) -> MessagingBackendSettings:
        """Return immutable capacity, retention, producer and waiting settings."""

        return self._settings

    async def prepare_messaging_storage(self) -> None:
        """Create empty tables or validate the complete current storage structure.

        Concurrent preparation converges under a database setup lock. Setup never
        clears records or converts a different existing schema. Accepted DDL finishes
        before caller cancellation so a retry can validate the resulting structure.

        Raises:
            MessagingBackendProtocolError: Existing tables or settings differ.
            MessagingBackendUnavailable: Database preparation fails.
        """

        async def prepared(cancellation: Cancellation) -> None:
            _check_cancelled(cancellation)

        await self._call(prepared)

    async def commit_messaging_transition(
        self, transition: MessagingTransition
    ) -> MessagingTransitionResult:
        """Commit one message or producer transition with its complete evidence.

        Args:
            transition: Framework lifecycle intent with complete run identity.

        Returns:
            Result after the atomic transition has committed.

        Raises:
            MessagingError: Durable state, settings, capacity or the database rejects
                the operation. Unknown commit outcomes remain failures.
            TypeError: A transition field has the wrong type.
            ValueError: An identifier, generation or option is invalid.
        """

        if not isinstance(transition, MessagingTransition):
            raise TypeError("transition must be a MessagingTransition")
        _query_identity(
            transition.channel, transition.identity, transition.cleanup_generation
        )
        if transition.settings != self._settings:
            raise MessagingBackendProtocolError(
                "Messaging transition settings differ from its backend"
            )
        scope = Scope(transition.channel, transition.identity)

        async def commit(cancellation: Cancellation) -> MessagingTransitionResult:
            if transition.kind in {"prepare_run", "append_message", "publish_message"}:
                await self._reclaim_expired(cancellation)
                # Preparation of this thread completes its own expired generation in
                # bounded transactions. Cancellation leaves a resumable sealed state.
                while not await self._reclaim_scope(scope, cancellation):
                    _check_cancelled(cancellation)
            selected = (
                transition.cleanup_generation
                if transition.kind == "finish_generation_cleanup"
                else (
                    None
                    if transition.run_reference is None
                    else transition.run_reference.generation
                )
            )
            async with SqlTransaction(self._engine) as connection:
                now = await self._write_point(connection)
                state = await self._snapshot(
                    connection,
                    MessagingStateQuery(
                        channel=scope.channel,
                        identity=scope.identity,
                        generation=selected,
                        message_id=transition.message_id,
                        include_active_run=True,
                    ),
                    now,
                )
                effect = _resolve_requested_transition(transition, state)
                await self._apply(connection, scope, state, effect, now)
                _check_cancelled(cancellation)
            return effect.result

        return await self._call(commit)

    async def load_messaging_state(
        self, query: MessagingStateQuery
    ) -> MessagingStateSnapshot:
        """Read bounded producer, message evidence and lease state from one snapshot.

        Args:
            query: Channel, complete run identity and optional exact generation.

        Returns:
            Database-clock state, including exact deleted or expired tombstones.

        Raises:
            MessagingError: Storage is unavailable or stored evidence is inconsistent.
            TypeError: A query field has the wrong type.
            ValueError: An identifier or generation is invalid.
        """

        if not isinstance(query, MessagingStateQuery):
            raise TypeError("query must be a MessagingStateQuery")
        _query_identity(query.channel, query.identity, query.generation)
        if query.message_id is not None:
            required_identifier("message_id", query.message_id)

        async def read(cancellation: Cancellation) -> MessagingStateSnapshot:
            async with SqlTransaction(self._engine, read_only=True) as connection:
                now = await self._read_point(connection)
                result = await self._snapshot(connection, query, now)
                _check_cancelled(cancellation)
                return result

        return await self._call(read)

    async def read_committed_messages(
        self, query: CommittedMessageQuery
    ) -> CommittedMessagePage:
        """Read one ordered generation page and its consistent terminal boundary.

        Args:
            query: Exact generation, exclusive cursor, upper bound and page size.

        Returns:
            At most 1000 messages with their observed message and control cursors.

        Raises:
            MessagingError: Cursor, generation, required run or stored evidence is
                unavailable, expired, deleted or invalid.
            TypeError: A query field has the wrong type.
            ValueError: A generation, cursor or page size is out of range.
        """

        if not isinstance(query, CommittedMessageQuery):
            raise TypeError("query must be a CommittedMessageQuery")
        _query_identity(query.channel, query.identity, query.generation)
        _positive("after_sequence", query.after_sequence, zero=True)
        _positive("limit", query.limit)
        if query.limit > 1000:
            raise ValueError("limit must be at most 1000")
        if query.through_sequence is not None:
            _positive("through_sequence", query.through_sequence, zero=True)
        scope = Scope(query.channel, query.identity)

        async def read(cancellation: Cancellation) -> CommittedMessagePage:
            async with SqlTransaction(self._engine, read_only=True) as connection:
                now = await self._read_point(connection)
                state = await self._snapshot(
                    connection,
                    MessagingStateQuery(
                        channel=query.channel,
                        identity=query.identity,
                        generation=query.generation,
                    ),
                    now,
                )
                stream = self._readable(scope, query.generation, state)
                if query.after_sequence > stream.latest_sequence:
                    raise InvalidCursor(
                        after=query.after_sequence, latest=stream.latest_sequence
                    )
                through = (
                    stream.latest_sequence
                    if query.through_sequence is None
                    else min(stream.latest_sequence, query.through_sequence)
                )
                run = state.target_run
                if query.stop_at_run_terminal:
                    if run is None:
                        raise RunNotFound(identity=query.identity)
                    if is_final_run_status(run.status):
                        through = min(through, run.end_sequence)
                assert state.channel is not None
                result = await connection.execute(
                    select(messages)
                    .where(
                        scope.where(messages, query.generation),
                        messages.c.sequence > query.after_sequence,
                        messages.c.sequence <= through,
                    )
                    .order_by(messages.c.sequence)
                    .limit(query.limit)
                )
                envelopes = tuple(
                    message_evidence(row, scope, state.channel.codec_id).envelope
                    for row in result.mappings()
                )
                expected = min(query.limit, max(0, through - query.after_sequence))
                if len(envelopes) != expected or any(
                    item.seq != query.after_sequence + index
                    for index, item in enumerate(envelopes, start=1)
                ):
                    raise MessagingBackendProtocolError(
                        "Messaging SQL replay contains a sequence gap"
                    )
                _check_cancelled(cancellation)
                return CommittedMessagePage(
                    generation=query.generation,
                    latest_sequence=stream.latest_sequence,
                    messages=envelopes,
                    change_cursor=MessagingChangeCursor(
                        message_sequence=stream.message_sequence,
                        control_sequence=stream.control_sequence,
                    ),
                    run_state=run,
                )

        return await self._call(read)

    async def wait_for_messaging_change(self, wait: MessagingChangeWait) -> None:
        """Wait briefly for a possible change without holding a database connection.

        A return is advisory; callers reload durable state even after a timeout.

        Args:
            wait: Exact generation, previously observed cursor and optional timeout.

        Raises:
            MessagingError: The exact generation or storage is unavailable.
            TypeError: The wait or an identity field has the wrong type.
            ValueError: A generation or timeout is invalid.
        """

        if not isinstance(wait, MessagingChangeWait):
            raise TypeError("wait must be a MessagingChangeWait")
        _query_identity(wait.channel, wait.identity, wait.generation)
        _positive("message_sequence", wait.after.message_sequence, zero=True)
        _positive("control_sequence", wait.after.control_sequence, zero=True)
        delay = self._settings.change_wait_timeout_seconds
        assert delay is not None
        if wait.timeout_seconds is not None:
            import math

            if isinstance(wait.timeout_seconds, bool) or not isinstance(
                wait.timeout_seconds, int | float
            ):
                raise TypeError("timeout_seconds must be numeric or None")
            if not math.isfinite(wait.timeout_seconds) or wait.timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be finite and positive")
            delay = min(delay, wait.timeout_seconds)
        state = await self.load_messaging_state(
            MessagingStateQuery(
                channel=wait.channel,
                identity=wait.identity,
                generation=wait.generation,
            )
        )
        stream = self._readable(
            Scope(wait.channel, wait.identity), wait.generation, state
        )
        if (
            stream.message_sequence != wait.after.message_sequence
            or stream.control_sequence != wait.after.control_sequence
        ):
            return
        await asyncio.sleep(delay)

    async def purge_stream_generation(
        self, purge: StreamGenerationPurge
    ) -> StreamGenerationPurgeResult:
        """Remove one bounded batch from an exact sealed generation.

        Args:
            purge: Sealed generation and maximum number of private records to remove.

        Returns:
            Exact removed-record count and whether no private records remain.

        Raises:
            MessagingError: The generation is active or storage cannot be accessed.
            TypeError: A purge field has the wrong type.
            ValueError: A generation or batch size is invalid.
        """

        if not isinstance(purge, StreamGenerationPurge):
            raise TypeError("purge must be a StreamGenerationPurge")
        _query_identity(purge.channel, purge.identity, purge.generation)
        _positive("maximum_records", purge.maximum_records)
        if purge.cleanup_token is not None:
            raise ValueError("SQL generation cleanup does not use ownership tokens")
        scope = Scope(purge.channel, purge.identity)

        async def remove(cancellation: Cancellation) -> StreamGenerationPurgeResult:
            async with SqlTransaction(self._engine) as connection:
                now = await self._write_point(connection)
                state = await self._snapshot(
                    connection,
                    MessagingStateQuery(
                        channel=purge.channel,
                        identity=purge.identity,
                        generation=purge.generation,
                    ),
                    now,
                )
                if state.stream is None:
                    return StreamGenerationPurgeResult(0, True)
                if state.stream.disposition not in {"deleting", "expiring"}:
                    raise StreamDeleteConflict(
                        channel=purge.channel,
                        identity=purge.identity,
                        active_identity=purge.identity,
                    )
                result = await self._purge(
                    connection, scope, purge.generation, purge.maximum_records
                )
                _check_cancelled(cancellation)
                return result

        return await self._call(remove)

    async def _call(self, operation: Callable[[Cancellation], Awaitable[_T]]) -> _T:
        cancellation = Cancellation()

        async def accepted() -> _T:
            rejected: BaseException | None = None
            try:
                await self._prepare()
                _check_cancelled(cancellation)
                return await operation(cancellation)
            except _RejectedTransition as error:
                rejected = error.error
                # Transaction cleanup may have added independent failures to the
                # marker. Keep those original causes when restoring the public
                # input error, removing back edges through that input exception.
                for evidence in (error.__cause__, error.__context__):
                    if evidence is not None:
                        rejected = select_failure(rejected, evidence)
            except MessagingError:
                raise
            except Exception as error:
                raise MessagingBackendUnavailable(
                    "Messaging SQL storage operation failed", cause=error
                ) from error
            # Raise outside the marker's except block. Linking the original input
            # exception back to its transport marker would create a context cycle.
            assert rejected is not None
            raise rejected

        # Capturing control exceptions prevents a child Task from stopping the event
        # loop before its caller can receive both the operation and cleanup outcomes.
        task = asyncio.create_task(capture(accepted()), name="messaging-sql-operation")
        return await join_owned_task(task, cancellation=cancellation)

    async def _prepare(self) -> None:
        if self._ready:
            return
        async with self._setup_lock:
            if self._ready:
                return
            async with SqlTransaction(
                self._engine, schema_lock="tinkerfin-messaging"
            ) as connection:
                await connection.run_sync(prepare_schema)
                row = (
                    (
                        await connection.execute(
                            select(capacity).where(capacity.c.id == 1)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    await connection.execute(
                        capacity.insert().values(
                            id=1,
                            settings=self._settings_json,
                            total_bytes=0,
                            total_records=0,
                        )
                    )
                else:
                    self._validate_capacity(row)
            self._ready = True

    def _clock(self) -> ColumnElement[datetime]:
        expression = {
            "sqlite": "strftime('%Y-%m-%d %H:%M:%f', 'now')",
            "mysql": "UTC_TIMESTAMP(6)",
            "postgresql": "timezone('UTC', clock_timestamp())",
        }[self._dialect]
        return literal_column(expression)

    def _validate_capacity(self, row: RowMapping) -> None:
        if text_value(row["settings"]) != self._settings_json:
            raise MessagingBackendProtocolError(
                "Messaging SQL deployment settings differ"
            )
        integer(row["total_bytes"])
        integer(row["total_records"])

    async def _read_point(self, connection: AsyncConnection) -> datetime:
        # The first MVCC row query establishes the snapshot and reads its storage
        # clock together. A clock-only query before any table read is insufficient.
        row = (
            (
                await connection.execute(
                    select(capacity, self._clock().label("observed_at")).where(
                        capacity.c.id == 1
                    )
                )
            )
            .mappings()
            .one()
        )
        self._validate_capacity(row)
        return utc_time(row["observed_at"])

    async def _write_point(self, connection: AsyncConnection) -> datetime:
        row = (
            (
                await connection.execute(
                    select(capacity).where(capacity.c.id == 1).with_for_update()
                )
            )
            .mappings()
            .one()
        )
        self._validate_capacity(row)
        # Lease time starts after the global admission lock has been acquired.
        return utc_time((await connection.execute(select(self._clock()))).scalar_one())

    async def _snapshot(
        self, connection: AsyncConnection, query: MessagingStateQuery, now: datetime
    ) -> MessagingStateSnapshot:
        scope = Scope(query.channel, query.identity)
        channel_row = (
            (await connection.execute(select(channels).where(scope.where(channels))))
            .mappings()
            .one_or_none()
        )
        channel = None
        if channel_row is not None:
            if decode_text(channel_row["channel"]) != query.channel:
                raise MessagingBackendProtocolError(
                    "Messaging SQL channel identity does not match its index"
                )
            channel = StoredMessagingChannel(
                query.channel,
                decode_text(channel_row["codec"]),
                self._settings.limits,
                self._settings.retention_policy,
            )
        thread_row = (
            (await connection.execute(select(threads).where(scope.where(threads))))
            .mappings()
            .one_or_none()
        )
        empty = MessagingStateSnapshot(now, channel, None, None, None, None)
        if thread_row is None:
            return empty
        if channel is None or text_value(thread_row["thread"]) != thread_key(
            query.identity
        ):
            raise MessagingBackendProtocolError(
                "Messaging SQL thread identity does not match its index"
            )
        generation = (
            query.generation
            if query.generation is not None
            else integer(thread_row["current_generation"])
        )
        row = (
            (
                await connection.execute(
                    select(generations).where(scope.where(generations, generation))
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            if query.generation is None or generation <= integer(
                thread_row["current_generation"]
            ):
                raise MessagingBackendProtocolError(
                    "Messaging SQL generation evidence is missing"
                )
            return empty
        disposition = text_value(row["disposition"])
        if disposition in {"deleted", "expired"}:
            return replace(
                empty,
                tombstone_generation=generation,
                tombstone_reason="deleted" if disposition == "deleted" else "expired",
            )
        if not live_disposition(disposition):
            raise MessagingBackendProtocolError(
                "Messaging SQL generation disposition is invalid"
            )
        deadline = row["retention_deadline"]
        stream = StoredMessagingStream(
            channel=query.channel,
            thread_id=query.identity.thread_id,
            generation=generation,
            disposition=disposition,
            latest_sequence=integer(row["latest_sequence"]),
            payload_bytes=integer(row["payload_bytes"]),
            next_producer_fence=integer(row["next_producer_fence"]),
            active_run_id=optional_decoded_text(row["active_run_id"]),
            retention_expired=deadline is not None and utc_time(deadline) <= now,
            message_sequence=integer(row["message_sequence"]),
            control_sequence=integer(row["control_sequence"]),
        )

        async def load_run(run_id: str) -> StoredMessagingRun | None:
            saved = (
                (
                    await connection.execute(
                        select(runs).where(
                            scope.where(runs, generation),
                            runs.c.run_id == digest(run_id),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if saved is None:
                return None
            result = stored_run(saved, scope, now)
            if result.identity.run_id != run_id:
                raise MessagingBackendProtocolError("Messaging SQL run hash collision")
            return result

        target = await load_run(query.identity.run_id)
        active = None
        if (
            query.include_active_run
            and stream.active_run_id is not None
            and stream.active_run_id != query.identity.run_id
        ):
            active = await load_run(stream.active_run_id)
            if active is None and disposition == "active":
                raise MessagingBackendProtocolError(
                    "Messaging SQL active producer is missing"
                )
        matching = None
        if query.message_id is not None:
            saved = (
                (
                    await connection.execute(
                        select(messages).where(
                            scope.where(messages, generation),
                            messages.c.message_id_hash == digest(query.message_id),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if saved is not None:
                matching = message_evidence(saved, scope, channel.codec_id)
                if matching.envelope.message_id != query.message_id:
                    raise MessagingBackendProtocolError(
                        "Messaging SQL message hash collision"
                    )
        return MessagingStateSnapshot(now, channel, stream, target, active, matching)

    async def _charge(
        self, connection: AsyncConnection, byte_delta: int, record_delta: int
    ) -> None:
        row = (
            (await connection.execute(select(capacity).where(capacity.c.id == 1)))
            .mappings()
            .one()
        )
        total_bytes, total_records = (
            integer(row["total_bytes"]) + byte_delta,
            integer(row["total_records"]) + record_delta,
        )
        if total_bytes < 0 or total_records < 0:
            raise MessagingBackendProtocolError(
                "Messaging SQL capacity accounting is inconsistent"
            )
        limits = self._settings.limits
        for resource, value, limit in (
            ("total_bytes", total_bytes, limits.max_total_bytes),
            ("total_records", total_records, limits.max_total_records),
        ):
            if value > limit:
                raise MessagingQuotaExceeded(resource=resource, limit=limit)
        await connection.execute(
            update(capacity)
            .where(capacity.c.id == 1)
            .values(total_bytes=total_bytes, total_records=total_records)
        )

    async def _apply(
        self,
        connection: AsyncConnection,
        scope: Scope,
        state: MessagingStateSnapshot,
        effect: MessagingStorageEffect,
        now: datetime,
    ) -> None:
        byte_delta = record_delta = 0
        if effect.channel is not None and state.channel is None:
            await connection.execute(
                channels.insert().values(
                    channel_id=scope.channel_id,
                    channel=encode_text(scope.channel),
                    codec=encode_text(effect.channel.codec_id),
                )
            )
            record_delta += 1
        stream = effect.stream or state.stream
        if stream is None:
            if (
                effect.runs
                or effect.message
                or effect.lease_action != "none"
                or effect.retention_action != "none"
            ):
                raise MessagingBackendProtocolError(
                    "Messaging SQL effect requires a generation"
                )
            return
        generation = stream.generation
        keys = scope.keys(generation)
        if effect.stream is not None:
            values = asdict(effect.stream)
            for name in ("channel", "thread_id", "generation", "retention_expired"):
                values.pop(name)
            values["active_run_id"] = (
                None
                if effect.stream.active_run_id is None
                else encode_text(effect.stream.active_run_id)
            )
            exists = (
                await connection.execute(
                    select(generations.c.generation).where(
                        scope.where(generations, generation)
                    )
                )
            ).first() is not None
            if exists:
                await connection.execute(
                    update(generations)
                    .where(scope.where(generations, generation))
                    .values(**values)
                )
            else:
                await connection.execute(
                    generations.insert().values(
                        **keys, **values, retention_deadline=None
                    )
                )
                record_delta += 1
                current = (
                    await connection.execute(
                        select(threads.c.current_generation).where(scope.where(threads))
                    )
                ).first()
                if current is None:
                    await connection.execute(
                        threads.insert().values(
                            **scope.keys(),
                            thread=thread_key(scope.identity),
                            current_generation=generation,
                        )
                    )
                    record_delta += 1
                else:
                    await connection.execute(
                        update(threads)
                        .where(scope.where(threads))
                        .values(current_generation=generation)
                    )
        lease_seconds = self._settings.producer_lease_seconds
        assert lease_seconds is not None
        lease_deadline = (now + timedelta(seconds=lease_seconds)).replace(tzinfo=None)
        for run in effect.runs:
            if (
                run.identity.thread != scope.identity.thread
                or run.generation != generation
            ):
                raise MessagingBackendProtocolError(
                    "Messaging SQL run replacement escapes its generation"
                )
            where = scope.where(runs, generation) & (
                runs.c.run_id == digest(run.identity.run_id)
            )
            old = (
                (await connection.execute(select(runs).where(where)))
                .mappings()
                .one_or_none()
            )
            if old is not None:
                previous = stored_run(old, scope, now)
                if previous.identity != run.identity:
                    raise MessagingBackendProtocolError(
                        "Messaging SQL run hash collision"
                    )
                byte_delta -= checkpoint_bytes(previous.checkpoint)
            else:
                record_delta += 1
            byte_delta += checkpoint_bytes(run.checkpoint)
            deadline = None if old is None else old["lease_deadline"]
            if run.producer_token is None:
                deadline = None
            elif effect.lease_run_id == run.identity.run_id and effect.lease_action in {
                "acquire",
                "renew",
            }:
                deadline = lease_deadline
            values: dict[str, object] = {
                "identity": run.identity.model_dump_json(by_alias=True),
                "start_sequence": run.start_sequence,
                "end_sequence": run.end_sequence,
                "status": run.status,
                "settlement_started": run.settlement_started,
                "cancellable": run.cancellable,
                "recoverable": run.recoverable,
                "producer_token": None
                if run.producer_token is None
                else encode_text(run.producer_token),
                "producer_fence": run.producer_fence,
                "lease_deadline": deadline,
                "publication_closed": run.publication_closed,
                "publication_ready": run.publication_ready,
                "request_digest": run.request_digest,
                "failure_class": encode_text(run.failure_class),
                "failure_message": encode_text(run.failure_message),
                **checkpoint_values(run.checkpoint),
            }
            if old is None:
                await connection.execute(
                    runs.insert().values(
                        **keys, run_id=digest(run.identity.run_id), **values
                    )
                )
            else:
                await connection.execute(update(runs).where(where).values(**values))
        if effect.lease_action != "none":
            if effect.lease_run_id is None:
                raise MessagingBackendProtocolError(
                    "Messaging SQL producer lease target is missing"
                )
            await connection.execute(
                update(runs)
                .where(
                    scope.where(runs, generation),
                    runs.c.run_id == digest(effect.lease_run_id),
                )
                .values(
                    lease_deadline=None
                    if effect.lease_action == "release"
                    else lease_deadline,
                )
            )
        if effect.message is not None:
            message = effect.message
            envelope = message.envelope
            await connection.execute(
                messages.insert().values(
                    **keys,
                    sequence=envelope.seq,
                    message_id_hash=digest(envelope.message_id),
                    message_id=encode_text(envelope.message_id),
                    identity=envelope.identity.model_dump_json(by_alias=True),
                    codec=encode_text(envelope.codec),
                    payload=envelope.payload,
                    created_at=envelope.created_at.replace(tzinfo=None),
                    signature=message.signature,
                    **checkpoint_values(message.checkpoint),
                )
            )
            record_delta += 1
            byte_delta += len(envelope.payload) + checkpoint_bytes(message.checkpoint)
        if effect.retention_action != "none":
            ttl = self._settings.retention_policy.terminal_ttl_seconds
            deadline = (
                None
                if effect.retention_action == "clear" or ttl is None
                else (now + timedelta(seconds=ttl)).replace(tzinfo=None)
            )
            await connection.execute(
                update(generations)
                .where(scope.where(generations, generation))
                .values(retention_deadline=deadline)
            )
        if effect.tombstone_reason is not None:
            if await self._private_records_exist(connection, scope, generation):
                raise MessagingBackendProtocolError(
                    "Messaging SQL generation cleanup is incomplete"
                )
            # Its allocated generation row becomes the tombstone; full capacity can
            # never prevent deletion from finishing or stale references from failing.
            await connection.execute(
                update(generations)
                .where(scope.where(generations, generation))
                .values(
                    retention_deadline=None,
                    active_run_id=None,
                    payload_bytes=0,
                )
            )
        if byte_delta or record_delta:
            await self._charge(connection, byte_delta, record_delta)

    async def _private_records_exist(
        self, connection: AsyncConnection, scope: Scope, generation: int
    ) -> bool:
        for table in (messages, runs):
            if (
                await connection.execute(
                    select(table.c.generation)
                    .where(scope.where(table, generation))
                    .limit(1)
                )
            ).first() is not None:
                return True
        return False

    async def _purge(
        self, connection: AsyncConnection, scope: Scope, generation: int, maximum: int
    ) -> StreamGenerationPurgeResult:
        maximum = min(maximum, 64)
        removed = released = 0
        # Each payload/checkpoint is counted once. Deleting by the selected primary
        # keys keeps both returned counts and capacity changes exact on repeated calls.
        # Only keys, lengths and bounded message identifiers cross the database
        # boundary; cleanup must not load a batch of large payloads into memory.
        for table, key in ((messages, "sequence"), (runs, "run_id")):
            if removed >= maximum:
                break
            selected = (
                (
                    await connection.execute(
                        select(
                            table.c[key],
                            func.coalesce(
                                func.length(table.c.checkpoint_position), 0
                            ).label("position_bytes"),
                            table.c.checkpoint_message_id,
                            (
                                func.length(table.c.payload)
                                if table is messages
                                else literal(0)
                            ).label("payload_bytes"),
                        )
                        .where(scope.where(table, generation))
                        .order_by(table.c[key])
                        .limit(maximum - removed)
                    )
                )
                .mappings()
                .all()
            )
            for row in selected:
                last_message_id = optional_decoded_text(row["checkpoint_message_id"])
                released += integer(row["position_bytes"]) + integer(
                    row["payload_bytes"]
                )
                released += len((last_message_id or "").encode())
            if selected:
                await connection.execute(
                    delete(table).where(
                        scope.where(table, generation),
                        table.c[key].in_([row[key] for row in selected]),
                    )
                )
                removed += len(selected)
        if removed:
            await self._charge(connection, -released, -removed)
        return StreamGenerationPurgeResult(
            removed,
            not await self._private_records_exist(connection, scope, generation),
        )

    async def _reclaim_expired(self, cancellation: Cancellation) -> None:
        async with SqlTransaction(self._engine, read_only=True) as connection:
            now = await self._read_point(connection)
            result = await connection.execute(
                select(
                    channels.c.channel,
                    threads.c.thread,
                    generations.c.channel_id,
                    generations.c.thread_id,
                )
                .select_from(
                    generations.join(
                        channels, generations.c.channel_id == channels.c.channel_id
                    ).join(
                        threads,
                        (generations.c.channel_id == threads.c.channel_id)
                        & (generations.c.thread_id == threads.c.thread_id)
                        & (generations.c.generation == threads.c.current_generation),
                    )
                )
                .where(
                    or_(
                        generations.c.retention_deadline <= now.replace(tzinfo=None),
                        generations.c.disposition == "expiring",
                    )
                )
                .order_by(
                    generations.c.retention_deadline,
                    generations.c.channel_id,
                    generations.c.thread_id,
                )
                .limit(16)
            )
            scopes: list[Scope] = []
            for row in result.mappings():
                try:
                    thread = ThreadIdentity.model_validate_json(
                        text_value(row["thread"])
                    )
                except ValueError as error:
                    raise MessagingBackendProtocolError(
                        "Messaging SQL expiry identity is invalid", cause=error
                    ) from error
                scope = Scope(
                    decode_text(row["channel"]),
                    RunIdentity(
                        namespace=thread.namespace,
                        thread_id=thread.thread_id,
                        run_id="expiry-cleanup",
                    ),
                )
                if scope.channel_id != binary(
                    row["channel_id"]
                ) or scope.thread_id != binary(row["thread_id"]):
                    raise MessagingBackendProtocolError(
                        "Messaging SQL expiry index escapes its thread"
                    )
                scopes.append(scope)
        for scope in scopes:
            _check_cancelled(cancellation)
            # Admission must not require user retries merely because an expired
            # generation exceeds one cleanup batch. Each batch commits separately
            # and checks cancellation; another worker can resume the indexed seal.
            while not await self._reclaim_scope(scope, cancellation):
                _check_cancelled(cancellation)

    async def _reclaim_scope(self, scope: Scope, cancellation: Cancellation) -> bool:
        async with SqlTransaction(self._engine) as connection:
            now = await self._write_point(connection)
            state = await self._snapshot(
                connection,
                MessagingStateQuery(
                    channel=scope.channel,
                    identity=scope.identity,
                    include_active_run=True,
                ),
                now,
            )
            stream = state.stream
            if stream is None or not (
                stream.retention_expired or stream.disposition == "expiring"
            ):
                return True
            intent = MessagingTransition(
                kind="begin_generation_cleanup",
                transition_id=uuid4().hex,
                channel=scope.channel,
                identity=scope.identity,
                settings=self._settings,
                cleanup_reason="expired",
            )
            effect = resolve_messaging_transition(intent, state)
            await self._apply(connection, scope, state, effect, now)
            if not effect.result.cleanup_required:
                return True
            state = replace(state, stream=effect.stream or state.stream)
            result = await self._purge(connection, scope, stream.generation, 64)
            if result.complete:
                finished = resolve_messaging_transition(
                    replace(
                        intent,
                        kind="finish_generation_cleanup",
                        cleanup_generation=stream.generation,
                    ),
                    state,
                )
                await self._apply(connection, scope, state, finished, now)
            _check_cancelled(cancellation)
            return result.complete

    @staticmethod
    def _readable(
        scope: Scope, generation: int, state: MessagingStateSnapshot
    ) -> StoredMessagingStream:
        stream = state.stream
        reason: MessagingCleanupReason = "deleted"
        if state.tombstone_reason == "expired" or (
            stream is not None
            and (
                stream.retention_expired
                or stream.disposition in {"expiring", "expired"}
            )
        ):
            reason = "expired"
        if (
            stream is None
            or stream.generation != generation
            or stream.disposition != "active"
            or stream.retention_expired
        ):
            error = StreamExpired if reason == "expired" else StreamDeleted
            raise error(
                channel=scope.channel, identity=scope.identity, generation=generation
            )
        return stream
