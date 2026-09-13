"""Runtime Store isolation through the public execution and LangGraph Store APIs."""

from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import pytest
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.backends.state import StateBackend
from deepagents.backends.store import StoreBackend
from langchain.tools import ToolRuntime, tool
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.store.base import (
    BaseStore,
    GetOp,
    ListNamespacesOp,
    MatchCondition,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)
from langgraph.store.memory import InMemoryStore

from tinkerfin import TinkerFin
from tinkerfin.deep_agent import create_graph


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


async def _runtime_store(
    store: BaseStore, namespace: str, *, direct: bool = False
) -> BaseStore:
    captured: list[BaseStore] = []

    @tool
    async def inspect_memory(runtime: ToolRuntime) -> str:
        """Inspect the memory available to this agent."""
        assert runtime.store is not None
        captured.append(runtime.store)
        return "memory is available"

    runtime = (
        TinkerFin(store=store)
        .with_namespace(namespace)
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "inspect_memory",
                                "args": {},
                                "id": "memory",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            tools=[inspect_memory],
        )
    )
    if direct:
        await (await create_graph(runtime)).ainvoke({"messages": []})
    else:
        await runtime.ainvoke(
            thread_id="same-thread", run_id="same-run", input={"messages": []}
        )
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("direct", [False, True])
async def test_runtime_stores_isolate_identical_business_paths(direct: bool) -> None:
    store = InMemoryStore()
    alpha = await _runtime_store(store, "组织/alpha", direct=direct)
    beta = await _runtime_store(store, "组织/beta", direct=direct)
    await alpha.aput(("users", "same-user"), "profile", {"owner": "alpha"})
    await beta.aput(("users", "same-user"), "profile", {"owner": "beta"})
    for view, owner in ((alpha, "alpha"), (beta, "beta")):
        item = await view.aget(("users", "same-user"), "profile")
        assert item is not None
        assert item.value == {"owner": owner}
        assert item.namespace == ("users", "same-user")
        found = await view.asearch(())
        assert [item.value for item in found] == [{"owner": owner}]
        assert await view.alist_namespaces() == [("users", "same-user")]
    await alpha.adelete(("users", "same-user"), "profile")
    assert await alpha.aget(("users", "same-user"), "profile") is None
    assert await beta.aget(("users", "same-user"), "profile") is not None
    assert len(await store.asearch(())) == 1


async def test_namespace_listing_applies_filters_depth_and_pagination_inside_scope() -> (
    None
):
    shared = InMemoryStore()
    view = await _runtime_store(shared, "scope")
    other = await _runtime_store(shared, "other")
    paths = [("a", "one", "end"), ("a", "two", "end"), ("b", "three")]
    for path in paths:
        await view.aput(path, "key", {"path": "/".join(path)})
        await other.aput(path, "key", {"other": True})
    assert await view.alist_namespaces(
        prefix=("a",), suffix=("end",), limit=1, offset=1
    ) == [paths[1]]
    assert await view.alist_namespaces(max_depth=1, limit=1, offset=1) == [("b",)]
    assert await view.alist_namespaces(max_depth=0) == [()]
    assert await view.alist_namespaces(prefix=("*", "one")) == [paths[0]]
    physical_root = urlsafe_b64encode(b"scope").decode().rstrip("=")
    assert await view.alist_namespaces(suffix=(physical_root, *paths[0])) == []
    assert await view.alist_namespaces(suffix=("*", "end")) == paths[:2]


async def test_scoped_batches_preserve_operation_options_and_result_types() -> None:
    class RecordingStore(InMemoryStore):
        def __init__(self) -> None:
            super().__init__(
                index={"dims": 6, "embed": DeterministicFakeEmbedding(size=6)}
            )
            self.operations: list[Op] = []

        async def abatch(self, ops: Iterable[Op]) -> list[Result]:
            batch = tuple(ops)
            self.operations.extend(batch)
            return await super().abatch(batch)

    shared = RecordingStore()
    view = await _runtime_store(shared, "scope")
    await view.abatch(
        [PutOp(("notes",), "key", {"text": "memory"}, index=["text"], ttl=12)]
    )
    results = await view.abatch(
        [
            GetOp(("notes",), "key", refresh_ttl=False),
            SearchOp(("notes",), query="memory", limit=3, offset=0, refresh_ttl=False),
            ListNamespacesOp(match_conditions=(MatchCondition("suffix", ("notes",)),)),
        ]
    )
    item, search, namespaces = results
    assert item is not None and not isinstance(item, list)
    assert item.namespace == ("notes",)
    assert isinstance(search, list) and len(search) == 1
    assert isinstance(search[0], SearchItem)
    assert search[0].namespace == ("notes",)
    assert search[0].score is not None
    assert namespaces == [("notes",)]
    put = shared.operations[0]
    assert isinstance(put, PutOp)
    assert put.index == ["text"] and put.ttl == 12
    get = shared.operations[1]
    assert isinstance(get, GetOp) and get.refresh_ttl is False
    query = shared.operations[2]
    assert isinstance(query, SearchOp)
    assert query.query == "memory" and query.limit == 3 and query.refresh_ttl is False
    assert (await shared.asearch(()))[0].namespace != ("notes",)
    with pytest.raises(NotImplementedError, match="asynchronous"):
        view.get(("notes",), "key")


@pytest.mark.parametrize("routed", [False, True])
def test_filesystem_memory_cannot_bypass_the_runtime_store(routed: bool) -> None:
    store = InMemoryStore()
    backend: BackendProtocol = StoreBackend(
        namespace=lambda _runtime: ("notes",), store=store
    )
    if routed:
        backend = CompositeBackend(default=StateBackend(), routes={"/memory/": backend})
    with pytest.raises(ValueError, match="pass store= to TinkerFin"):
        TinkerFin(store=store).with_namespace("scope").build(
            model=_Model(responses=[AIMessage(content="done")]),
            backend=backend,
        )
