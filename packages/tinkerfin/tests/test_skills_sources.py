"""Configured skill paths refresh within a persisted conversation."""

from typing import Any

import pytest
from deepagents.backends import StoreBackend
from deepagents.backends.utils import create_file_data
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field
from test_runtime_store import _Model, _runtime_store

from tinkerfin import TinkerFin


class _SkillModel(_Model):
    observations: list[str] = Field(default_factory=list)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.observations.append(
            "\n".join(str(message.content) for message in messages)
        )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="done"))]
        )


async def test_public_store_seeds_the_same_namespace_used_by_agent_tools() -> None:
    shared = InMemoryStore()
    configured = TinkerFin(store=shared).with_namespace("owner")
    await configured.store.aput(("library",), "manual", {"text": "instructions"})
    runtime_view = await _runtime_store(shared, "owner")
    item = await runtime_view.aget(("library",), "manual")
    assert item is not None and item.value == {"text": "instructions"}
    assert await configured.with_namespace("other").store.asearch(()) == []
    assert [item.key for item in await configured.store.asearch(())] == ["manual"]
    with pytest.raises(NotImplementedError):
        configured.store.get(("library",), "manual")


def test_public_store_requires_persistence_and_namespace() -> None:
    with pytest.raises(ValueError):
        _ = TinkerFin(store=InMemoryStore()).store
    with pytest.raises(ValueError):
        _ = TinkerFin().with_namespace("owner").store


async def test_skill_discovery_refreshes_changed_paths_and_preserves_unchanged_cache() -> (
    None
):
    model = _SkillModel(responses=[AIMessage(content="done")])
    configured = TinkerFin(
        store=InMemoryStore(), checkpointer=InMemorySaver()
    ).with_namespace("skills")

    async def seed(path: str, description: str) -> None:
        await configured.store.aput(
            ("files",),
            path,
            dict(
                create_file_data(
                    f"---\nname: reporting\ndescription: {description}\n---\nFollow these instructions.\n"
                )
            ),
        )

    async def run(paths: list[str], run_id: str) -> str:
        runtime = configured.build(
            model=model,
            backend=StoreBackend(namespace=lambda _: ("files",)),
            skills=paths,
        )
        await runtime.ainvoke(
            thread_id="same",
            run_id=run_id,
            input={"messages": [HumanMessage(content="Work")]},
        )
        return model.observations[-1]

    await seed("/first/reporting/SKILL.md", "first-description")
    await seed("/second/reporting/SKILL.md", "second-description")
    assert "first-description" in await run(["/first/"], "first")
    await seed("/first/reporting/SKILL.md", "changed-description")
    assert "first-description" in await run(["/first/"], "cached")
    changed = await run(["/second/"], "second")
    assert "second-description" in changed and "first-description" not in changed
    empty = await run([], "empty")
    assert "second-description" not in empty
