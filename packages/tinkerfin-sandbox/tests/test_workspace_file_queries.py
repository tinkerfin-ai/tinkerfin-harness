"""Existing-file queries share project identity without preparing execution."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from test_pause_resume import _world
from test_workspace_lifecycle import _WorkspaceRemote
from test_workspace_watch import _manager, _prepare

from tinkerfin_sandbox import (
    OpenSandboxFileChangedError,
    OpenSandboxNotTextError,
    OpenSandboxPausedError,
    OpenSandboxWorkspaceNotInitializedError,
)

_JSON = TypeAdapter(dict[str, JsonValue])


class _FileRemote(_WorkspaceRemote):
    def __init__(self) -> None:
        super().__init__()
        self.queries: list[dict[str, JsonValue]] = []
        self.failure: str | None = None
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.released.set()

    async def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/command":
            command = _JSON.validate_json(await request.aread()).get("command")
            if isinstance(command, str) and "<workspace-files>" in command:
                payload = _JSON.validate_json(shlex.split(command)[-1])
                self.queries.append(payload)
                self.started.set()
                await self.released.wait()
                if self.failure:
                    result = {"kind": "error", "code": self.failure}
                else:
                    entry: dict[str, JsonValue] = {
                        "path": payload["path"],
                        "name": "note.txt",
                        "kind": "file",
                        "size_bytes": 5,
                        "modified_at": "2026-10-07T00:00:00+00:00",
                        "etag": "a" * 64,
                    }
                    operation = payload["operation"]
                    if operation == "list":
                        entry["path"] = "/note.txt"
                        result = {
                            "kind": "directory",
                            "path": payload["path"],
                            "entries": [entry],
                            "next_cursor": None,
                        }
                    elif operation == "stat":
                        result = {"kind": "info", "file": entry}
                    else:
                        result = {
                            "kind": "text",
                            "file": entry,
                            "text": "hello",
                            "truncated": False,
                        }
                return self.command_response(json.dumps(result))
        return await super().respond(request)


async def test_uninitialized_queries_do_not_create_resources(tmp_path: Path) -> None:
    async with _world(tmp_path) as world:
        manager, _ = await _manager(world)
        project = manager.workspace("owner", workspace_key="project-a")
        for query in (
            project.list_directory,
            lambda: project.get_file_info("/a.txt"),
            lambda: project.read_text("/a.txt", max_bytes=100),
        ):
            with pytest.raises(OpenSandboxWorkspaceNotInitializedError):
                await query()
        assert world.remote.created == 0


async def test_queries_use_existing_project_without_execution_preparation(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        remote = _FileRemote()
        remote.changes = world.changes
        world.remote = remote
        manager, _ = await _manager(world)
        await _prepare(manager)
        project = manager.workspace("owner", workspace_key="project-a")
        registry_before = list(remote.registry_requests)
        created_before = remote.created
        assert (await project.list_directory()).entries[0].path == "/note.txt"
        assert (await project.get_file_info("/note.txt")).size_bytes == 5
        assert (await project.read_text("/note.txt", max_bytes=100)).text == "hello"
        assert remote.registry_requests == registry_before
        assert remote.created == created_before
        assert not remote.live and not remote.network_live
        projects = [query["project"] for query in remote.queries]
        assert all(project == projects[0] for project in projects)


@pytest.mark.parametrize(
    "code,error",
    [
        ("changed", OpenSandboxFileChangedError),
        ("not_text", OpenSandboxNotTextError),
        ("not_found", FileNotFoundError),
    ],
)
async def test_query_failures_keep_public_meaning(
    tmp_path: Path, code: str, error: type[Exception]
) -> None:
    async with _world(tmp_path) as world:
        remote = _FileRemote()
        remote.changes = world.changes
        world.remote = remote
        manager, _ = await _manager(world)
        await _prepare(manager)
        remote.failure = code
        with pytest.raises(error):
            await manager.workspace("owner", workspace_key="project-a").read_text(
                "/note.txt", max_bytes=100
            )


@pytest.mark.parametrize("path", ["../private", "/../private", "//private", "/a\x00b"])
async def test_query_rejects_unsafe_paths_before_remote_access(
    tmp_path: Path, path: str
) -> None:
    async with _world(tmp_path) as world:
        manager, _ = await _manager(world)
        with pytest.raises(ValueError):
            await manager.workspace("owner", workspace_key="project-a").get_file_info(
                path
            )
        assert world.remote.created == 0 and not world.remote.executions


async def test_queries_keep_projects_distinct_and_do_not_resume(tmp_path: Path) -> None:
    async with _world(tmp_path) as world:
        remote = _FileRemote()
        remote.changes = world.changes
        world.remote = remote
        manager, _ = await _manager(world)
        await _prepare(manager)
        first = manager.workspace("owner", workspace_key="project-a")
        second = manager.workspace("owner", workspace_key="project-b")
        await first.get_file_info("/note.txt")
        await second.get_file_info("/note.txt")
        assert remote.queries[0]["project"] != remote.queries[1]["project"]
        await manager.pause("owner")
        with pytest.raises(OpenSandboxPausedError):
            await first.list_directory()
        assert not remote.resume_calls and remote.created == 1


async def test_cancelled_read_releases_admission_and_preserves_cancellation(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        remote = _FileRemote()
        remote.changes = world.changes
        world.remote = remote
        manager, _ = await _manager(world)
        await _prepare(manager)
        remote.released.clear()
        project = manager.workspace("owner", workspace_key="project-a")
        task = asyncio.create_task(project.read_text("/note.txt", max_bytes=100))
        await remote.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        remote.released.set()
        assert (await project.read_text("/note.txt", max_bytes=100)).text == "hello"
        assert not remote.live and not remote.network_live


async def test_parent_removal_during_read_does_not_create_a_replacement(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        remote = _FileRemote()
        remote.changes = world.changes
        world.remote = remote
        manager, _ = await _manager(world)
        await _prepare(manager)
        remote.released.clear()
        task = asyncio.create_task(
            manager.workspace("owner", workspace_key="project-a").get_file_info(
                "/note.txt"
            )
        )
        await remote.started.wait()
        await manager.destroy("owner")
        remote.released.set()
        with pytest.raises(OpenSandboxFileChangedError):
            await task
        assert remote.created == 1
