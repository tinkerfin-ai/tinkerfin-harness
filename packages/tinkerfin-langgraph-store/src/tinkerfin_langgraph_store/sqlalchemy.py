"""Asynchronous LangGraph memory on a caller-owned SQLAlchemy Engine."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

from langgraph.store.base import (
    BaseStore,
    Item,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)
from sqlalchemy import and_, case, delete, exists, or_, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from tinkerfin_contracts.storage import DocumentSnapshot
from tinkerfin_sqlalchemy import (
    SqlTransaction,
    engine_dialect,
    mysql_error_code,
    postgresql_error_code,
    sqlite_lock_error,
)

from ._lifetime import StoreLifetime
from ._operations import (
    Filter,
    Get,
    JsonValue,
    ListNamespaces,
    Operation,
    Put,
    Search,
    decode_namespace,
    digest,
    encode_json,
    equality_key,
    json_object,
    namespace_text,
    number_operand,
    prepare_operations,
)
from ._sql_schema import (
    documents,
    fields,
    namespaces,
    paths,
    prepare_schema,
)
from .errors import StoreCorruptionError, StoreDriverError


@dataclass(frozen=True, slots=True)
class _ExpectedDocument:
    namespace: tuple[str, ...]
    key: str
    value: dict[str, JsonValue] | None


class _WriteConflict(Exception):
    """Roll back the complete transaction when a write condition is stale."""


def _condition_value(value: dict[str, JsonValue]) -> str:
    # Object order does not change JSON values; arrays and scalar types do.
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StoreCorruptionError("A stored text value has an invalid type")
    return value


def _bytes(value: object) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if not isinstance(value, bytes):
        raise StoreCorruptionError("A stored identity has an invalid type")
    return value


def _time(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise StoreCorruptionError("A stored timestamp has an invalid type")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _namespace_id(namespace: tuple[str, ...]) -> bytes:
    return digest(namespace_text(namespace))


def _key_text(key: str) -> bytes:
    return key.encode("utf-8").hex().encode("ascii")


def _document_where(namespace: tuple[str, ...], key: str) -> ColumnElement[bool]:
    return and_(
        documents.c.namespace_id == _namespace_id(namespace),
        documents.c.key_id == digest(key),
    )


def _item(row: Mapping[str, object], *, search: bool = False) -> Item:
    try:
        namespace = decode_namespace(_bytes(row["namespace"]).decode("ascii"))
        key = bytes.fromhex(_bytes(row["key"]).decode("ascii")).decode("utf-8")
        if _namespace_id(namespace) != _bytes(row["namespace_id"]) or digest(
            key
        ) != _bytes(row["key_id"]):
            raise StoreCorruptionError(
                "Stored document identity does not match its index"
            )
        value = json_object(json.loads(_text(row["value"])))
        created, updated = _time(row["created_at"]), _time(row["updated_at"])
    except (TypeError, ValueError, OverflowError) as error:
        raise StoreCorruptionError(
            "Stored document data is invalid", cause=error
        ) from error
    cls = SearchItem if search else Item
    return cls(
        namespace=namespace,
        key=key,
        value=value,
        created_at=created,
        updated_at=updated,
    )


def _filter_expression(condition: Filter) -> ColumnElement[bool]:
    index = fields.alias()
    equality = equality_key(condition.value)
    if condition.comparison in {"$eq", "$ne"}:
        comparison = and_(
            index.c.value_id == digest(equality), index.c.value == equality
        )
        if condition.comparison == "$ne":
            comparison = ~comparison
    else:
        operand = number_operand(condition.value)
        if condition.comparison in {"$gt", "$gte"}:
            finite = (
                index.c.number > operand
                if condition.comparison == "$gt"
                else index.c.number >= operand
            )
            comparison = or_(
                index.c.number_range == 1, and_(index.c.number_range == 0, finite)
            )
        else:
            finite = (
                index.c.number < operand
                if condition.comparison == "$lt"
                else index.c.number <= operand
            )
            comparison = or_(
                index.c.number_range == -1, and_(index.c.number_range == 0, finite)
            )
    return exists(
        select(1).where(
            index.c.namespace_id == documents.c.namespace_id,
            index.c.key_id == documents.c.key_id,
            index.c.field_id == digest(condition.field),
            index.c.field == condition.field.encode("utf-8").hex(),
            comparison,
        )
    ).correlate(documents)


def _prefix_expression(prefix: tuple[str, ...]) -> ColumnElement[bool]:
    encoded = namespace_text(prefix).encode("ascii")
    # Dot is the complete-label separator and slash is its immediate ASCII
    # successor. A binary range matches descendants without LIKE coercion on BYTEA.
    return or_(
        namespaces.c.namespace == encoded,
        and_(
            namespaces.c.namespace >= encoded + b".",
            namespaces.c.namespace < encoded + b"/",
        ),
    )


def _field_values(
    namespace_id: bytes, key_id: bytes, value: Mapping[str, JsonValue]
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    field_ids: set[bytes] = set()
    for field, item in value.items():
        field_id = digest(field)
        if field_id in field_ids:
            raise StoreCorruptionError("Document field digest collision")
        field_ids.add(field_id)
        number_range: int | None = None
        number: float | None = None
        if isinstance(item, int | float) and not isinstance(item, bool):
            try:
                number = float(item)
            except OverflowError:
                number_range = 1 if item > 0 else -1
            else:
                number_range = 0 if math.isfinite(number) else 1 if number > 0 else -1
                if number_range:
                    number = None
        equality = equality_key(item)
        result.append(
            {
                "namespace_id": namespace_id,
                "key_id": key_id,
                "field_id": field_id,
                "field": field.encode("utf-8").hex(),
                "value_id": digest(equality),
                "value": equality,
                "number_range": number_range,
                "number": number,
            }
        )
    return result


class SqlAlchemyStore(BaseStore):
    """Persist LangGraph memory using asynchronous SQLite, MySQL, or PostgreSQL.

    Pass an AsyncEngine and use LangGraph's aget/aput/asearch/alist_namespaces or
    abatch methods. Setup runs automatically before the first operation; setup()
    is also available for startup validation. Each batch reads one snapshot before
    applying its writes, and commits all writes together. Last write wins for a
    repeated complete key. Updates preserve the first creation time.

    Search supports literal namespace prefixes and typed top-level JSON filters.
    Results sort by update time descending, then namespace and key ascending.
    Namespace listing filters complete paths before truncation and pagination.
    Database payload and resource limits apply to namespace, key, and document
    sizes. TTL, semantic queries, vector indexing, and sync I/O are rejected.

    The Engine and its timeouts belong to the caller. Closing stops new operations
    and waits for accepted work, including when the close waiter is cancelled.
    Interrupted database work is cleaned up before cancellation propagates. A
    failed commit acknowledgement is never replayed. Known database transaction
    conflicts may repeat the internal batch at most three times.
    Accepted initialization finishes before caller cancellation is delivered, so
    cancelling setup cannot leave a partially created MySQL schema.

    Args:
        engine: Borrowed asynchronous Engine; its connection pool must provide
            exclusive checkouts. Select and install the asynchronous driver yourself.

    Raises:
        TypeError: The Engine cannot provide the required asynchronous ownership.
        ValueError: The Engine dialect is unsupported.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Validate the borrowed Engine without opening a connection."""

        super().__init__()
        SqlTransaction(engine)
        self._engine = engine
        self._dialect = engine_dialect(engine)
        self._lifetime = StoreLifetime()
        self._setup_task: asyncio.Task[BaseException | None] | None = None

    async def __aenter__(self) -> Self:
        """Validate storage and return the Store for an explicitly closed scope."""

        await self.setup()
        return self

    async def __aexit__(
        self,
        _type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Wait for accepted operations without closing the caller's Engine."""

        try:
            await self.aclose()
        except asyncio.CancelledError:
            if error is not None and not isinstance(
                error, Exception | asyncio.CancelledError
            ):
                # The cancelled waiter becomes context, leaving the body's
                # existing explicit cause intact. Python removes context cycles.
                raise error
            raise

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """Reject synchronous I/O; use abatch or the asynchronous convenience methods."""

        raise NotImplementedError("SqlAlchemyStore supports async operations only")

    async def setup(self) -> None:
        """Create empty storage or validate its complete current tables.

        Raises:
            StoreClosedError: The Store no longer accepts work.
            StoreSchemaError: Existing tables differ from the required structure.
            StoreDriverError: Database initialization fails.
        """

        with self._lifetime.operation():
            await self._setup()

    async def _setup(self) -> None:
        task = self._setup_task
        if task is None or (
            task.done() and (task.cancelled() or task.result() is not None)
        ):
            # MySQL commits each DDL statement. Accepted setup owns the complete
            # schema operation even when callers cancel. Selecting this shared task
            # has no await point; StoreLifetime already enforces a single event loop.
            # Each accepted waiter joins it, without holding a local lock during I/O.
            task = asyncio.create_task(
                self._initialize(), name="tinkerfin-langgraph-store-setup"
            )
            self._setup_task = task
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        try:
            failure = task.result()
        except asyncio.CancelledError as error:
            failure = error
        if cancellation is not None:
            try:
                raise cancellation
            except asyncio.CancelledError:
                if failure is not None:
                    if not isinstance(failure, Exception):
                        raise failure
                    raise cancellation from failure
                raise
        if failure is not None:
            raise failure

    async def _initialize(self) -> BaseException | None:
        """Capture setup failures so process control is raised by the owning caller."""

        try:
            async with SqlTransaction(
                self._engine, schema_lock="tinkerfin_store_schema"
            ) as connection:
                await connection.run_sync(prepare_schema)
        except SQLAlchemyError as error:
            return StoreDriverError("Store initialization failed", cause=error)
        except BaseException as error:  # noqa: BLE001 - the joined owner re-raises control failures
            return error
        return None

    async def aclose(self) -> None:
        """Stop new operations and wait for accepted work; never dispose the Engine."""

        await self._lifetime.close()

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """Read one snapshot, then atomically apply the final write for every key.

        Args:
            ops: Operations consumed exactly once; results retain their input order.

        Returns:
            One LangGraph result for each operation, including None for writes.

        Raises:
            TypeError: An operation or JSON value has the wrong type.
            ValueError: An identity, filter, or pagination value is invalid.
            NotImplementedError: TTL, vector indexing, or semantic search is requested.
            StoreClosedError: The Store is closing or closed.
            StoreCorruptionError: Stored identities or documents are inconsistent.
            StoreDriverError: The database operation failed after cleanup.
            StoreSchemaError: Existing tables differ from the required structure.
        """

        return await self._execute(ops)

    async def acompare_and_set(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        expected: dict[str, JsonValue] | None,
        value: dict[str, JsonValue] | None,
    ) -> bool:
        """Commit a create, replacement or deletion only against the read value.

        The comparison and write share the provider's transaction and namespace
        lock. Independent workers cannot both replace one observed value. None
        selects absence for the condition and deletion for the replacement.
        JSON types are compared exactly, including bool versus integer.

        Args:
            namespace: Complete document namespace, with ordinary Store rules.
            key: Document key within that namespace.
            expected: Observed JSON document, or None for an absent document.
            value: Replacement JSON document, or None to delete.

        Returns:
            True after commit; False if the condition no longer matches.

        Raises:
            TypeError: An identity or JSON value has the wrong type.
            ValueError: An identity or JSON value is invalid.
            LangGraphStoreError: The Store or database cannot complete the operation.
        """
        condition = _ExpectedDocument(
            namespace, key, None if expected is None else json_object(expected)
        )
        try:
            await self._execute((PutOp(namespace, key, value),), condition)
        except _WriteConflict:
            return False
        return True

    async def asearch_exact(
        self,
        namespace: tuple[str, ...],
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentSnapshot]:
        """Read a page from one namespace, excluding descendants before paging.

        Uses the same ordering, validation, cancellation and borrowed Engine
        lifetime as asearch. The namespace must identify a document collection.

        Args:
            namespace: Complete collection namespace.
            limit: Nonnegative maximum number of documents returned.
            offset: Nonnegative number of documents skipped in this collection.

        Returns:
            Independent snapshots sorted by update time descending, then key.

        Raises:
            ValueError: The namespace or pagination is invalid.
            TypeError: An input has the wrong type.
            LangGraphStoreError: The Store or database cannot complete the query.
        """
        if not namespace:
            raise ValueError("Document namespace cannot be empty")
        results = await self._execute(
            (SearchOp(namespace, limit=limit, offset=offset),), exact=True
        )
        items = results[0]
        assert isinstance(items, list)
        snapshots: list[DocumentSnapshot] = []
        for item in items:
            assert isinstance(item, SearchItem)
            snapshots.append(
                DocumentSnapshot(
                    item.key, json_object(item.value), item.created_at, item.updated_at
                )
            )
        return snapshots

    async def _execute(
        self,
        ops: Iterable[Op],
        condition: _ExpectedDocument | None = None,
        *,
        exact: bool = False,
    ) -> list[Result]:
        with self._lifetime.operation():
            operations = prepare_operations(ops)
            if not operations:
                return []
            await self._setup()
            writes = {
                (op.namespace, op.key): op for op in operations if isinstance(op, Put)
            }
            for attempt in range(3):
                transaction = SqlTransaction(
                    self._engine,
                    read_only=not writes,
                    isolation_level="REPEATABLE READ",
                )
                try:
                    async with transaction as connection:
                        return await self._batch(
                            connection,
                            operations,
                            tuple(writes.values()),
                            condition,
                            exact=exact,
                        )
                except SQLAlchemyError as error:
                    # Only replay internal work when no COMMIT was acknowledged or
                    # left uncertain. A return from _batch still exits/commits this
                    # context before the caller can observe its snapshot results.
                    retryable = isinstance(error, DBAPIError) and (
                        sqlite_lock_error(error)
                        or mysql_error_code(error) in {1062, 1205, 1213}
                        or postgresql_error_code(error) in {"23505", "40001", "40P01"}
                    )
                    if (
                        not retryable
                        or transaction.committed
                        or transaction.commit_uncertain
                        or transaction.cleanup_failed
                        or attempt == 2
                    ):
                        raise StoreDriverError(
                            "Store database operation failed", cause=error
                        ) from error
            raise AssertionError("the bounded Store transaction loop must settle")

    async def _batch(
        self,
        connection: AsyncConnection,
        operations: Sequence[Operation],
        writes: Sequence[Put],
        condition: _ExpectedDocument | None = None,
        *,
        exact: bool = False,
    ) -> list[Result]:
        changed_namespaces = sorted({op.namespace for op in writes})
        # One lock per affected namespace also protects removal of its final
        # document. Stable ordering bounds cross-namespace lock conflicts.
        for namespace in changed_namespaces:
            await self._lock_namespace(connection, namespace)
        if condition is not None:
            current = await self._get(
                connection, Get(condition.namespace, condition.key)
            )
            actual = (
                None
                if current is None
                else _condition_value(json_object(current.value))
            )
            expected = (
                None if condition.value is None else _condition_value(condition.value)
            )
            if actual != expected:
                raise _WriteConflict
        result: list[Result] = []
        for op in operations:
            if isinstance(op, Get):
                result.append(await self._get(connection, op))
            elif isinstance(op, Search):
                result.append(await self._search(connection, op, exact=exact))
            elif isinstance(op, ListNamespaces):
                result.append(await self._list(connection, op))
            else:
                result.append(None)
        if writes:
            now = await self._now(connection)
            for op in writes:
                await self._put(connection, op, now)
            for namespace in changed_namespaces:
                namespace_id = _namespace_id(namespace)
                remaining = await connection.scalar(
                    select(documents.c.key_id)
                    .where(documents.c.namespace_id == namespace_id)
                    .limit(1)
                )
                if remaining is None:
                    await connection.execute(
                        delete(paths).where(paths.c.namespace_id == namespace_id)
                    )
                    await connection.execute(
                        delete(namespaces).where(
                            namespaces.c.namespace_id == namespace_id
                        )
                    )
        return result

    async def _lock_namespace(
        self, connection: AsyncConnection, namespace: tuple[str, ...]
    ) -> None:
        encoded, namespace_id = namespace_text(namespace), _namespace_id(namespace)
        row = (
            (
                await connection.execute(
                    select(namespaces)
                    .where(namespaces.c.namespace_id == namespace_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is not None:
            if _bytes(row["namespace"]) != encoded.encode("ascii"):
                raise StoreCorruptionError("Namespace digest collision")
            return
        await connection.execute(
            namespaces.insert().values(
                namespace_id=namespace_id,
                namespace=encoded.encode("ascii"),
                depth=len(namespace),
            )
        )
        await connection.execute(
            paths.insert(),
            [
                {
                    "namespace_id": namespace_id,
                    "depth": depth,
                    "prefix": namespace_text(namespace[:depth]).encode("ascii"),
                    "label": ""
                    if depth == 0
                    else namespace[depth - 1].encode("utf-8").hex(),
                }
                for depth in range(len(namespace) + 1)
            ],
        )

    async def _get(self, connection: AsyncConnection, op: Get) -> Item | None:
        row = (
            (
                await connection.execute(
                    select(documents, namespaces.c.namespace)
                    .outerjoin(
                        namespaces,
                        namespaces.c.namespace_id == documents.c.namespace_id,
                    )
                    .where(_document_where(op.namespace, op.key))
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        result = _item(dict(row))
        if result.namespace != op.namespace or result.key != op.key:
            raise StoreCorruptionError("Document identity digest collision")
        return result

    async def _search(
        self, connection: AsyncConnection, op: Search, *, exact: bool = False
    ) -> list[SearchItem]:
        query = select(documents, namespaces.c.namespace).join(
            namespaces, namespaces.c.namespace_id == documents.c.namespace_id
        )
        if exact:
            query = query.where(documents.c.namespace_id == _namespace_id(op.prefix))
        elif op.prefix:
            query = query.where(_prefix_expression(op.prefix))
        for condition in op.filters:
            query = query.where(_filter_expression(condition))
        query = (
            query.order_by(
                documents.c.updated_at.desc(), namespaces.c.namespace, documents.c.key
            )
            .limit(op.limit)
            .offset(op.offset)
        )
        result: list[SearchItem] = []
        for row in (await connection.execute(query)).mappings():
            item = _item(dict(row), search=True)
            assert isinstance(item, SearchItem)
            if exact and item.namespace != op.prefix:
                raise StoreCorruptionError("Namespace digest collision")
            result.append(item)
        return result

    async def _list(
        self, connection: AsyncConnection, op: ListNamespaces
    ) -> list[tuple[str, ...]]:
        if op.max_depth is None:
            selected = namespaces.c.namespace
            query = select(selected)
        else:
            selected = paths.c.prefix
            depth = case(
                (namespaces.c.depth <= op.max_depth, namespaces.c.depth),
                else_=op.max_depth,
            )
            query = select(selected).select_from(
                namespaces.join(
                    paths,
                    and_(
                        paths.c.namespace_id == namespaces.c.namespace_id,
                        paths.c.depth == depth,
                    ),
                )
            )
        query = query.where(
            exists(
                select(1).where(documents.c.namespace_id == namespaces.c.namespace_id)
            ).correlate(namespaces)
        )
        for direction, labels in op.conditions:
            query = query.where(namespaces.c.depth >= len(labels))
            for index, label in enumerate(labels, start=1):
                if label == "*":
                    continue
                path = paths.alias()
                depth = (
                    index
                    if direction == "prefix"
                    else namespaces.c.depth - len(labels) + index
                )
                query = query.where(
                    exists(
                        select(1).where(
                            path.c.namespace_id == namespaces.c.namespace_id,
                            path.c.depth == depth,
                            path.c.label == label.encode("utf-8").hex(),
                        )
                    ).correlate(namespaces)
                )
        query = query.distinct().order_by(selected).limit(op.limit).offset(op.offset)
        try:
            return [
                decode_namespace(_bytes(value).decode("ascii"))
                for value in (await connection.execute(query)).scalars()
            ]
        except (ValueError, UnicodeError) as error:
            raise StoreCorruptionError(
                "Stored namespace data is invalid", cause=error
            ) from error

    async def _put(self, connection: AsyncConnection, op: Put, now: datetime) -> None:
        namespace_id, key_id = _namespace_id(op.namespace), digest(op.key)
        row = (
            (
                await connection.execute(
                    select(documents)
                    .where(_document_where(op.namespace, op.key))
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is not None:
            # A replacement must not hide invalid evidence in the selected current
            # document. Namespace ownership was checked while acquiring its lock.
            current: dict[str, object] = dict(row)
            current["namespace"] = namespace_text(op.namespace).encode("ascii")
            item = _item(current)
            if item.key != op.key:
                raise StoreCorruptionError("Document key digest collision")
        await connection.execute(
            delete(fields).where(
                fields.c.namespace_id == namespace_id, fields.c.key_id == key_id
            )
        )
        if op.value is None:
            await connection.execute(
                delete(documents).where(_document_where(op.namespace, op.key))
            )
            return
        values = {"value": encode_json(op.value), "updated_at": now}
        if row is None:
            await connection.execute(
                documents.insert().values(
                    namespace_id=namespace_id,
                    key_id=key_id,
                    key=_key_text(op.key),
                    created_at=now,
                    **values,
                )
            )
        else:
            await connection.execute(
                documents.update()
                .where(_document_where(op.namespace, op.key))
                .values(**values)
            )
        indexed_fields = _field_values(namespace_id, key_id, op.value)
        if indexed_fields:
            await connection.execute(fields.insert(), indexed_fields)

    async def _now(self, connection: AsyncConnection) -> datetime:
        statement = {
            "sqlite": "SELECT STRFTIME('%Y-%m-%d %H:%M:%f', 'now')",
            "mysql": "SELECT UTC_TIMESTAMP(6)",
            "postgresql": "SELECT timezone('UTC', clock_timestamp())",
        }[self._dialect]
        return _time(await connection.scalar(text(statement))).replace(tzinfo=None)
