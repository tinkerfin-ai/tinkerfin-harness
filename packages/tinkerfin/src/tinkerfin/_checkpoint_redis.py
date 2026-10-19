"""Complete Redis checkpoint history and deletion without owning its connection.

AsyncRedisSaver 0.5.2 caps alist and adelete_thread at 10000 indexed rows. This
integration keeps its documents, indexes, serializers, and tuple loading intact.
Ordered pages bound client memory; exact key scans also cover orphan writes and
auxiliary keys that neither checkpoint index can enumerate.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast

from langgraph.checkpoint.base import CheckpointMetadata, CheckpointTuple, PendingWrite
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.redis.base import BaseRedisSaver
from langgraph.checkpoint.redis.key_registry import WRITE_KEYS_ZSET_PREFIX
from langgraph.checkpoint.redis.util import (
    from_storage_safe_id,
    from_storage_safe_str,
    to_storage_safe_id,
    to_storage_safe_str,
)
from redis.commands.search.aggregation import AggregateRequest, AggregateResult
from redis.commands.search.result import Result
from redisvl.query import CountQuery, FilterQuery
from redisvl.query.filter import FilterExpression, Tag

from ._checkpoint_writes import CheckpointWriteView
from .errors import TinkerFinLifecycleError

_PAGE_SIZE = 128
_ANCHOR_PATHS = (
    "$.thread_id",
    "$.checkpoint_ns",
    "$.checkpoint_id",
    "$.checkpoint.id",
    "$.metadata",
)


@dataclass(frozen=True, slots=True)
class _CheckpointLocation:
    thread_id: str
    graph_namespace: str
    checkpoint_id: str


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TinkerFinLifecycleError(
        "Redis checkpoint index returned an invalid coordinate"
    )


def _reply_fields(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise TinkerFinLifecycleError(
            "Redis checkpoint query returned an invalid mapping"
        )
    return {
        _text(key): value for key, value in cast(Mapping[object, object], raw).items()
    }


def _count(result: object) -> int:
    if isinstance(result, Result):
        value: object = vars(result).get("total")
    else:
        reply = _reply_fields(result)
        if reply.get("warning"):
            raise TinkerFinLifecycleError("Redis checkpoint count could not complete")
        value = reply.get("total_results")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TinkerFinLifecycleError("Redis checkpoint history count is invalid")
    return value


def _locations(result: object) -> list[_CheckpointLocation]:
    # redis-py returns result objects for RESP2 and maps for RESP3. This is a
    # transport boundary; the borrowed client's protocol never changes storage.
    if isinstance(result, AggregateResult):
        raw_rows: object = vars(result).get("rows")
    else:
        reply = _reply_fields(result)
        if reply.get("warning"):
            raise TinkerFinLifecycleError(
                "Redis checkpoint history query could not complete"
            )
        raw_rows = reply.get("results")
    if not isinstance(raw_rows, list):
        raise TinkerFinLifecycleError("Redis checkpoint history returned invalid rows")
    locations: list[_CheckpointLocation] = []
    for raw in cast(list[object], raw_rows):
        if isinstance(result, AggregateResult):
            if not isinstance(raw, list):
                raise TinkerFinLifecycleError(
                    "Redis checkpoint history returned an invalid row"
                )
            row = cast(list[object], raw)
            if len(row) != 6:
                raise TinkerFinLifecycleError(
                    "Redis checkpoint history returned an invalid coordinate count"
                )
            fields = {_text(row[index]): row[index + 1] for index in range(0, 6, 2)}
        else:
            fields = _reply_fields(_reply_fields(raw).get("extra_attributes"))
        if fields.keys() != {"thread_id", "checkpoint_ns", "checkpoint_id"}:
            raise TinkerFinLifecycleError("Redis checkpoint history lacks coordinates")
        locations.append(
            _CheckpointLocation(
                thread_id=from_storage_safe_id(_text(fields["thread_id"])),
                graph_namespace=from_storage_safe_str(_text(fields["checkpoint_ns"])),
                checkpoint_id=from_storage_safe_id(_text(fields["checkpoint_id"])),
            )
        )
    return locations


def _expression_literal(value: str) -> str:
    # RediSearch's unescapeStringDup removes repeated backslashes before
    # punctuation. Assemble backslashes as values so coordinates retain their
    # exact bytes; escaped newlines remain legal expression string literals.
    literals = [
        '"' + part.replace('"', '\\"').replace("\n", "\\\n") + '"'
        for part in value.split("\\")
    ]
    if len(literals) == 1:
        return literals[0]
    arguments: list[str] = []
    for literal in literals:
        if arguments:
            arguments.append('substr("a\\b", 1, 1)')
        arguments.append(literal)
    return 'format("' + "%s" * len(arguments) + '", ' + ", ".join(arguments) + ")"


async def checkpoint_writes(
    saver: AsyncRedisSaver,
    thread_id: str,
    graph_namespace: str,
    checkpoint_id: str,
    owner_prefix: str,
) -> CheckpointWriteView | None:
    """Read one checkpoint anchor and at most two matching receipt candidates.

    AsyncRedisSaver 0.5.2 indexes task_id and stores each pending write separately.
    Two candidates suffice to detect an immutable receipt conflict; no channel
    values, pending sends, or unrelated receipt payloads are fetched. Decode with
    the borrowed saver serializer and preserve its explicit-checkpoint TTL refresh
    rules, including the complete write-key set. Failure never triggers a broad
    read or a retry. NamespaceCheckpointer verifies logical identity and lineage.
    """

    checkpoint_key = saver._make_redis_checkpoint_key(
        thread_id, graph_namespace, checkpoint_id
    )
    pipeline = saver._redis.pipeline(transaction=False)
    # redis-py's JSON get omits its variadic path types. This read requests only
    # the exact checkpoint header, not any channel or write document.
    get_header = cast(
        Callable[[str, str, str, str, str, str], object],
        pipeline.json().get,  # pyright: ignore[reportUnknownMemberType]
    )
    get_header(checkpoint_key, *_ANCHOR_PATHS)
    refresh = bool(saver.ttl_config and saver.ttl_config.get("refresh_on_read"))
    if refresh:
        pipeline.ttl(checkpoint_key)
    responses: list[object] = await pipeline.execute()
    if not responses or responses[0] is None:
        return None
    selected = _reply_fields(responses[0])
    header: dict[str, object] = {}
    for path in _ANCHOR_PATHS:
        values = selected.get(path)
        if not isinstance(values, list):
            raise TinkerFinLifecycleError(
                "Redis checkpoint anchor field is unavailable"
            )
        entries = cast(list[object], values)
        if len(entries) != 1:
            raise TinkerFinLifecycleError(
                "Redis checkpoint anchor field is unavailable"
            )
        header[path[2:]] = entries[0]
    expected = {
        "thread_id": to_storage_safe_id(thread_id),
        "checkpoint_ns": to_storage_safe_str(graph_namespace),
        "checkpoint_id": to_storage_safe_id(checkpoint_id),
    }
    if any(header.get(field) != value for field, value in expected.items()):
        raise TinkerFinLifecycleError("Redis checkpoint write anchor changed identity")
    if header.get("checkpoint.id") != checkpoint_id:
        raise TinkerFinLifecycleError(
            "Redis checkpoint header has conflicting coordinates"
        )

    if refresh:
        ttl = responses[1] if len(responses) == 2 else None
        if not isinstance(ttl, int) or isinstance(ttl, bool):
            raise TinkerFinLifecycleError("Redis checkpoint TTL is invalid")
        if ttl > 0:
            registry = saver._key_registry
            keys = (
                []
                if registry is None
                else await registry.get_write_keys(
                    thread_id, graph_namespace, checkpoint_id
                )
            )
            await saver._apply_ttl_to_keys(checkpoint_key, keys or None)
            default_ttl = (
                None
                if saver.ttl_config is None
                else saver.ttl_config.get("default_ttl")
            )
            if registry is not None and default_ttl is not None:
                await registry.apply_ttl(
                    thread_id, graph_namespace, checkpoint_id, int(default_ttl * 60)
                )

    raw_metadata = header.get("metadata")
    metadata = _reply_fields(
        json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    )
    sanitized = {
        key.replace("\u0000", ""): value.replace("\u0000", "")
        if isinstance(value, str)
        else value
        for key, value in metadata.items()
    }
    selection = cast(
        FilterExpression,
        (Tag("thread_id") == expected["thread_id"])
        & (Tag("checkpoint_ns") == expected["checkpoint_ns"])
        & (Tag("checkpoint_id") == expected["checkpoint_id"])
        & (Tag("task_id") % (owner_prefix + "*")),
    )
    query = FilterQuery(
        filter_expression=selection,
        return_fields=[
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
            "channel",
            "type",
            "$.blob",
        ],
        num_results=2,
    )
    search = cast(
        Callable[[FilterQuery], Awaitable[object]],
        saver.checkpoint_writes_index.search,  # pyright: ignore[reportUnknownMemberType]
    )
    result = await search(query)
    total = _count(result)
    if isinstance(result, Result):
        documents = cast(list[object], result.docs)  # pyright: ignore[reportUnknownMemberType]
        rows = [_reply_fields(vars(document)) for document in documents]
    else:
        raw_rows = _reply_fields(result).get("results")
        if not isinstance(raw_rows, list):
            raise TinkerFinLifecycleError("Redis receipt query returned invalid rows")
        rows = [
            _reply_fields(_reply_fields(row).get("extra_attributes"))
            for row in cast(list[object], raw_rows)
        ]
    if len(rows) != min(total, 2):
        raise TinkerFinLifecycleError(
            "Redis receipt query returned incomplete evidence"
        )
    writes: list[PendingWrite] = []
    for row in rows:
        if any(_text(row.get(field)) != value for field, value in expected.items()):
            raise TinkerFinLifecycleError(
                "Redis receipt query crossed checkpoint identity"
            )
        owner = _text(row.get("task_id"))
        if not owner.startswith(owner_prefix):
            raise TinkerFinLifecycleError(
                "Redis receipt query crossed its selected owner"
            )
        index = row.get("idx")
        if isinstance(index, bool) or index not in (0, "0", b"0"):
            raise TinkerFinLifecycleError("Redis receipt has an invalid write index")
        blob = row.get("$.blob")
        if not isinstance(blob, (str, bytes)):
            raise TinkerFinLifecycleError("Redis receipt payload is unavailable")
        value = saver.serde.loads_typed(
            (_text(row.get("type")), BaseRedisSaver._decode_blob_static(blob))
        )
        writes.append((owner, _text(row.get("channel")), value))
    return CheckpointWriteView(
        thread_id,
        graph_namespace,
        checkpoint_id,
        cast(CheckpointMetadata, sanitized),
        tuple(writes),
    )


async def checkpoint_history(
    saver: AsyncRedisSaver, physical_thread: str
) -> AsyncGenerator[CheckpointTuple, None]:
    """Yield every indexed checkpoint in descending ID order using bounded pages.

    Snapshot history requires the caller to keep the index stable. Count changes
    and server truncation fail explicitly. No server cursor remains open between
    pages. Reads preserve pending writes, serialization, and the saver's TTL policy.

    Args:
        saver: Open Redis saver whose connection remains owned by the caller.
        physical_thread: Framework-issued hexadecimal thread key in this storage domain.

    Yields:
        Exact indexed checkpoints with their upstream physical coordinates.

    Raises:
        TinkerFinLifecycleError: The index response, count, order, or coordinates
            cannot prove complete history.
    """

    index = saver.checkpoints_index
    # Physical threads are framework-issued SHA256 hex, never raw business IDs.
    selection = FilterExpression(f"@thread_id:{{{physical_thread}}}")
    # RedisVL's annotations omit parameters and RESP3 result shapes. Narrow each
    # external response before using counts or checkpoint coordinates.
    search = cast(Callable[[CountQuery], Awaitable[object]], index.search)  # pyright: ignore[reportUnknownMemberType]
    aggregate = cast(
        Callable[[AggregateRequest], Awaitable[object]],
        index.aggregate,  # pyright: ignore[reportUnknownMemberType]
    )
    total = _count(await search(CountQuery(selection)))
    count = 0
    previous: _CheckpointLocation | None = None
    while True:
        query = AggregateRequest(str(selection)).load(
            "@thread_id", "@checkpoint_ns", "@checkpoint_id"
        )
        if previous is not None:
            # The complete coordinate keeps equal checkpoint IDs in different
            # graphs distinct without exposing unescaped expression syntax.
            checkpoint_id = _expression_literal(
                to_storage_safe_id(previous.checkpoint_id)
            )
            graph_namespace = _expression_literal(
                to_storage_safe_str(previous.graph_namespace)
            )
            query.filter(
                f"@checkpoint_id < {checkpoint_id} || "
                f"(@checkpoint_id == {checkpoint_id} && @checkpoint_ns < {graph_namespace})"
            )
        # Redis caps a single sorted aggregate, even with WITHCURSOR. Every query
        # selects only the next bounded page, without growing offsets.
        # redis-py leaves **kwargs untyped; AggregateRequest.sort_by accepts max.
        query.sort_by(  # pyright: ignore[reportUnknownMemberType]
            "@checkpoint_id", "DESC", "@checkpoint_ns", "DESC", max=_PAGE_SIZE
        )
        query.limit(0, _PAGE_SIZE)
        page = await aggregate(query)
        locations = _locations(page)
        if not locations:
            break
        for location in locations:
            if location.thread_id != physical_thread:
                raise TinkerFinLifecycleError(
                    "Redis checkpoint index crossed thread identity"
                )
            coordinate = (
                location.checkpoint_id,
                to_storage_safe_str(location.graph_namespace),
            )
            if previous is not None and coordinate >= (
                previous.checkpoint_id,
                to_storage_safe_str(previous.graph_namespace),
            ):
                raise TinkerFinLifecycleError(
                    "Redis checkpoint history is not strictly ordered"
                )
            previous = location
            count += 1
            saved = await saver.aget_tuple(
                {
                    "configurable": {
                        "thread_id": physical_thread,
                        "checkpoint_ns": location.graph_namespace,
                        "checkpoint_id": location.checkpoint_id,
                    }
                }
            )
            if saved is None:
                raise TinkerFinLifecycleError(
                    "Redis checkpoint history changed while reading"
                )
            coordinates = saved.config.get("configurable", {})
            if (
                coordinates.get("thread_id") != physical_thread
                or coordinates.get("checkpoint_ns", "") != location.graph_namespace
                or coordinates.get("checkpoint_id") != location.checkpoint_id
                or saved.checkpoint["id"] != location.checkpoint_id
            ):
                raise TinkerFinLifecycleError(
                    "Redis checkpoint lookup returned another indexed location"
                )
            yield saved
    remaining_total = _count(await search(CountQuery(selection)))
    if count != total or remaining_total != total:
        raise TinkerFinLifecycleError(
            "Redis checkpoint history is incomplete or changed while reading",
            diagnostic_context={"expected_count": total, "actual_count": count},
        )


def _scan_pattern(prefix: str) -> str:
    return "".join("\\" + char if char in "\\*?[]" else char for char in prefix) + "*"


async def delete_checkpoint_thread(
    saver: AsyncRedisSaver, physical_thread: str
) -> None:
    """Remove exact thread keys after the caller has stopped every thread writer.

    Redis SCAN guarantees visiting keys that remain present throughout an entire
    iteration. Removing only yielded keys preserves that guarantee; offset-based
    index deletion does not. Each pipeline contains bounded single-key commands,
    so a cluster can route them independently. Failure or cancellation may leave
    partial deletion, which is safe to repeat. The borrowed client remains open.

    Args:
        saver: Open saver whose configured key prefixes identify the storage domain.
        physical_thread: Framework-issued hexadecimal thread key to remove.

    Raises:
        TinkerFinLifecycleError: A returned key crosses the selected thread or
            matching keys remain after deletion.
    """

    # These are the four key families in BaseRedisSaver and CheckpointKeyRegistry.
    # A terminating separator and escaped configured prefix prevent adjacent
    # threads or literal Redis glob characters from extending the deletion scope.
    prefixes = (
        f"{saver._checkpoint_prefix}:{physical_thread}:",
        f"{saver._checkpoint_write_prefix}:{physical_thread}:",
        f"{saver._checkpoint_prefix}_latest:{physical_thread}:",
        f"{WRITE_KEYS_ZSET_PREFIX}:{physical_thread}:",
    )
    client = saver._redis
    # redis-py's scan iterator omits its item type. Validate each external value
    # before using it as a key, including values returned by a cluster client.
    scan = cast(Callable[..., AsyncIterator[object]], client.scan_iter)  # pyright: ignore[reportUnknownMemberType]

    async def remove(keys: list[str | bytes]) -> None:
        async with client.pipeline(transaction=False) as pipeline:
            for key in keys:
                pipeline.delete(key)
            await pipeline.execute()

    for prefix in prefixes:
        keys: list[str | bytes] = []
        async for raw_key in scan(match=_scan_pattern(prefix), count=_PAGE_SIZE):
            key = _text(raw_key)
            if not key.startswith(prefix):
                raise TinkerFinLifecycleError(
                    "Redis checkpoint scan crossed thread identity"
                )
            keys.append(key)
            if len(keys) == _PAGE_SIZE:
                await remove(keys)
                keys = []
        if keys:
            await remove(keys)
    for prefix in prefixes:
        async for _key in scan(match=_scan_pattern(prefix), count=_PAGE_SIZE):
            raise TinkerFinLifecycleError(
                "Redis checkpoint deletion is incomplete; stop thread writers before retrying"
            )
