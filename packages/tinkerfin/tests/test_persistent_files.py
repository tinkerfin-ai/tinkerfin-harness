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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin import TinkerFin
from tinkerfin.files import FileConflict
from tinkerfin_contracts.storage import JsonValue
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


async def test_file_pages_and_agent_search_share_an_exact_collection(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'exact.db'}")
    try:
        async with SqlAlchemyStore(engine) as store:
            configured = TinkerFin(store=store).with_namespace("owner")
            files = configured.files(("notes",))
            child = configured.files(("notes", "private"))
            await files.create("/same.md", b"parent")
            await child.create("/same.md", b"secret-child")
            await child.create("/child-only.md", b"secret-child")
            assert [item.path for item in await files.list(limit=1)] == ["/same.md"]
            assert await files.list(limit=1, offset=1) == []
            runtime = configured.build(
                backend=files.backend,
                model=Model(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "ls",
                                    "args": {"path": "/"},
                                    "id": "list",
                                    "type": "tool_call",
                                },
                                {
                                    "name": "glob",
                                    "args": {"pattern": "*.md", "path": "/"},
                                    "id": "glob",
                                    "type": "tool_call",
                                },
                                {
                                    "name": "grep",
                                    "args": {
                                        "pattern": "secret-child",
                                        "path": "/",
                                        "output_mode": "content",
                                    },
                                    "id": "grep",
                                    "type": "tool_call",
                                },
                            ],
                        ),
                        AIMessage(content="done"),
                    ]
                ),
            )
            result = await runtime.ainvoke(
                thread_id="thread", run_id="search", input={"messages": []}
            )
            messages = tool_messages(result)
            assert len(messages) == 3 and all(
                message.status == "success" for message in messages
            )
            assert all(
                "child-only" not in str(message.content)
                and "secret-child" not in str(message.content)
                for message in messages
            )
            assert "/same.md" in str(messages[0].content)
            assert (await files.read("/same.md")).content == b"parent"
    finally:
        await engine.dispose()


