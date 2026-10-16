"""Agent routes and external editors share scoped conditional file writes."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin import TinkerFin
from tinkerfin.files import FileConflict
from tinkerfin_langgraph_store import SqlAlchemyStore


class Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        return self


def tool_messages(result: Mapping[str, object]) -> list[ToolMessage]:
    messages = result["messages"]
    assert isinstance(messages, list)
    return [message for message in messages if isinstance(message, ToolMessage)]


async def test_agent_root_deletion_stays_within_the_bound_collection(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'root.db'}")
    try:
        async with SqlAlchemyStore(engine) as store:
            configured = TinkerFin(store=store).with_namespace("owner")
            files = configured.files(("notes",))
            child = configured.files(("notes", "private"))
            foreign = TinkerFin(store=store).with_namespace("foreign").files(("notes",))
            await files.create("/one.md", b"one")
            await files.create("/sub/two.md", b"two")
            private = await child.create("/one.md", b"private")
            outsider = await foreign.create("/one.md", b"foreign")
            runtime = configured.build(
                backend=files.backend,
                model=Model(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "delete",
                                    "args": {"file_path": "/"},
                                    "id": "delete",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content="done"),
                    ]
                ),
            )
            result = await runtime.ainvoke(
                thread_id="thread", run_id="root", input={"messages": []}
            )
            messages = tool_messages(result)
            assert len(messages) == 1 and messages[0].status == "success"
            assert await files.list() == []
            assert (await child.read(private.path)).etag == private.etag
            assert (await foreign.read(outsider.path)).etag == outsider.etag
    finally:
        await engine.dispose()


async def test_scoped_editors_reject_stale_updates_and_recreated_files(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'files.db'}")
    try:
        async with SqlAlchemyStore(engine) as store:
            owner = TinkerFin(store=store).with_namespace("alice")
            files = owner.files(("project-a", "memories"))
            other = owner.files(("project-b", "memories"))
            foreign = (
                TinkerFin(store=store)
                .with_namespace("bob")
                .files(("project-a", "memories"))
            )
            first = await files.create("/note.md", "你好".encode())
            assert await other.list() == await foreign.list() == []
            with pytest.raises(FileConflict):
                await files.create("/note.md", b"overwrite")
            outcomes = await asyncio.gather(
                files.update(first.path, b"one", expected_etag=first.etag),
                files.update(first.path, b"two", expected_etag=first.etag),
                return_exceptions=True,
            )
            assert sum(isinstance(result, FileConflict) for result in outcomes) == 1
            latest = await files.read(first.path)
            same_content = await files.update(
                latest.path, latest.content, expected_etag=latest.etag
            )
            assert same_content.etag != latest.etag
            with pytest.raises(FileConflict):
                await files.update(latest.path, b"stale", expected_etag=latest.etag)
            latest = same_content
            await files.delete(latest.path, expected_etag=latest.etag)
            recreated = await files.create(latest.path, latest.content)
            assert recreated.etag != latest.etag
            with pytest.raises(FileConflict):
                await files.delete(latest.path, expected_etag=latest.etag)
            assert (await files.read(first.path)).content == latest.content
    finally:
        await engine.dispose()
