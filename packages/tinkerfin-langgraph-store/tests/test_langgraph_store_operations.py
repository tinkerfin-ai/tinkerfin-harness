"""Database-independent LangGraph memory behavior through public operations."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable
from typing import Any

import pytest
from langgraph.store.base import (
    GetOp,
    InvalidNamespaceError,
    Item,
    ListNamespacesOp,
    MatchCondition,
    Op,
    PutOp,
    SearchItem,
    SearchOp,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_langgraph_store import SqlAlchemyStore, StoreClosedError


async def test_complete_unicode_identities_roundtrip_and_creation_time(
    store: SqlAlchemyStore,
) -> None:
    namespace_values = [
        ("scope",),
        ("scope ",),
        ("Scope",),
        ("é",),
        ("é",),
        ("%_*/", "普通🙂 "),
        ("a", "b"),
        ("aa",),
        ("a", "b "),
        ("nul\0label",),
    ]
    keys = ["key", "Key", "key ", "é", "é", "", "nul\0key"]
    value = {"nested": {"items": [True, None, 2**80, 3.5, "字🙂\0"]}}
    await store.abatch(
        PutOp(namespace, key, value) for namespace in namespace_values for key in keys
    )
    for namespace in namespace_values:
        for key in keys:
            item = await store.aget(namespace, key)
            assert item is not None and item.namespace == namespace and item.key == key
            assert item.value == value and item.created_at.tzinfo is not None
    before = await store.aget(namespace_values[0], keys[0])
    await store.aput(namespace_values[0], keys[0], {"changed": True})
    after = await store.aget(namespace_values[0], keys[0])
    assert before is not None and after is not None
    assert after.created_at == before.created_at
    assert after.updated_at.tzinfo is not None
    assert after.value == {"changed": True}
    await store.adelete(namespace_values[0], keys[0])
    assert await store.aget(namespace_values[0], keys[0]) is None


async def test_batch_reads_precede_all_writes_and_keep_input_positions(
    store: SqlAlchemyStore,
) -> None:
    await store.aput(("before",), "same", {"value": "before"})
    operations: list[Op] = [
        PutOp(("before",), "same", {"value": "intermediate"}),
        GetOp(("before",), "same"),
        PutOp(("created",), "new", {}),
        SearchOp(()),
        ListNamespacesOp(),
        PutOp(("before",), "same", {"value": "last"}),
        PutOp(("absent",), "deleted", {}),
        PutOp(("absent",), "deleted", None),
        GetOp(("missing",), "missing"),
    ]
    used = False

    def once() -> Iterable[Op]:
        nonlocal used
        assert not used
        used = True
        yield from operations

    result = await store.abatch(once())
    assert len(result) == len(operations)
    assert isinstance(result[1], Item) and result[1].value == {"value": "before"}
    assert isinstance(result[3], list) and len(result[3]) == 1
    assert result[4] == [("before",)]
    assert all(result[index] is None for index in (0, 2, 5, 6, 7, 8))
    last = await store.aget(("before",), "same")
    assert last is not None and last.value == {"value": "last"}
    assert await store.alist_namespaces() == [("before",), ("created",)]
    await store.adelete(("created",), "new")
    assert await store.alist_namespaces() == [("before",)]


async def test_namespace_matching_filtering_truncation_and_pagination(
    store: SqlAlchemyStore,
) -> None:
    values = [
        ("a",),
        ("a", "*"),
        ("a", "%_"),
        ("a", "*", "leaf"),
        ("a", "b", "leaf"),
        ("a", "c", "leaf"),
        ("aa", "b", "leaf"),
        ("a", "b", "leaf", "extra"),
        ("ab",),
        ("é",),
        ("é",),
    ]
    await store.abatch(PutOp(namespace, "key", {}) for namespace in reversed(values))
    assert await store.alist_namespaces(limit=100) == sorted(values)
    assert {item.namespace for item in await store.asearch(("a",), limit=100)} == {
        n for n in values if n[0] == "a"
    }
    assert {item.namespace for item in await store.asearch(("a", "*"), limit=100)} == {
        ("a", "*"),
        ("a", "*", "leaf"),
    }
    op = ListNamespacesOp(
        match_conditions=(
            MatchCondition("prefix", ("a", "*")),
            MatchCondition("suffix", ("leaf",)),
        ),
        max_depth=2,
        limit=1,
        offset=1,
    )
    assert await store.abatch([op]) == [[("a", "b")]]
    assert await store.alist_namespaces(max_depth=0) == [()]
    assert await store.alist_namespaces(prefix=("missing",), max_depth=0) == []
    assert await store.alist_namespaces(max_depth=0, offset=1) == []
    for start in range(len(values)):
        assert await store.alist_namespaces(offset=start, limit=1) == [
            sorted(values)[start]
        ]
    searches = await store.asearch((), limit=100)
    assert [item.namespace for item in searches] == sorted(values)
    assert all(isinstance(item, SearchItem) and item.score is None for item in searches)


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ({"value": None}, {"null"}),
        ({"value": True}, {"boolean"}),
        ({"value": 1}, {"integer", "float"}),
        (
            {"value": {"$ne": 1}},
            {"null", "boolean", "string", "object", "array", "big", "rounded", "huge"},
        ),
        ({"value": {"$eq": {"b": 2, "a": 1}}}, {"object"}),
        ({"value": [1, "x"]}, {"array"}),
        ({"value": 2**53 + 1}, {"big"}),
        ({"value": float(2**53 + 1)}, {"rounded"}),
        ({"value": {"$gt": 1}}, {"big", "rounded", "huge"}),
        ({"value": {"$gte": 1, "$lte": 1}}, {"integer", "float"}),
        ({"value": {"$lt": 2}}, {"integer", "float"}),
        ({"field.dot": True, "$literal": "yes"}, {"object"}),
    ],
)
async def test_json_filters_have_the_same_typed_semantics(
    store: SqlAlchemyStore, condition: dict[str, Any], expected: set[str]
) -> None:
    values = {
        "null": None,
        "boolean": True,
        "integer": 1,
        "float": 1.0,
        "string": "1",
        "object": {"a": 1, "b": 2},
        "array": [1, "x"],
        "big": 2**53 + 1,
        "rounded": float(2**53),
        "huge": 10**400,
    }
    await store.abatch(
        [
            PutOp(
                ("filters",),
                key,
                {
                    "value": value,
                    **(
                        {"field.dot": True, "$literal": "yes"}
                        if key == "object"
                        else {}
                    ),
                },
            )
            for key, value in values.items()
        ]
        + [PutOp(("filters",), "missing", {})]
    )
    assert {
        item.key
        for item in await store.asearch(("filters",), filter=condition, limit=100)
    } == expected


@pytest.mark.parametrize(
    "operation",
    [
        PutOp(("good",), "bad", {}, ttl=1),
        PutOp(("good",), "bad", {}, index=["value"]),
        SearchOp((), query="semantic"),
        PutOp((), "bad", {}),
        GetOp(("langgraph",), "bad"),
        PutOp(("a.b",), "bad", {}),
        PutOp(("good",), "bad", {"number": float("nan")}),
        SearchOp((), filter={"value": {"nested": 1}}),
        SearchOp((), filter={"value": {"$gt": True}}),
        SearchOp((), filter={"value": {"$gt": "1"}}),
        SearchOp((), offset=-1),
        ListNamespacesOp(max_depth=-1),
    ],
)
async def test_invalid_last_operation_cannot_commit_prior_writes(
    store: SqlAlchemyStore, operation: Op
) -> None:
    with pytest.raises(
        (NotImplementedError, InvalidNamespaceError, TypeError, ValueError)
    ):
        await store.abatch(
            [PutOp(("first",), "key", {"must": "not persist"}), operation]
        )
    assert await store.aget(("first",), "key") is None


async def test_concurrent_stores_keep_document_and_namespace_ownership(
    storage: AsyncEngine,
    store: SqlAlchemyStore,
) -> None:
    peer = SqlAlchemyStore(storage)
    try:
        await asyncio.gather(store.setup(), peer.setup())
        await asyncio.gather(
            store.aput(("shared",), "one", {"writer": 1}),
            peer.aput(("shared",), "two", {"writer": 2}),
        )
        assert {item.key for item in await store.asearch(("shared",))} == {"one", "two"}
        await asyncio.gather(
            store.adelete(("shared",), "one"),
            peer.aput(("shared",), "three", {"writer": 3}),
        )
        assert await store.alist_namespaces() == [("shared",)]
        assert {item.key for item in await peer.asearch(("shared",))} == {
            "two",
            "three",
        }
        await store.aclose()
        with pytest.raises(StoreClosedError):
            await store.aget(("shared",), "two")
        assert await peer.aget(("shared",), "two") is not None
    finally:
        await peer.aclose()


async def test_sync_rejection_and_plain_nonindexed_mode(
    store: SqlAlchemyStore,
) -> None:
    with pytest.raises(NotImplementedError, match="async"):
        store.batch([])
    assert not store.supports_ttl
    assert await store.abatch([]) == []
    await store.aput(("plain",), "key", {}, index=False)
    assert await store.aget(("plain",), "key") is not None


async def test_long_namespaces_and_keys_keep_full_order_through_pagination(
    store: SqlAlchemyStore,
) -> None:
    root = base64.urlsafe_b64encode(("🌌" * 128).encode()).decode().rstrip("=")
    shared = "科研档案" * 1200
    namespaces = [(root, shared + tail, "results") for tail in ("b", "a", "a ")]
    keys = ["observation-" * 1000 + tail for tail in ("b", "a", "a ")]
    await store.abatch(
        PutOp(namespace, key, {"source": "observatory"})
        for namespace in namespaces
        for key in keys
    )
    expected = sorted((namespace, key) for namespace in namespaces for key in keys)
    for offset, pair in enumerate(expected):
        results = await store.asearch((root,), limit=1, offset=offset)
        assert [(item.namespace, item.key) for item in results] == [pair]
    assert await store.alist_namespaces(
        prefix=(root,), suffix=("results",), max_depth=2
    ) == sorted({namespace[:2] for namespace in namespaces})
    assert await store.alist_namespaces(prefix=(root,), max_depth=0) == [()]
