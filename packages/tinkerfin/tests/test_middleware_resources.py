"""Declared middleware obey the same Store isolation as ordinary backends."""

from collections.abc import Mapping
from contextlib import aclosing
from typing import Any

import pytest
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.backends.utils import create_file_data
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
    create_summarization_middleware,
)
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.store.memory import InMemoryStore
from test_runtime_store import _Model, _runtime_store

from tinkerfin import TinkerFin
from tinkerfin.deep_agent import create_graph
from tinkerfin.subagents import SubAgent


def _middleware(kind: str, backend: BackendProtocol) -> AgentMiddleware[Any, Any, Any]:
    if kind == "filesystem":
        return FilesystemMiddleware(backend=backend)
    if kind == "memory":
        return MemoryMiddleware(backend=backend, sources=["/AGENTS.md"])
    if kind == "skills":
        return SkillsMiddleware(backend=backend, sources=["/skills/"])
    summary = create_summarization_middleware(
        _Model(responses=[AIMessage(content="summary")]), backend
    )
    return summary if kind == "summary" else SummarizationToolMiddleware(summary)


@pytest.mark.parametrize(
    "kind", ["filesystem", "memory", "skills", "summary", "compact"]
)
@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("subagent", [False, True])
def test_declared_middleware_reject_raw_stores_before_execution(
    kind: str, routed: bool, subagent: bool
) -> None:
    shared = InMemoryStore()
    backend: BackendProtocol = StoreBackend(
        namespace=lambda _: ("files",), store=shared
    )
    if routed:
        backend = CompositeBackend(default=StateBackend(), routes={"/memory/": backend})
    middleware = _middleware(kind, backend)
    builder = TinkerFin(store=shared).with_namespace("alpha")
    spec: SubAgent = {
        "name": "worker",
        "description": "Inspect files",
        "system_prompt": "Inspect the available files.",
        "middleware": [middleware],
    }
    subagents = [spec] if subagent else None
    with pytest.raises(ValueError, match="pass store= to TinkerFin"):
        builder.build(
            model=_Model(responses=[AIMessage(content="done")]),
            middleware=[] if subagent else [middleware],
            subagents=subagents,
        )


@pytest.mark.parametrize("kind", ["filesystem", "memory", "skills"])
@pytest.mark.parametrize("direct", [False, True])
async def test_builtin_middleware_use_async_scoped_stores_without_mutating_hosts(
    kind: str, direct: bool
) -> None:
    shared = InMemoryStore()
    backend = StoreBackend(namespace=lambda _: ("files",))
    middleware = _middleware(kind, backend)
    original = dict(vars(middleware))
    path = "/skills/reader/SKILL.md" if kind == "skills" else "/AGENTS.md"
    for namespace in ("alpha", "beta"):
        scoped = await _runtime_store(shared, namespace)
        content = (
            f"---\nname: reader\ndescription: {namespace} description\n---\nRead files"
            if kind == "skills"
            else f"{namespace} private contents"
        )
        await scoped.aput(("files",), path, dict(create_file_data(content)))
        model = (
            _Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {"file_path": path},
                                "id": "read",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )
            if kind == "filesystem"
            else _Model(responses=[AIMessage(content="done")])
        )
        runtime = (
            TinkerFin(store=shared)
            .with_namespace(namespace)
            .build(model=model, middleware=[middleware])
        )
        if direct:
            result = await (await create_graph(runtime)).ainvoke({"messages": []})
        else:
            result = await runtime.ainvoke(
                thread_id="same", run_id="same", input={"messages": []}
            )
        if kind == "filesystem":
            messages = result["messages"]
            assert isinstance(messages, list)
            observed = str([m.content for m in messages if isinstance(m, ToolMessage)])
        else:
            observed = str(
                result["memory_contents" if kind == "memory" else "skills_metadata"]
            )
        assert namespace in observed
        assert ("beta" if namespace == "alpha" else "alpha") not in observed
        assert vars(middleware) == original


@pytest.mark.parametrize("agui", [False, True])
async def test_file_middleware_keeps_permissions_and_its_tool_allowlist(
    agui: bool,
) -> None:
    shared = InMemoryStore()
    scoped = await _runtime_store(shared, "alpha")
    await scoped.aput(("files",), "/secret", dict(create_file_data("private contents")))
    middleware = FilesystemMiddleware(
        backend=StoreBackend(namespace=lambda _: ("files",)),
        tools=["read_file", "ls"],
        custom_tool_descriptions={"read_file": "Read permitted files"},
        _permissions=[FilesystemPermission(["read"], ["/secret"], "deny")],
        grep_max_count=3,
    )
    names = [tool.name for tool in middleware.tools]
    model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/secret"},
                        "id": "read",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = (
        TinkerFin(store=shared)
        .with_namespace("alpha")
        .build(model=model, middleware=[middleware])
    )
    if agui:
        async with aclosing(
            runtime.open_agui_run(
                thread_id="thread", run_id="run", input={"messages": []}
            )
        ) as stream:
            events = [event.model_dump(mode="json") async for event in stream]
        observed = str(events)
    else:
        result: Mapping[str, object] = await runtime.ainvoke(
            thread_id="thread", run_id="run", input={"messages": []}
        )
        observed = str(result)
    assert "private contents" not in observed
    assert "denied" in observed.lower()
    assert [tool.name for tool in middleware.tools] == names == ["ls", "read_file"]


async def test_summary_history_uses_the_current_namespace_store() -> None:
    shared = InMemoryStore()
    middleware = SummarizationMiddleware(
        _Model(responses=[AIMessage(content="A short summary")]),
        backend=StoreBackend(namespace=lambda _: ("history",)),
        trigger=("messages", 2),
        keep=("messages", 1),
    )
    for namespace in ("alpha", "beta"):
        runtime = (
            TinkerFin(store=shared)
            .with_namespace(namespace)
            .build(
                model=_Model(responses=[AIMessage(content="done")]),
                middleware=[middleware],
            )
        )
        await runtime.ainvoke(
            thread_id="same",
            run_id="same",
            input={
                "messages": [
                    HumanMessage(content=f"{namespace} message {i}", id=str(i))
                    for i in range(4)
                ]
            },
        )
        scoped = await _runtime_store(shared, namespace)
        files = await scoped.asearch(("history",))
        assert files
        assert namespace in str([item.value for item in files])
        assert ("beta" if namespace == "alpha" else "alpha") not in str(
            [item.value for item in files]
        )
