"""Exercise directory preparation through public workspaces and SDK transport."""

import asyncio
import json
import shlex
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from test_pause_resume import _Remote
from test_workspace_lifecycle import _Gate, _projects, _WorkspaceRemote

from tinkerfin_contracts import RunIdentity
from tinkerfin_sandbox import (
    OpenSandboxError,
    OpenSandboxWorkspaceNotInitializedError,
    WorkspaceDirectoryContents,
)


class DirectoryRemote(_WorkspaceRemote):
    def __init__(self) -> None:
        super().__init__()
        self.markers: dict[str, str] = {}
        self.published: set[str] = set()
        self.requests: dict[str, bytes] = {}
        self.transferred_manifests = 0

    async def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/directories":
            return httpx.Response(200)
        if request.url.path == "/files" and request.method == "DELETE":
            for path in request.url.params.get_list("path"):
                del self.requests[path]
            return httpx.Response(200)
        if request.url.path == "/files/upload":
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + request.headers["Content-Type"].encode()
                + b"\r\n\r\n"
                + await request.aread()
            )
            parts = list(message.iter_parts())
            metadata = json.loads(parts[0].get_content())
            if metadata["path"].startswith("/tmp/.tinkerfin-registry-"):
                content = parts[1].get_content()
                assert isinstance(content, bytes)
                assert metadata["mode"] == 600
                self.requests[metadata["path"]] = content
                self.transferred_manifests += 1
                return httpx.Response(200)
        if request.url.path == "/command":
            command = json.loads(await request.aread())["command"]
            assert len(command.encode()) < 128 * 1024
            if "registry.py < " in command:
                arguments = shlex.split(command)
                payload = TypeAdapter(dict[str, JsonValue]).validate_json(
                    self.requests[arguments[-1]]
                )
                sandbox_id, _ = self.endpoints[request.url.host]
                code, result = self.registry(sandbox_id, payload)
                return self.command_response(json.dumps(result), exit_code=code)
        if request.url.path == "/files/upload":
            return await _Remote.respond(self, request)
        return await super().respond(request)

    def registry(
        self, sandbox_id: str, payload: dict[str, JsonValue]
    ) -> tuple[int, JsonValue]:
        operation, project, arguments = (
            payload["operation"],
            payload["project"],
            payload["arguments"],
        )
        assert isinstance(project, str) and isinstance(arguments, dict)
        if operation not in {"managed_begin", "managed_commit", "maintenance_end"}:
            assert project not in self.markers
            return super().registry(sandbox_id, payload)
        self.registry_requests.append((sandbox_id, str(operation), payload))
        identity = arguments["operation_id"]
        assert isinstance(identity, str)
        if operation == "managed_begin":
            if project in self.published:
                return 0, {"status": "unchanged"}
            record = self.records[sandbox_id, project]
            assert record.sessions == []
            self.markers[project] = identity
            prefix = "/var/lib/tinkerfin-workspaces"
            return 0, {
                "status": "prepared",
                "operation_id": identity,
                "incarnation": record.incarnation,
                "stage": f"{prefix}/directory-staging/{identity}",
                "root": f"{prefix}/projects/{project}/{record.incarnation}/files",
            }
        if operation == "maintenance_end":
            if self.markers.get(project) == identity:
                del self.markers[project]
            return 0, None
        assert self.markers[project] == identity and self.active_uploads == 0
        self.published.add(project)
        return 0, {"status": "published"}


async def test_publication_finishes_before_execution_and_repeated_content_is_unchanged(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, _):
        remote = DirectoryRemote()
        remote.changes = world.changes
        remote.upload_release.set()
        world.remote = remote
        manager = await world.add()
        contents = WorkspaceDirectoryContents("/skills", (("example/SKILL.md", b"hi"),))
        workspace = manager.workspace(
            "owner", workspace_key="project"
        ).with_directories([contents])
        identity = RunIdentity(namespace="ns", thread_id="thread", run_id="run")
        async with workspace.prepare(identity):
            assert remote.published and not remote.markers
        assert len(remote.uploads) == 1
        async with workspace.prepare(identity):
            assert not remote.markers
        assert len(remote.uploads) == 1
        assert not remote.live


