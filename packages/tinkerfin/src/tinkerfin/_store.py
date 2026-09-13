"""Keep borrowed LangGraph stores within one Runtime's logical namespace."""

from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import Iterable
from copy import copy
from typing import TypeVar

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.store import StoreBackend
from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    MatchCondition,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

from tinkerfin_contracts.identity import validate_namespace

from .errors import TinkerFinLifecycleError

ItemT = TypeVar("ItemT", bound=Item)


def validate_store_backend(backend: object) -> None:
    """Require filesystem memory routes to use the Runtime's borrowed Store."""

    pending = [backend]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, StoreBackend) and current._store is not None:
            # Deep Agents 0.7.5 StoreBackend._get_store gives this constructor
            # value precedence over get_store(), bypassing Runtime isolation.
            raise ValueError(
                "StoreBackend must use the Runtime store; pass store= to TinkerFin() "
                "and omit store= from StoreBackend"
            )
        if isinstance(current, CompositeBackend):
            pending.extend((current.default, *current.routes.values()))


class NamespaceStore(BaseStore):
    """Expose relative namespaces while keeping all queries under an encoded root.

    The underlying Store remains borrowed. Query filters, pagination, TTL, indexing,
    and search scores remain its responsibility. Namespace filtering happens before
    its pagination; no operation reads other tenants and filters them in memory.
    """

    def __init__(self, store: BaseStore, namespace: str) -> None:
        """Bind one namespace without starting I/O or acquiring Store ownership."""

        self._store = store
        self._root = (
            urlsafe_b64encode(validate_namespace(namespace).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        self.supports_ttl = store.supports_ttl
        self.ttl_config = copy(store.ttl_config)

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """Reject synchronous storage before invoking the borrowed implementation."""

        del ops
        raise NotImplementedError("Runtime stores support asynchronous operations only")

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """Execute one batch in order and restore each result's relative namespace."""

        requests = tuple(ops)
        results = await self._store.abatch(self._scoped(op) for op in requests)
        if len(results) != len(requests):
            raise TinkerFinLifecycleError(
                "Store returned an invalid batch result count"
            )
        return [
            self._relative_result(op, result)
            for op, result in zip(requests, results, strict=True)
        ]

    def _scoped(self, op: Op) -> Op:
        if isinstance(op, GetOp | PutOp):
            return op._replace(namespace=(self._root, *op.namespace))
        if isinstance(op, SearchOp):
            return op._replace(namespace_prefix=(self._root, *op.namespace_prefix))
        if not isinstance(op, ListNamespacesOp):
            raise TypeError("unsupported Store operation")
        conditions = [MatchCondition("prefix", (self._root,))]
        for condition in op.match_conditions or ():
            if condition.match_type == "prefix":
                conditions.append(
                    MatchCondition("prefix", (self._root, *condition.path))
                )
            else:
                conditions.append(condition)
                # A suffix must fit entirely inside the relative namespace,
                # rather than accidentally matching the hidden root component.
                conditions.append(
                    MatchCondition(
                        "prefix", (self._root, *("*",) * len(condition.path))
                    )
                )
        return op._replace(
            match_conditions=tuple(conditions),
            max_depth=None if op.max_depth is None else op.max_depth + 1,
        )

    def _relative_namespace(self, namespace: tuple[str, ...]) -> tuple[str, ...]:
        if not namespace or namespace[0] != self._root:
            raise TinkerFinLifecycleError(
                "Store returned data outside the Runtime namespace"
            )
        return namespace[1:]

    def _relative_item(self, item: ItemT) -> ItemT:
        relative = copy(item)
        relative.namespace = self._relative_namespace(item.namespace)
        return relative

    def _relative_result(self, op: Op, result: Result) -> Result:
        if isinstance(op, GetOp) and (result is None or isinstance(result, Item)):
            return None if result is None else self._relative_item(result)
        if isinstance(op, PutOp) and result is None:
            return None
        if isinstance(result, list):
            if isinstance(op, SearchOp):
                items: list[SearchItem] = []
                for item in result:
                    if not isinstance(item, SearchItem):
                        raise TinkerFinLifecycleError(
                            "Store search returned an invalid item"
                        )
                    items.append(self._relative_item(item))
                return items
            if isinstance(op, ListNamespacesOp):
                namespaces: list[tuple[str, ...]] = []
                for namespace in result:
                    if not isinstance(namespace, tuple):
                        raise TinkerFinLifecycleError(
                            "Store listing returned an invalid namespace"
                        )
                    namespaces.append(self._relative_namespace(namespace))
                return namespaces
        raise TinkerFinLifecycleError("Store returned an invalid operation result")
