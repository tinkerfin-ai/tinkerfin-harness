"""Configured skill paths refresh within a persisted conversation."""

import asyncio
from typing import Any

import pytest
from deepagents.backends import StoreBackend
from deepagents.backends.protocol import BackendProtocol, FileDownloadResponse, LsResult
from deepagents.backends.utils import create_file_data
from langchain.tools import tool
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field
from test_runtime_store import _Model, _runtime_store

from tinkerfin import SkillSource, TinkerFin


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
        response = self.responses[
            min(len(self.observations) - 1, len(self.responses) - 1)
        ]
        return ChatResult(generations=[ChatGeneration(message=response)])


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


async def test_skill_discovery_refreshes_current_files_in_persisted_conversation() -> (
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
            skills=[SkillSource(path) for path in paths],
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
    assert "changed-description" in await run(["/first/"], "changed")
    changed = await run(["/second/"], "second")
    assert "second-description" in changed and "first-description" not in changed
    empty = await run([], "empty")
    assert "second-description" not in empty


async def test_skill_changes_are_visible_after_a_tool_in_the_same_run() -> None:
    configured = TinkerFin(store=InMemoryStore()).with_namespace("owner")

    async def publish(description: str) -> None:
        await configured.store.aput(
            ("files",),
            "/skills/reporting/SKILL.md",
            dict(
                create_file_data(
                    f"---\nname: reporting\ndescription: {description}\n---\nInstructions"
                )
            ),
        )

    @tool
    async def revise_skill() -> str:
        """Update the project skill's current instructions."""
        await publish("UPDATED-INSTRUCTIONS")
        return "updated"

    await publish("INITIAL-INSTRUCTIONS")
    model = _SkillModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "revise_skill", "args": {}, "id": "revise"}],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = configured.build(
        model=model,
        tools=[revise_skill],
        backend=StoreBackend(namespace=lambda _: ("files",)),
        skills=[SkillSource("/skills/")],
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [HumanMessage(content="Revise")]},
    )
    assert "INITIAL-INSTRUCTIONS" in model.observations[0]
    assert "UPDATED-INSTRUCTIONS" in model.observations[1]
    assert "INITIAL-INSTRUCTIONS" not in model.observations[1]


async def test_source_name_selection_precedes_same_name_override() -> None:
    configured = TinkerFin(store=InMemoryStore()).with_namespace("owner")
    for directory, description in [
        ("first", "ALLOWED-DESCRIPTION"),
        ("second", "EXCLUDED-DESCRIPTION"),
    ]:
        await configured.store.aput(
            ("files",),
            f"/{directory}/reporting/SKILL.md",
            dict(
                create_file_data(
                    f"---\nname: reporting\ndescription: {description}\n---\nInstructions"
                )
            ),
        )
    model = _SkillModel(responses=[AIMessage(content="done")])
    sources = [
        SkillSource("/first/", names=("reporting",)),
        SkillSource("/second/", names=("another",)),
    ]
    runtime = configured.build(
        model=model,
        backend=StoreBackend(namespace=lambda _: ("files",)),
        skills=sources,
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [HumanMessage(content="Work")]},
    )
    assert "ALLOWED-DESCRIPTION" in model.observations[-1]
    assert "EXCLUDED-DESCRIPTION" not in model.observations[-1]
    assert sources == [
        SkillSource("/first/", names=("reporting",)),
        SkillSource("/second/", names=("another",)),
    ]


async def test_bom_skill_is_discovered_without_changing_stored_or_tool_content() -> (
    None
):
    configured = TinkerFin(store=InMemoryStore()).with_namespace("owner")
    path = "/skills/reporting/SKILL.md"
    text = "\ufeff---\nname: reporting\ndescription: BOM-DISCOVERY-MARKER\n---\nInstructions"
    original = dict(create_file_data(text))
    await configured.store.aput(("files",), path, original)
    model = _SkillModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"file_path": path}, "id": "read"}
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = configured.build(
        model=model,
        backend=StoreBackend(namespace=lambda _: ("files",)),
        skills=[SkillSource("/skills/", names=("reporting",))],
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [HumanMessage(content="Read the reporting skill")]},
    )
    assert "BOM-DISCOVERY-MARKER" in model.observations[0]
    assert "\ufeff---" in model.observations[1]
    stored = await configured.store.aget(("files",), path)
    assert stored is not None and stored.value == original


@pytest.mark.parametrize(
    "content,error",
    [(None, "permission_denied"), (b"\xef\xbb\xbf\xff", None)],
)
async def test_skill_discovery_preserves_failed_and_invalid_file_reads(
    content: bytes | None, error: str | None
) -> None:
    response = FileDownloadResponse(
        path="/skills/reporting/SKILL.md", content=content, error=error
    )

    class MetadataBackend(BackendProtocol):
        async def als(self, path: str) -> LsResult:
            return LsResult(entries=[{"path": "/skills/reporting/", "is_dir": True}])

        async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
            return [response]

    model = _SkillModel(responses=[AIMessage(content="done")])
    runtime = (
        TinkerFin()
        .with_namespace("owner")
        .build(model=model, backend=MetadataBackend(), skills=[SkillSource("/skills/")])
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [HumanMessage(content="Work")]},
    )
    assert "/skills/reporting/SKILL.md" not in model.observations[0]
    assert response.content == content and response.error == error


async def test_cancelling_skill_discovery_releases_the_borrowed_read() -> None:
    entered = asyncio.Event()
    released = asyncio.Event()

    class PendingBackend(BackendProtocol):
        async def als(self, path: str) -> LsResult:
            return LsResult(entries=[{"path": "/skills/reporting/", "is_dir": True}])

        async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
            entered.set()
            try:
                await asyncio.Event().wait()
                return []
            finally:
                released.set()

    model = _SkillModel(responses=[AIMessage(content="unused")])
    runtime = (
        TinkerFin()
        .with_namespace("owner")
        .build(model=model, backend=PendingBackend(), skills=[SkillSource("/skills/")])
    )
    run = asyncio.create_task(
        runtime.ainvoke(
            thread_id="thread",
            run_id="run",
            input={"messages": [HumanMessage(content="Work")]},
        )
    )
    reading = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait((run, reading), return_when=asyncio.FIRST_COMPLETED)
        if not reading.done():
            pytest.fail(f"discovery did not start: {await run}")
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert released.is_set() and model.observations == []
    finally:
        reading.cancel()
        run.cancel()
        await asyncio.gather(reading, run, return_exceptions=True)