@pytest.mark.parametrize("gate_name", ["managed_begin", "managed_commit"])
async def test_cancellation_waits_for_owned_publication_and_cleanup(
    tmp_path: Path,
    gate_name: str,
) -> None:
    async with _projects(tmp_path) as (world, _):
        remote = DirectoryRemote()
        remote.changes = world.changes
        remote.upload_release.set()
        world.remote = remote
        gate = remote.gates[gate_name] = _Gate()
        manager = await world.add()
        workspace = manager.workspace(
            "owner", workspace_key="project"
        ).with_directories(
            [WorkspaceDirectoryContents("/skills", (("example/SKILL.md", b"hi"),))]
        )

        async def use() -> None:
            async with workspace.prepare(
                RunIdentity(namespace="ns", thread_id="thread", run_id="run")
            ):
                pytest.fail("cancelled preparation must never admit execution")

        task = world.spawn(use())
        task.add_done_callback(lambda _: gate.entered.set())
        await gate.entered.wait()
        if task.done():
            await task
        task.cancel()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert remote.published and not remote.markers and not remote.live


async def test_lost_begin_response_still_releases_exact_maintenance_identity(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, _):
        remote = DirectoryRemote()
        remote.changes = world.changes
        remote.lose_response = "managed_begin"
        world.remote = remote
        manager = await world.add()
        workspace = manager.workspace(
            "owner", workspace_key="project"
        ).with_directories([WorkspaceDirectoryContents("/skills", ())])
        with pytest.raises(OpenSandboxError):
            async with workspace.prepare(
                RunIdentity(namespace="ns", thread_id="thread", run_id="run")
            ):
                pytest.fail("failed publication must not admit a Run")
        assert not remote.markers and not remote.live
        assert any(
            operation == "maintenance_end"
            for _, operation, _ in remote.registry_requests
        )


async def test_synchronize_absent_workspace_does_not_create_resources(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        with pytest.raises(OpenSandboxWorkspaceNotInitializedError):
            await manager.workspace(
                "owner", workspace_key="project"
            ).synchronize_directories([WorkspaceDirectoryContents("/skills", ())])
        assert remote.created == 0


@pytest.mark.parametrize(
    "path", ["/", "/skills/../other", "/skills/", "relative", "/skills//x"]
)
def test_directory_declaration_rejects_ambiguous_roots(path: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceDirectoryContents(path, ())


@pytest.mark.parametrize("quoted", [False, True])
async def test_legal_manifest_larger_than_shell_argument_uses_owned_file_transfer(
    tmp_path: Path,
    quoted: bool,
) -> None:
    # This payload crosses Linux's single-argument boundary, not a timing target.
    async with _projects(tmp_path) as (world, _):
        remote = DirectoryRemote()
        remote.changes = world.changes
        remote.upload_release.set()
        world.remote = remote
        manager = await world.add()
        contents = WorkspaceDirectoryContents(
            "/inputs",
            tuple(
                (("'" * 200 if quoted else "input") + f"-{index}.txt", b"")
                for index in range(170 if quoted else 1600)
            ),
        )
        workspace = manager.workspace(
            "owner", workspace_key="large-inputs"
        ).with_directories([contents])
        async with workspace.prepare(
            RunIdentity(namespace="ns", thread_id="thread", run_id="run")
        ):
            assert remote.published
        assert remote.transferred_manifests == 1
        assert not remote.requests and not remote.markers


def test_file_capacity_counts_directories_separately() -> None:
    contents = WorkspaceDirectoryContents(
        "/inputs", tuple((f"folder-{index}/file.txt", b"") for index in range(4097))
    )
    assert len(contents.files) == 4097
    with pytest.raises(ValueError, match="entry capacity"):
        WorkspaceDirectoryContents(
            "/inputs",
            tuple((f"one-{index}/two/file.txt", b"") for index in range(6000)),
        )