async def test_agent_replaces_files_and_recursively_deletes_directories(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tools.db'}")
    try:
        async with SqlAlchemyStore(engine) as store:
            configured = TinkerFin(store=store).with_namespace("owner")
            files = configured.files(("notes",))
            first = await files.create("/existing.md", b"old")
            await files.create("/folder/one.md", b"one")
            await files.create("/folder/sub/two.md", b"two")
            retained = await files.create("/folder-other.md", b"retained")
            runtime = configured.build(
                backend=files.backend,
                model=Model(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "write_file",
                                    "args": {
                                        "file_path": "/existing.md",
                                        "content": "replacement",
                                    },
                                    "id": "replace",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "delete",
                                    "args": {"file_path": "/folder/"},
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
                thread_id="thread", run_id="tools", input={"messages": []}
            )
            messages = tool_messages(result)
            assert len(messages) == 2 and all(
                message.status == "success" for message in messages
            )
            assert (await files.read(first.path)).content == b"replacement"
            with pytest.raises(FileConflict):
                await files.delete(first.path, expected_etag=first.etag)
            assert [item.path for item in await files.list()] == [
                "/existing.md",
                "/folder-other.md",
            ]
            assert (await files.read(retained.path)).etag == retained.etag
    finally:
        await engine.dispose()


@pytest.mark.parametrize("concurrent", ["update", "create", "cancel"])
async def test_recursive_delete_reports_remaining_files_and_preserves_cancellation(
    tmp_path: Path,
    concurrent: str,
) -> None:
    class GatedStore(SqlAlchemyStore):
        def __init__(self, engine: AsyncEngine) -> None:
            super().__init__(engine)
            self.entered = asyncio.Event()
            self.proceed = asyncio.Event()
            self.deleted_key: str | None = None
            self.blocked_key: str | None = None

        async def acompare_and_set(
            self,
            namespace: tuple[str, ...],
            key: str,
            *,
            expected: dict[str, JsonValue] | None,
            value: dict[str, JsonValue] | None,
        ) -> bool:
            if value is None and self.deleted_key is not None:
                self.blocked_key = key
                self.entered.set()
                await self.proceed.wait()
            changed = await super().acompare_and_set(
                namespace, key, expected=expected, value=value
            )
            if value is None and changed:
                self.deleted_key = key
            return changed

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delete-race.db'}")
    try:
        async with GatedStore(engine) as store:
            configured = TinkerFin(store=store).with_namespace("owner")
            files = configured.files(("notes",))
            first = await files.create("/folder/one.md", b"one")
            second = await files.create("/folder/two.md", b"two")
            snapshots = {item.path: item for item in (first, second)}
            runtime = configured.build(
                backend=files.backend,
                model=Model(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "delete",
                                    "args": {"file_path": "/folder"},
                                    "id": "delete",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content="done"),
                    ]
                ),
            )
            deletion = asyncio.create_task(
                runtime.ainvoke(
                    thread_id="thread", run_id="delete", input={"messages": []}
                )
            )
            entered = asyncio.create_task(store.entered.wait())
            try:
                await asyncio.wait(
                    (entered, deletion), return_when=asyncio.FIRST_COMPLETED
                )
                assert store.entered.is_set(), (
                    "Deletion must reach the controlled competing mutation"
                )
                assert store.deleted_key is not None and store.blocked_key is not None
                blocked = snapshots[store.blocked_key]
                with pytest.raises(FileNotFoundError):
                    await files.read(store.deleted_key)
                if concurrent == "cancel":
                    deletion.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await deletion
                    assert (await files.read(blocked.path)).content == blocked.content
                    return
                if concurrent == "update":
                    await files.update(
                        blocked.path, b"changed", expected_etag=blocked.etag
                    )
                else:
                    await files.create("/folder/new.md", b"new")
                store.proceed.set()
                result = await deletion
                messages = tool_messages(result)
                assert len(messages) == 1 and messages[0].status == "error"
                assert "Deletion incomplete" in str(messages[0].content)
                assert "already be deleted" in str(messages[0].content)
                if concurrent == "update":
                    assert (await files.read(blocked.path)).content == b"changed"
                else:
                    assert (await files.read("/folder/new.md")).content == b"new"
                    with pytest.raises(FileNotFoundError):
                        await files.read(blocked.path)
            finally:
                store.proceed.set()
                if not deletion.done():
                    deletion.cancel()
                if not entered.done():
                    entered.cancel()
                await asyncio.gather(deletion, entered, return_exceptions=True)
    finally:
        await engine.dispose()


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


@pytest.mark.parametrize(
    "path", ["relative.md", "/../secret", "/a/../b", "/", "/a//b", "/a\\b"]
)
async def test_file_paths_are_canonical_virtual_files(
    tmp_path: Path, path: str
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'paths.db'}")
    try:
        async with SqlAlchemyStore(engine) as store:
            files = TinkerFin(store=store).with_namespace("owner").files(("notes",))
            with pytest.raises(ValueError):
                await files.create(path, b"no")
    finally:
        await engine.dispose()


async def test_agent_writes_are_visible_to_editors(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")
    try:
        async with SqlAlchemyStore(engine) as store:
            configured = TinkerFin(store=store).with_namespace("owner")
            files = configured.files(("project", "memories"))
            runtime = configured.build(
                backend=files.backend,
                model=Model(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "write_file",
                                    "args": {
                                        "file_path": "/note.md",
                                        "content": "original",
                                    },
                                    "id": "create",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "edit_file",
                                    "args": {
                                        "file_path": "/note.md",
                                        "old_string": "original",
                                        "new_string": "updated",
                                    },
                                    "id": "edit",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content="done"),
                    ]
                ),
            )
            await runtime.ainvoke(
                thread_id="thread", run_id="run", input={"messages": []}
            )
            assert (await files.read("/note.md")).content == b"updated"
    finally:
        await engine.dispose()
