"""Exercise durable remote execution over two independent databases."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import pytest
from ag_ui.core import RunFinishedEvent
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import JsonValue
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin import AgUiResumeRequest, TinkerFin
from tinkerfin_contracts import (
    FilePath,
    PreparedWorkspace,
    RunIdentity,
    ToolExecutionScope,
    bind_workspace_operation,
    workspace_operation,
)
from tinkerfin_remote import (
    RemoteEndpoint,
    RemoteError,
    RemoteErrorCode,
    RemoteLimits,
    RemoteSession,
    RemoteTools,
    ToolContextArg,
    ToolExecutor,
    ToolWorker,
    WorkspaceRequest,
)
from tinkerfin_remote.models import (
    CloseWorkspace,
    CoordinatorFrame,
    Dispatch,
    OpenWorkspace,
    WorkerFrame,
)

IDENTITY = RunIdentity(namespace="account", thread_id="project-thread", run_id="turn")
LIMITS = RemoteLimits(
    frame_bytes=8192,
    chunk_bytes=512,
    request_bytes=4096,
    output_bytes=32768,
    file_bytes=65536,
    retained_bytes=4 * 1024 * 1024,
)


class MemoryFiles(SandboxBackendProtocol):
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.writes = 0
        self.commands = 0
        self.opened = 0
        self.closed = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = asyncio.Event()
        self.resolutions: dict[str, str] = {}
        self.search_roots: list[tuple[str, str | None]] = []
        self.cleanup_gate: tuple[asyncio.Event, asyncio.Event] | None = None
        self.closed_event = asyncio.Event()
        self.backend = CompositeBackend(
            default=self, routes={}, artifacts_root="/workspace/.artifacts"
        )

    @property
    def id(self) -> str:
        return "memory"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        raise NotImplementedError

    async def aexecute_stream(
        self,
        command: str,
        *,
        timeout: int | None = None,
        emit_output: Callable[[str], Awaitable[None]],
    ) -> ExecuteResponse:
        self.commands += 1
        self.started.set()
        try:
            await emit_output("running")
            if command == "wait":
                await self.release.wait()
            return ExecuteResponse(output="running", exit_code=0, truncated=False)
        finally:
            if self.cleanup_gate is not None:
                entered, release = self.cleanup_gate
                entered.set()
                await release.wait()
            self.stopped.set()

    async def resolve(
        self, path: str | None, *, operation: Literal["read", "write"]
    ) -> str:
        selected = path or "/workspace"
        return self.resolutions.get(
            selected, selected if selected.startswith("/") else "/workspace/" + selected
        )

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        self.writes += 1
        self.files[file_path] = content.encode()
        return WriteResult(path=file_path)

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        if file_path not in self.files:
            return ReadResult(error="not found")
        return ReadResult(
            file_data={
                "content": "\n".join(
                    self.files[file_path].decode().splitlines()[offset : offset + limit]
                ),
                "encoding": "utf-8",
                "created_at": "created",
                "modified_at": "modified",
            }
        )

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        self.files[file_path] = self.files[file_path].replace(
            old_string.encode(), new_string.encode(), -1 if replace_all else 1
        )
        return EditResult(path=file_path, occurrences=1)

    async def adelete(self, file_path: str) -> DeleteResult:
        del self.files[file_path]
        return DeleteResult(path=file_path)

    async def als(self, path: str) -> LsResult:
        self.search_roots.append(("ls", path))
        return LsResult(entries=[{"path": path} for path in self.files])

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        self.search_roots.append(("glob", path))
        return GlobResult(matches=[{"path": entry} for entry in self.files])

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        self.search_roots.append(("grep", path))
        matches: list[GrepMatch] = [
            {"path": entry, "line": 1, "text": content.decode()}
            for entry, content in self.files.items()
            if pattern.encode() in content
        ]
        return GrepResult(matches=matches[:max_count])

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        self.writes += 1
        self.files.update(files)
        return [FileUploadResponse(path=path) for path, _content in files]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [
            FileDownloadResponse(
                path=path,
                content=self.files.get(path),
                error=None if path in self.files else "file_not_found",
            )
            for path in paths
        ]

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncGenerator[PreparedWorkspace[MemoryFiles, BackendProtocol]]:
        self.opened += 1
        try:
            yield PreparedWorkspace(
                workspace=self,
                backend=self.backend,
                file_path_resolver=self.resolve,
            )
        finally:
            self.closed += 1
            self.closed_event.set()


class Duplex:
    def __init__(self) -> None:
        self.to_worker: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self.to_cloud: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self.worker_frames: list[WorkerFrame] = []
        self.cloud_frames: list[CoordinatorFrame] = []
        self.changed = asyncio.Condition()
        self.cloud_delivery_stopped = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []

    async def send_cloud(self, payload: str) -> None:
        async with self.changed:
            self.cloud_frames.append(CoordinatorFrame.model_validate_json(payload))
            self.changed.notify_all()
        await self.to_worker.put(payload)

    async def send_worker(self, payload: str) -> None:
        async with self.changed:
            self.worker_frames.append(WorkerFrame.model_validate_json(payload))
            self.changed.notify_all()
        await self.to_cloud.put(payload)

    async def wait_cloud(
        self, predicate: Callable[[CoordinatorFrame], bool]
    ) -> CoordinatorFrame:
        async with self.changed:
            await self.changed.wait_for(
                lambda: any(predicate(frame) for frame in self.cloud_frames)
            )
            return next(frame for frame in self.cloud_frames if predicate(frame))

    async def wait_worker(
        self, predicate: Callable[[WorkerFrame], bool], *, after: int = 0
    ) -> WorkerFrame:
        async with self.changed:
            await self.changed.wait_for(
                lambda: any(predicate(frame) for frame in self.worker_frames[after:])
            )
            return next(
                frame for frame in self.worker_frames[after:] if predicate(frame)
            )

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result


class Harness:
    def __init__(
        self,
        path: Path,
        *,
        tools: tuple[BaseTool, ...] = (),
        limits: RemoteLimits = LIMITS,
    ) -> None:
        self.cloud_engine = create_async_engine(f"sqlite+aiosqlite:///{path}/cloud.db")
        self.worker_engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}/worker.db"
        )
        self.files = MemoryFiles()
        self.tools = tools
        self.limits = limits
        self.stack = AsyncExitStack()
        self.output: list[JsonValue] = []

    @asynccontextmanager
    async def host(
        self, request: WorkspaceRequest
    ) -> AsyncGenerator[ToolExecutor[MemoryFiles]]:
        assert request.authorization == "grant"
        yield ToolExecutor(workspace=self.files, tools=self.tools)

    async def open(self) -> None:
        self.stack.push_async_callback(self.cloud_engine.dispose)
        self.stack.push_async_callback(self.worker_engine.dispose)
        self.remote = await self.stack.enter_async_context(
            RemoteTools(self.cloud_engine, limits=self.limits)
        )
        self.endpoint = self.remote.endpoint(namespace="account", target="device")
        self.worker = await self.stack.enter_async_context(
            ToolWorker(
                self.worker_engine,
                namespace="account",
                target="device",
                open_workspace=self.host,
                limits=self.limits,
            )
        )
        self.wire = self.connect()
        prepared = await self.stack.enter_async_context(
            self.endpoint.workspace(authorization="grant").prepare(IDENTITY)
        )
        assert isinstance(prepared.workspace, RemoteSession)
        assert prepared.workspace_scope_id is not None
        self.prepared = prepared
        self.session = prepared.workspace
        self.scope_id = prepared.workspace_scope_id

    def connect(self) -> Duplex:
        wire = Duplex()

        async def serve_cloud() -> None:
            try:
                await self.endpoint.serve(
                    send=wire.send_cloud, receive=wire.to_cloud.get
                )
            finally:
                wire.cloud_delivery_stopped.set()

        wire.tasks = [
            asyncio.create_task(serve_cloud()),
            asyncio.create_task(
                self.worker.serve(send=wire.send_worker, receive=wire.to_worker.get)
            ),
        ]
        self.stack.push_async_callback(wire.close)
        return wire

    def scope(self, name: str) -> ToolExecutionScope:
        return ToolExecutionScope(
            identity=IDENTITY,
            origin_identity=IDENTITY,
            workspace_scope_id=self.scope_id,
            invocation_id=name,
            graph_namespace=(),
            purpose="tool",
            tool_call_id=name,
        )

    async def emit(self, value: JsonValue) -> None:
        self.output.append(value)

    async def close(self) -> None:
        await self.stack.aclose()


class _ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


async def test_named_remote_workspace_preserves_cloud_files_and_review_receipt(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    await harness.open()
    try:
        cloud = tmp_path / "cloud"
        cloud.mkdir()
        model = _ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "computer_tools",
                            "id": "discover",
                            "args": {},
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "id": "cloud-write",
                            "args": {"file_path": "/report.md", "content": "cloud"},
                        },
                        {
                            "name": "computer_write_file",
                            "id": "computer-write",
                            "args": {"file_path": "report.md", "content": "computer"},
                        },
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
        runtime = (
            TinkerFin(checkpointer=InMemorySaver())
            .with_namespace("account")
            .build(
                model=model,
                backend=FilesystemBackend(root_dir=cloud, virtual_mode=True),
                workspaces={
                    "computer": harness.endpoint.workspace(authorization="grant")
                },
                interrupt_on={"computer_write_file": True},
            )
        )
        async with aclosing(
            runtime.open_agui_run(
                thread_id="review-thread",
                run_id="proposal",
                messages=[
                    {"id": "user", "role": "user", "content": "write both reports"}
                ],
            )
        ) as stream:
            proposal = [event async for event in stream]
        terminal = proposal[-1]
        assert isinstance(terminal, RunFinishedEvent)
        assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
        assert harness.files.writes == 0
        resume = AgUiResumeRequest.model_validate(
            {
                "entries": [
                    {
                        "interruptId": item.id,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                    for item in terminal.outcome.interrupts
                ]
            }
        )
        async with aclosing(
            runtime.open_agui_run(
                thread_id="review-thread",
                run_id="approved",
                resume=resume,
            )
        ) as stream:
            completed = [event async for event in stream]
        assert isinstance(completed[-1], RunFinishedEvent)
        assert harness.files.files == {"/workspace/report.md": b"computer"}
        assert harness.files.writes == 1
        assert (cloud / "report.md").read_text() == "cloud"
        requests = [
            command.request
            for frame in harness.wire.cloud_frames
            for envelope in frame.commands
            if isinstance(command := envelope.command, Dispatch)
            and command.request.operation.kind == "file.write"
        ]
        assert len({request.operation_id for request in requests}) == 1
        assert requests[0].origin.run_id == "proposal"
        receipt = await harness.endpoint.get_operation(requests[0].operation_id)
        assert receipt is not None and receipt.state == "completed"
        private_events = [
            event.model_dump(mode="json")
            for event in [*proposal, *completed]
            if any(
                key in event.model_dump_json()
                for key in (
                    "_tinkerfin_named_workspaces",
                    "_tinkerfin_file_contract",
                )
            )
        ]
        assert not private_events
        assert sum(event.type == "RUN_FINISHED" for event in proposal) == 1
        assert sum(event.type == "RUN_FINISHED" for event in completed) == 1
    finally:
        await harness.close()
    assert harness.files.closed == harness.files.opened


@pytest.mark.asyncio
async def test_files_binary_and_replay_use_one_concrete_effect(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await harness.open()
    try:
        for _repeat in range(2):
            with bind_workspace_operation(harness.scope("write")):
                assert (
                    await harness.session.awrite("/workspace/a.txt", "hello")
                ).path == "/workspace/a.txt"
        assert harness.files.writes == 1
        with bind_workspace_operation(harness.scope("write")):
            with pytest.raises(RemoteError, match="arguments"):
                await harness.session.awrite("/workspace/a.txt", "different")
        with bind_workspace_operation(harness.scope("read-edit")):
            assert (
                await harness.session.aread("/workspace/a.txt")
            ).file_data is not None
            assert (
                await harness.session.aedit("/workspace/a.txt", "hello", "world")
            ).occurrences == 1
            assert (await harness.session.als("/workspace")).entries
            assert (await harness.session.aglob("*.txt", "/workspace")).matches
            assert (
                await harness.session.agrep("world", "/workspace", max_count=1)
            ).matches
        content = bytes(range(256)) * 80
        with bind_workspace_operation(harness.scope("binary")):
            assert (
                await harness.session.aupload_files([("/workspace/data.bin", content)])
            )[0].error is None
            downloaded = await harness.session.adownload_files(
                ["/workspace/data.bin", "/workspace/missing"]
            )
            assert downloaded[0].content == content
            assert downloaded[1].error == "file_not_found"
            await harness.session.adelete("/workspace/data.bin")
        assert any(
            event.chunk
            for frame in harness.wire.worker_frames
            for event in frame.events
        )
    finally:
        await harness.close()
    assert harness.files.closed == harness.files.opened


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "alias", "directory", "canonical", "root"),
    [
        ("note.md", "./note.md", ".", "/workspace/note.md", "/workspace"),
        ("~/note.md", "/home/user/note.md", "~", "/home/user/note.md", "/home/user"),
        (
            "/workspace/./note.md",
            "/workspace//note.md",
            "/workspace/",
            "/workspace/note.md",
            "/workspace",
        ),
    ],
)
async def test_direct_file_calls_resolve_native_paths_and_preserve_result_contracts(
    tmp_path: Path,
    requested: str,
    alias: str,
    directory: str,
    canonical: str,
    root: str,
) -> None:
    harness = Harness(tmp_path)
    harness.files.resolutions.update(
        {requested: canonical, alias: canonical, directory: root}
    )
    await harness.open()
    try:
        with bind_workspace_operation(harness.scope("native-file-calls")):
            assert (await harness.session.awrite(requested, "first")).path == canonical
            read = await harness.session.aread(requested)
            assert read.file_data is not None and read.file_data["content"] == "first"
            edited = await harness.session.aedit(requested, "first", "second")
            assert edited.path == canonical and edited.occurrences == 1
            assert (await harness.session.als(directory)).entries == [
                {"path": canonical}
            ]
            assert (await harness.session.aglob("*.md", directory)).matches == [
                {"path": canonical}
            ]
            matches = (await harness.session.agrep("second", directory)).matches
            assert matches and matches[0]["path"] == canonical
            assert harness.files.search_roots == [
                ("ls", root),
                ("glob", root),
                ("grep", root),
            ]
            uploaded = await harness.session.aupload_files(
                [(requested, b"final"), (alias, b"final")]
            )
            assert uploaded == [
                FileUploadResponse(path=requested),
                FileUploadResponse(path=alias),
            ]
            downloaded = await harness.session.adownload_files(
                [alias, requested, "missing.md"]
            )
            assert downloaded == [
                FileDownloadResponse(path=alias, content=b"final"),
                FileDownloadResponse(path=requested, content=b"final"),
                FileDownloadResponse(path="missing.md", error="file_not_found"),
            ]
            assert (await harness.session.adelete(requested)).path == canonical
        assert harness.files.files == {}
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_native_path_replay_keeps_original_arguments_and_never_resolves_again(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    await harness.open()
    try:
        with bind_workspace_operation(harness.scope("native-replay")):
            assert await harness.session.aupload_files([("note.md", b"once")]) == [
                FileUploadResponse(path="note.md")
            ]
        harness.files.resolutions["note.md"] = "/workspace/retargeted.md"
        with bind_workspace_operation(harness.scope("native-replay")):
            assert await harness.session.aupload_files([("note.md", b"once")]) == [
                FileUploadResponse(path="note.md")
            ]
        harness.files.resolutions["./note.md"] = "/workspace/note.md"
        with bind_workspace_operation(harness.scope("native-replay")):
            with pytest.raises(RemoteError) as error:
                await harness.session.aupload_files([("./note.md", b"once")])
        assert error.value.code == RemoteErrorCode.CONFLICT
        assert harness.files.writes == 1
        assert harness.files.files == {"/workspace/note.md": b"once"}
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "canonical"),
    [
        ("/workspace/reviewed", "/outside/changed"),
        ("/workspace/link/../reviewed", "/workspace/reviewed"),
        ("//workspace/reviewed", "/workspace/reviewed"),
    ],
)
async def test_absolute_file_targets_cannot_rebind_before_any_direct_operation(
    tmp_path: Path, target: str, canonical: str
) -> None:
    harness = Harness(tmp_path)
    harness.files.files[target] = b"original"
    harness.files.resolutions[target] = canonical
    await harness.open()
    calls: tuple[Callable[[], Awaitable[object]], ...] = (
        lambda: harness.session.aupload_files([(target, b"changed")]),
        lambda: harness.session.adownload_files([target]),
        lambda: harness.session.awrite(target, "changed"),
        lambda: harness.session.aread(target),
        lambda: harness.session.aedit(target, "original", "changed"),
        lambda: harness.session.adelete(target),
        lambda: harness.session.als(target),
        lambda: harness.session.aglob("*", target),
        lambda: harness.session.agrep("original", target),
    )
    try:
        for index, call in enumerate(calls):
            with bind_workspace_operation(harness.scope(f"changed-target-{index}")):
                with pytest.raises(RemoteError):
                    await call()
        assert harness.files.files == {target: b"original"}
        assert harness.files.writes == 0
        assert harness.files.search_roots == []
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transfer", ["upload", "download"])
@pytest.mark.parametrize("mismatch", ["count", "order"])
async def test_native_transfer_rejects_backend_correlation_before_mapping_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transfer: str, mismatch: str
) -> None:
    harness = Harness(tmp_path)

    async def upload(files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        result = [FileUploadResponse(path=path) for path, _content in files]
        return result[:1] if mismatch == "count" else result[::-1]

    async def download(paths: list[str]) -> list[FileDownloadResponse]:
        result = [FileDownloadResponse(path=path, content=b"value") for path in paths]
        return result[:1] if mismatch == "count" else result[::-1]

    await harness.open()
    try:
        monkeypatch.setattr(harness.files.backend, "aupload_files", upload)
        monkeypatch.setattr(harness.files.backend, "adownload_files", download)
        with bind_workspace_operation(harness.scope("bad-correlation")):
            with pytest.raises(RemoteError):
                if transfer == "upload":
                    await harness.session.aupload_files([("a", b"a"), ("b", b"b")])
                else:
                    await harness.session.adownload_files(["a", "b"])
        assert not any(
            event.chunk
            for frame in harness.wire.worker_frames
            for event in frame.events
        )
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_native_paths_preserve_backend_permission_errors_and_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    harness.files.resolutions["../private"] = "/protected/private"

    async def upload(files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        result: list[FileUploadResponse] = []
        for path, content in files:
            if path == "/protected/private":
                result.append(FileUploadResponse(path=path, error="permission_denied"))
            else:
                harness.files.files[path] = content
                result.append(FileUploadResponse(path=path))
        return result

    monkeypatch.setattr(harness.files, "aupload_files", upload)
    await harness.open()
    try:
        with bind_workspace_operation(harness.scope("permissions")):
            assert await harness.session.aupload_files(
                [("allowed", b"allowed"), ("../private", b"denied")]
            ) == [
                FileUploadResponse(path="allowed"),
                FileUploadResponse(path="../private", error="permission_denied"),
            ]
        assert harness.files.files == {"/workspace/allowed": b"allowed"}
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_disconnect_keeps_owned_execution_and_replays_result_once(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    await harness.open()

    async def execute() -> ExecuteResponse:
        with bind_workspace_operation(
            harness.scope("command"), emit_output=harness.emit
        ):
            return await harness.session.aexecute("wait")

    task = asyncio.create_task(execute())
    try:
        await harness.files.started.wait()
        operation_id = next(
            command.command.request.operation_id
            for frame in harness.wire.cloud_frames
            for command in frame.commands
            if isinstance(command.command, Dispatch)
        )
        before = await harness.endpoint.get_operation(operation_id)
        assert before is not None and before.state in {"pending", "started"}
        assert before.result is None and not before.cancellation_requested
        await harness.wire.close()
        assert await harness.endpoint.get_operation(operation_id) is not None
        foreign = harness.remote.endpoint(namespace="foreign", target="device")
        assert await foreign.get_operation(operation_id) is None
        assert await harness.endpoint.get_operation("0" * 64) is None
        assert not harness.files.stopped.is_set()
        harness.files.release.set()
        await harness.worker.wait_idle()
        assert harness.files.stopped.is_set()
        harness.wire = harness.connect()
        assert (await task).exit_code == 0
        assert harness.output == ["running"]
        assert harness.files.commands == 1
        after = await harness.endpoint.get_operation(operation_id)
        assert after is not None and after.state == "completed"
        assert after.result_available and after.result is not None
        assert harness.files.commands == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_local_stop(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await harness.open()

    async def execute() -> ExecuteResponse:
        with bind_workspace_operation(
            harness.scope("cancel"), emit_output=harness.emit
        ):
            return await harness.session.aexecute("wait")

    task = asyncio.create_task(execute())
    try:
        await harness.files.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness.files.stopped.is_set()
        await harness.worker.wait_idle()
        assert harness.files.commands == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_real_context_json_and_changed_path_rejection(tmp_path: Path) -> None:
    contexts: list[ToolContextArg[MemoryFiles]] = []

    @tool
    async def inspect_file(
        file_path: Annotated[str, FilePath("read")],
        runtime: ToolContextArg[MemoryFiles],
    ) -> dict[str, JsonValue]:
        """Inspect one approved file path."""
        contexts.append(runtime)
        await runtime.emit_output("inspecting")
        return {"file": file_path, "count": 2, "flags": [True, None]}

    harness = Harness(tmp_path, tools=(inspect_file,))
    await harness.open()
    try:
        entry = harness.prepared.tools[0]
        properties = entry.descriptor.input_schema["properties"]
        assert isinstance(properties, dict) and "runtime" not in properties
        with bind_workspace_operation(
            harness.scope("inspect"), emit_output=harness.emit
        ):
            result = await entry.invoke({"file_path": "/workspace/a"})
        assert result.value == {
            "file": "/workspace/a",
            "count": 2,
            "flags": [True, None],
        }
        assert contexts[0].identity == IDENTITY
        with pytest.raises(RuntimeError, match="ended"):
            _workspace = contexts[0].workspace
        assert harness.output == ["inspecting"]
        harness.files.resolutions["/workspace/a"] = "/outside/changed"
        with bind_workspace_operation(
            harness.scope("changed"), emit_output=harness.emit
        ):
            with pytest.raises(RemoteError):
                await entry.invoke({"file_path": "/workspace/a"})
        assert len(contexts) == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_named_parallel_steps_have_independent_operation_ids(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    await harness.open()
    try:

        async def write(name: str) -> None:
            with workspace_operation(name):
                await harness.session.awrite("/workspace/" + name, name)

        for _repeat in range(2):
            with bind_workspace_operation(harness.scope("parallel")):
                await asyncio.gather(write("a"), write("b"))
        assert harness.files.writes == 2
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_namespace_and_injected_argument_are_rejected(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await harness.open()
    try:
        with pytest.raises(RemoteError) as error:
            async with harness.endpoint.workspace(authorization="grant").prepare(
                IDENTITY.model_copy(update={"namespace": "foreign"})
            ):
                pytest.fail("foreign namespace opened")
        assert error.value.code == RemoteErrorCode.REJECTED
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_cancel_after_dispatch_delivery_keeps_durable_cancel(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    await harness.open()
    await harness.wire.close()
    sent: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    received: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    delivery = asyncio.create_task(
        harness.endpoint.serve(send=sent.put, receive=received.get)
    )

    async def execute() -> ExecuteResponse:
        with bind_workspace_operation(
            harness.scope("commit-cancel"), emit_output=harness.emit
        ):
            return await harness.session.aexecute("wait")

    task = asyncio.create_task(execute())
    try:
        while True:
            frame = CoordinatorFrame.model_validate_json(await sent.get())
            if any(isinstance(item.command, Dispatch) for item in frame.commands):
                break
        delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        harness.wire = harness.connect()
        await harness.wire.wait_worker(
            lambda frame: any(event.kind == "cancelled" for event in frame.events)
        )
        await harness.worker.wait_idle()
        assert harness.files.commands == 0
    finally:
        delivery.cancel()
        task.cancel()
        await asyncio.gather(delivery, task, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_cancel_after_workspace_delivery_closes_saved_borrow(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    await harness.open()
    await harness.wire.close()
    sent: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    received: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    delivery = asyncio.create_task(
        harness.endpoint.serve(send=sent.put, receive=received.get)
    )

    async def prepare() -> None:
        async with harness.endpoint.workspace(authorization="grant").prepare(IDENTITY):
            pytest.fail("cancelled preparation entered")

    task = asyncio.create_task(prepare())
    try:
        while True:
            frame = CoordinatorFrame.model_validate_json(await sent.get())
            requests = [
                item.command.request
                for item in frame.commands
                if isinstance(item.command, OpenWorkspace)
                and item.command.request.session_id != harness.session.id
            ]
            if requests:
                requested = requests[0].session_id
                break
        delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        harness.wire = harness.connect()
        await harness.wire.wait_worker(
            lambda frame: any(
                reply.session_id == requested and reply.closed
                for reply in frame.workspaces
            )
        )
        assert harness.files.opened - harness.files.closed == 1
    finally:
        delivery.cancel()
        task.cancel()
        await asyncio.gather(delivery, task, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_offline_cancel_returns_pending_and_stops_on_reconnect(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    await harness.open()

    async def execute() -> ExecuteResponse:
        with bind_workspace_operation(
            harness.scope("offline-cancel"), emit_output=harness.emit
        ):
            return await harness.session.aexecute("wait")

    task = asyncio.create_task(execute())
    try:
        await harness.files.started.wait()
        await harness.wire.close()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert "not yet confirmed" in " ".join(error.value.__notes__)
        assert not harness.files.stopped.is_set()
        harness.wire = harness.connect()
        await harness.files.stopped.wait()
        await harness.worker.wait_idle()
        assert harness.files.commands == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_second_worker_detaches_without_stopping_owned_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tinkerfin_messaging import MessageChannel

    harness = Harness(tmp_path)
    await harness.open()

    async def execute() -> ExecuteResponse:
        with bind_workspace_operation(
            harness.scope("multi-owner"), emit_output=harness.emit
        ):
            return await harness.session.aexecute("wait")

    task = asyncio.create_task(execute())
    attached = asyncio.Event()
    original_wrap = MessageChannel.wrap

    async def subscribed() -> None:
        attached.set()

    async def wrap(channel, source, **options):
        return await original_wrap(channel, source, **options, on_subscribed=subscribed)

    try:
        await harness.files.started.wait()
        monkeypatch.setattr(MessageChannel, "wrap", wrap)
        async with ToolWorker(
            harness.worker_engine,
            namespace="account",
            target="device",
            open_workspace=harness.host,
            limits=LIMITS,
        ):
            await attached.wait()
            assert harness.files.commands == 1
        assert not harness.files.stopped.is_set()
        harness.files.release.set()
        assert (await task).exit_code == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_json_values_remain_typed_and_python_results_are_rejected(
    tmp_path: Path,
) -> None:
    @tool
    async def result_value(kind: str) -> object:
        """Return the requested finite value."""
        if kind == "json":
            return {"text": "ok", "items": [None, True, 3]}
        if kind == "blocks":
            return [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,YQ=="},
                }
            ]
        if kind == "number":
            return 7
        if kind == "command":
            return Command(update={"messages": []})
        return object()

    harness = Harness(tmp_path, tools=(result_value,))
    await harness.open()
    try:
        entry = harness.prepared.tools[0]
        expected: dict[str, JsonValue] = {
            "json": {"text": "ok", "items": [None, True, 3]},
            "blocks": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,YQ=="},
                }
            ],
            "number": 7,
        }
        for kind, value in expected.items():
            with bind_workspace_operation(harness.scope(kind)):
                assert (await entry.invoke({"kind": kind})).value == value
        for kind in ("command", "object"):
            with bind_workspace_operation(harness.scope(kind)):
                with pytest.raises(RemoteError) as error:
                    await entry.invoke({"kind": kind})
                assert error.value.code == RemoteErrorCode.REJECTED
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_closed_bulk_sessions_reclaim_space_and_keep_small_replay(
    tmp_path: Path,
) -> None:
    limits = RemoteLimits(
        frame_bytes=32768,
        chunk_bytes=4096,
        request_bytes=16384,
        output_bytes=131072,
        file_bytes=131072,
        replay_result_bytes=4096,
        retained_bytes=400000,
        operations=2,
    )
    harness = Harness(tmp_path, limits=limits)
    await harness.open()
    content = bytes(range(256)) * 256
    first = IDENTITY.model_copy(update={"run_id": "bulk-0"})
    try:
        for index in range(4):
            current = IDENTITY.model_copy(update={"run_id": f"bulk-{index}"})
            async with harness.endpoint.workspace(authorization="grant").prepare(
                current
            ) as prepared:
                expected = prepared.workspace.id
                assert prepared.workspace_scope_id is not None
                scope = ToolExecutionScope(
                    identity=current,
                    origin_identity=current,
                    workspace_scope_id=prepared.workspace_scope_id,
                    invocation_id="bulk",
                    graph_namespace=(),
                    purpose="tool",
                    tool_call_id="bulk",
                )
                with bind_workspace_operation(scope):
                    await prepared.workspace.aupload_files(
                        [("/workspace/bulk.bin", content)]
                    )
                    assert (
                        await prepared.workspace.adownload_files(
                            ["/workspace/bulk.bin"]
                        )
                    )[0].content == content
            closed = await harness.wire.wait_worker(
                lambda frame: any(
                    item.closed and item.session_id == expected
                    for item in frame.workspaces
                )
            )
            await harness.wire.wait_worker(
                lambda frame: not frame.workspaces and not frame.events,
                after=harness.wire.worker_frames.index(closed) + 1,
            )
        assert harness.files.writes == 4
        current = IDENTITY.model_copy(update={"run_id": "rebuilt"})
        async with harness.endpoint.workspace(authorization="grant").prepare(
            current
        ) as prepared:
            assert prepared.workspace_scope_id is not None
            scope = ToolExecutionScope(
                identity=current,
                origin_identity=first,
                workspace_scope_id=prepared.workspace_scope_id,
                invocation_id="bulk",
                graph_namespace=(),
                purpose="tool",
                tool_call_id="bulk",
            )
            with bind_workspace_operation(scope):
                assert (
                    await prepared.workspace.aupload_files(
                        [("/workspace/bulk.bin", content)]
                    )
                )[0].error is None
                with pytest.raises(RemoteError, match="expired") as error:
                    await prepared.workspace.adownload_files(["/workspace/bulk.bin"])
                assert error.value.code == RemoteErrorCode.UNCERTAIN
        assert harness.files.writes == 4
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_revoke_sends_cancellation_without_queued_work(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await harness.open()

    async def execute() -> ExecuteResponse:
        with bind_workspace_operation(
            harness.scope("revoke"), emit_output=harness.emit
        ):
            return await harness.session.aexecute("wait")

    task = asyncio.create_task(execute())
    try:
        await harness.files.started.wait()
        await harness.endpoint.revoke()
        frame = await harness.wire.wait_cloud(lambda frame: frame.revoked)
        assert not frame.commands and not frame.chunks
        await harness.files.stopped.wait()
        await harness.worker.wait_idle()
        with pytest.raises(RemoteError) as error:
            await task
        assert error.value.code == RemoteErrorCode.UNCERTAIN
        with pytest.raises(RemoteError) as reopened:
            async with harness.endpoint.workspace(authorization="grant").prepare(
                IDENTITY
            ):
                pytest.fail("revoked endpoint opened")
        assert reopened.value.code == RemoteErrorCode.REJECTED
        assert harness.files.commands == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Revocation deliberately terminates the delivery attachment.
        for delivery in harness.wire.tasks:
            delivery.cancel()
        for result in await asyncio.gather(*harness.wire.tasks, return_exceptions=True):
            assert isinstance(result, (RemoteError, asyncio.CancelledError))
        harness.wire.tasks.clear()
        await harness.close()


@pytest.mark.asyncio
async def test_coordinator_close_during_cancel_does_not_claim_remote_stop(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    await harness.open()
    entered, released = asyncio.Event(), asyncio.Event()
    harness.files.cleanup_gate = entered, released

    async def execute() -> ExecuteResponse:
        with bind_workspace_operation(
            harness.scope("closing"), emit_output=harness.emit
        ):
            return await harness.session.aexecute("wait")

    task = asyncio.create_task(execute())
    try:
        await harness.files.started.wait()
        task.cancel()
        await entered.wait()
        await harness.remote.aclose()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert "not yet confirmed" in " ".join(error.value.__notes__)
        assert not harness.files.stopped.is_set()
        released.set()
        await harness.files.stopped.wait()
        await harness.worker.wait_idle()
    finally:
        released.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_shutdown_saves_other_endpoints_after_storage_failure(
    tmp_path: Path,
) -> None:
    """A failed endpoint cannot prevent stop delivery to another endpoint."""
    from contextvars import ContextVar

    closing = ContextVar("closing", default=False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/endpoints.db")
    remote = await RemoteTools(engine).__aenter__()
    preparations: list[asyncio.Task[None]] = []
    requests: dict[str, str] = {}
    failed = False

    async def first_frame(endpoint: RemoteEndpoint) -> CoordinatorFrame:
        sent: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        received: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        delivery = asyncio.create_task(
            endpoint.serve(send=sent.put, receive=received.get)
        )
        try:
            while True:
                frame = CoordinatorFrame.model_validate_json(await sent.get())
                if frame.commands:
                    return frame
        finally:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)

    async def preparing(endpoint: RemoteEndpoint) -> None:
        async with endpoint.workspace(authorization="grant").prepare(IDENTITY):
            pytest.fail("no peer supplied capabilities")

    def fail_one(connection, clause, multiparams, params, options):
        nonlocal failed
        if closing.get() and not failed:
            failed = True
            raise SQLAlchemyError("synthetic storage failure")

    try:
        for target in ("first", "second"):
            endpoint = remote.endpoint(namespace="account", target=target)
            preparations.append(asyncio.create_task(preparing(endpoint)))
            frame = await first_frame(endpoint)
            request = next(
                item.command.request
                for item in frame.commands
                if isinstance(item.command, OpenWorkspace)
            )
            requests[target] = request.session_id
        event.listen(engine.sync_engine, "before_execute", fail_one)
        token = closing.set(True)
        try:
            with pytest.raises(RemoteError) as error:
                await remote.aclose()
        finally:
            closing.reset(token)
            event.remove(engine.sync_engine, "before_execute", fail_one)
        assert error.value.code == RemoteErrorCode.CLEANUP
        assert error.value.cause is error.value.__cause__
        assert isinstance(error.value.cause, ExceptionGroup)
        assert failed
        results = await asyncio.gather(*preparations, return_exceptions=True)
        assert all(isinstance(result, RemoteError) for result in results)
        closed: set[str] = set()
        async with RemoteTools(engine) as reopened:
            for target, session_id in requests.items():
                frame = await first_frame(
                    reopened.endpoint(namespace="account", target=target)
                )
                if any(
                    isinstance(item.command, CloseWorkspace)
                    and item.command.session_id == session_id
                    for item in frame.commands
                ):
                    closed.add(target)
        assert len(closed) == 1
    finally:
        for task in preparations:
            task.cancel()
        await asyncio.gather(*preparations, return_exceptions=True)
        await asyncio.gather(remote.aclose(), return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_shutdown_keeps_cancellation_after_host_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("private cleanup diagnostic")

    @asynccontextmanager
    async def host(
        request: WorkspaceRequest,
    ) -> AsyncGenerator[ToolExecutor[MemoryFiles]]:
        try:
            yield ToolExecutor(workspace=harness.files)
        finally:
            entered.set()
            await release.wait()
            raise failure

    monkeypatch.setattr(harness, "host", host)
    await harness.open()
    await harness.wire.close()
    closing = asyncio.create_task(harness.worker.aclose())
    try:
        await entered.wait()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await closing
        assert closing.cancelled()
        assert isinstance(cancelled.value.__cause__, RemoteError)
        assert cancelled.value.__cause__.code == RemoteErrorCode.CLEANUP
        assert harness.files.closed == harness.files.opened
        with pytest.raises(RemoteError) as repeated:
            await harness.worker.aclose()
        assert repeated.value.code == RemoteErrorCode.CLEANUP
        assert "private cleanup diagnostic" not in str(repeated.value)
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        await asyncio.gather(harness.close(), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("component_level", [logging.NOTSET, logging.CRITICAL])
async def test_remote_failure_logs_are_host_controlled_and_exclude_tool_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, component_level: int
) -> None:
    sentinel = "private-token-tool-output-and-arguments"

    @tool
    async def failing(value: str) -> str:
        """Fail with a private provider diagnostic."""
        raise RuntimeError(value)

    harness = Harness(tmp_path, tools=(failing,))
    logger = logging.getLogger("tinkerfin.remote")
    previous = logger.level
    logger.setLevel(component_level)
    try:
        with caplog.at_level(logging.ERROR, logger="tinkerfin"):
            await harness.open()
            entry = harness.prepared.tools[0]
            for _replay in range(2):
                with bind_workspace_operation(harness.scope("failure")):
                    with pytest.raises(RemoteError):
                        await entry.invoke({"value": sentinel})
        records = [
            record
            for record in caplog.records
            if record.name.startswith("tinkerfin.remote")
        ]
        assert len(records) == (0 if component_level == logging.CRITICAL else 1)
        for record in records:
            assert record.exc_info is None
            assert sentinel not in str(record.__dict__)
            assert record.__dict__["tinkerfin_error_code"] == "remote.rejected"
            assert len(record.__dict__["tinkerfin_operation_id"]) == 64
    finally:
        logger.setLevel(previous)
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["coordinator", "worker"])
async def test_shutdown_joins_cancelled_sql_before_returning_borrowed_engine(
    tmp_path: Path, owner: str
) -> None:
    """Gate a documented DB checkout boundary without depending on SQL or cursors."""
    from contextvars import ContextVar

    from sqlalchemy.engine import AdaptedConnection

    selected = ContextVar("selected", default=False)
    entered, release = asyncio.Event(), asyncio.Event()
    harness = Harness(tmp_path)
    await harness.open()
    await harness.wire.close()
    receive_started, receive_cancelled = asyncio.Event(), asyncio.Event()
    idle = asyncio.Event()

    async def receive() -> str:
        receive_started.set()
        try:
            await idle.wait()
            raise AssertionError("receive is only released by cancellation")
        finally:
            receive_cancelled.set()

    async def send(payload: str) -> None:
        pass

    serve = harness.endpoint.serve if owner == "coordinator" else harness.worker.serve
    delivery = asyncio.create_task(serve(send=send, receive=receive))
    await receive_started.wait()
    engine = harness.cloud_engine if owner == "coordinator" else harness.worker_engine
    operation = (
        harness.endpoint.revoke if owner == "coordinator" else harness.worker.wait_idle
    )
    close = harness.remote.aclose if owner == "coordinator" else harness.worker.aclose

    async def wait_for_release(driver: object) -> None:
        entered.set()
        await release.wait()

    def checkout(dbapi: AdaptedConnection, record, proxy) -> None:
        if selected.get():
            dbapi.run_async(wait_for_release)

    async def selected_operation() -> None:
        token = selected.set(True)
        try:
            await operation()
        finally:
            selected.reset(token)

    event.listen(engine.sync_engine, "checkout", checkout)
    reading = asyncio.create_task(selected_operation())
    closing: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        reading.cancel()
        closing = asyncio.create_task(close())
        await receive_cancelled.wait()
        assert not reading.done()
        assert not closing.done()
        release.set()
        await closing
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert reading.cancelled()
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        release.set()
        event.remove(engine.sync_engine, "checkout", checkout)
        await asyncio.gather(reading, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["coordinator", "worker"])
async def test_owner_context_keeps_body_cancellation_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    owner: str,
) -> None:
    from tinkerfin_messaging import Messaging

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/context.db")
    entered, parked = asyncio.Event(), asyncio.Event()
    sentinel = "private-cleanup-provider-diagnostic"
    original = Messaging.aclose

    async def fail_close(messaging: Messaging) -> None:
        await original(messaging)
        raise RuntimeError(sentinel)

    def fail_sql(connection, clause, multiparams, params, options):
        raise SQLAlchemyError(sentinel)

    @asynccontextmanager
    async def host(
        request: WorkspaceRequest,
    ) -> AsyncGenerator[ToolExecutor[MemoryFiles]]:
        yield ToolExecutor(workspace=MemoryFiles())

    async def own() -> None:
        if owner == "coordinator":
            async with RemoteTools(engine) as remote:
                await remote.endpoint(namespace="account", target="device").revoke()
                event.listen(engine.sync_engine, "before_execute", fail_sql)
                entered.set()
                await parked.wait()
        else:
            async with ToolWorker(
                engine, namespace="account", target="device", open_workspace=host
            ):
                monkeypatch.setattr(Messaging, "aclose", fail_close)
                entered.set()
                await parked.wait()

    task = asyncio.create_task(own())
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert task.cancelled()
        assert isinstance(cancelled.value.__cause__, RemoteError)
        assert cancelled.value.__cause__.code == RemoteErrorCode.CLEANUP
        assert sentinel not in caplog.text
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if owner == "coordinator":
            event.remove(engine.sync_engine, "before_execute", fail_sql)
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_tool_cleanup_never_logs_its_private_provider_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    entered = asyncio.Event()
    parked = asyncio.Event()
    cleaned = asyncio.Event()
    sentinel = "private-tool-cleanup-data"

    @tool
    async def waiting(value: str) -> str:
        """Wait until the current tool operation is cancelled."""
        entered.set()
        try:
            await parked.wait()
            return "released"
        finally:
            cleaned.set()
            raise RuntimeError(value)

    harness = Harness(tmp_path, tools=(waiting,))
    await harness.open()

    async def invoke() -> None:
        with bind_workspace_operation(harness.scope("cancelled-private-cleanup")):
            await harness.prepared.tools[0].invoke({"value": sentinel})

    task = asyncio.create_task(invoke())
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        await harness.worker.wait_idle()
        assert sentinel not in caplog.text
        assert not any(record.exc_info is not None for record in caplog.records)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()
