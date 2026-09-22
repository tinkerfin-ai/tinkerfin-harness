"""Virtual-root view contracts for OpenSandbox."""

import asyncio
import base64
import json
import shlex
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    DeleteResult,
    EditResult,
    ExecuteOffloadResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox
from opensandbox import Sandbox

import tinkerfin_sandbox as opensandbox
from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxHandle,
    OpenSandboxRuntimeInfo,
    RootedOpenSandboxBackend,
)
from tinkerfin_sandbox.backends import _rooted_protocol


class _RecordingBackend:
    id = "sandbox-1"
    enable_capture_offload = True

    def __init__(self) -> None:
        self.guard_results: list[list[bool]] = []
        self.guard_commands: list[str] = []
        self.commands: list[tuple[str, int | None]] = []
        self.rooted_file_operation_count = 0
        self.download_calls: list[list[str]] = []
        self.upload_calls: list[list[tuple[str, bytes]]] = []
        self.read_calls: list[tuple[str, int, int]] = []
        self.write_calls: list[tuple[str, str]] = []
        self.edit_calls: list[tuple[str, str, str, bool]] = []
        self.delete_calls: list[str] = []
        self.ls_calls: list[str] = []
        self.glob_calls: list[tuple[str, str | None]] = []
        self.grep_calls: list[tuple[str, str | None, str | None, int | None]] = []
        self.read_error: str | None = None
        self.ls_error: str | None = None
        self.glob_error: str | None = None
        self.grep_error: str | None = None
        self.download_error: str | None = None
        self.upload_error: str | None = None
        self.async_execute_calls: list[tuple[str, int | None]] = []
        self.async_download_calls: list[list[str]] = []
        self.async_upload_calls: list[list[tuple[str, bytes]]] = []
        self.rooted_download_calls: list[tuple[str, str]] = []
        self.rooted_upload_calls: list[tuple[str, str, bytes]] = []
        self.rooted_download_errors: dict[str, str] = {}
        self.rooted_upload_errors: dict[str, str] = {}
        self.rooted_download_exceptions: dict[str, Exception] = {}
        self.rooted_upload_exceptions: dict[str, Exception] = {}
        self.async_read_calls: list[tuple[str, int, int]] = []
        self.async_write_calls: list[tuple[str, str]] = []
        self.async_edit_calls: list[tuple[str, str, str, bool]] = []
        self.async_delete_calls: list[str] = []
        self.async_ls_calls: list[str] = []
        self.async_glob_calls: list[tuple[str, str | None]] = []
        self.async_grep_calls: list[tuple[str, str | None, str | None, int | None]] = []
        self.offload_calls: list[tuple[str, str, int, int | None, int | None]] = []
        self.async_offload_calls: list[
            tuple[str, str, int, int | None, int | None]
        ] = []
        self.rooted_offload_calls: list[
            tuple[str, str, str, int, int | None, int | None]
        ] = []
        self.renew_calls: list[timedelta] = []
        self.runtime_info = OpenSandboxRuntimeInfo(
            sandbox_id=self.id,
            available=True,
            healthy=True,
        )

    @contextmanager
    def _rooted_file_operation(self) -> Iterator[None]:
        self.rooted_file_operation_count += 1
        yield

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if "invalid_request" in command:
            return self._rooted_helper_response(command, is_async=False)
        if self.guard_results and "commonpath" in command:
            self.guard_commands.append(command)
            return ExecuteResponse(
                output=json.dumps({"safe": self.guard_results.pop(0)}),
                exit_code=0,
            )
        self.commands.append((command, timeout))
        return ExecuteResponse(output="user output", exit_code=0)

    def _rooted_helper_response(
        self,
        command: str,
        *,
        is_async: bool,
    ) -> ExecuteResponse:
        encoded_request = shlex.split(command)[-1]
        request = json.loads(base64.b64decode(encoded_request).decode("utf-8"))
        operation = request["operation"]
        arguments = request["arguments"]
        physical_path = request["root"].rstrip("/") + (
            "" if arguments["path"] == "/" else arguments["path"]
        )
        if operation == "read":
            call = (physical_path, arguments["offset"], arguments["limit"])
            if is_async:
                self.async_read_calls.append(call)
            else:
                self.read_calls.append(call)
            if self.read_error is None:
                status = "ok"
                error = None
                result = {
                    "encoding": "utf-8",
                    "content": "content",
                    "total_lines": 1,
                    "start_line": 1,
                    "end_line": 1,
                    "next_offset": None,
                    "no_lines_requested": False,
                }
            else:
                status = "error"
                error = {
                    "code": (
                        "permission_denied"
                        if "permission_denied" in self.read_error
                        else "operation_failed"
                    ),
                    "message": self.read_error,
                }
                result = None
        elif operation == "edit":
            call = (
                physical_path,
                arguments["old"],
                arguments["new"],
                arguments["replace_all"],
            )
            if is_async:
                self.async_edit_calls.append(call)
            else:
                self.edit_calls.append(call)
            status = "ok"
            error = None
            result = {"count": 1}
        elif operation == "delete":
            if is_async:
                self.async_delete_calls.append(physical_path)
            else:
                self.delete_calls.append(physical_path)
            status = "ok"
            error = None
            result = {"deleted": True}
        elif operation == "list":
            if is_async:
                self.async_ls_calls.append(physical_path)
            else:
                self.ls_calls.append(physical_path)
            status = "ok"
            error = None
            result = {
                "entries": [
                    {
                        "path": arguments["path"].rstrip("/") + "/child.txt",
                        "is_dir": False,
                    }
                ],
                "partial_error": self.ls_error,
            }
        elif operation == "glob":
            call = (arguments["pattern"], physical_path)
            if is_async:
                self.async_glob_calls.append(call)
            else:
                self.glob_calls.append(call)
            status = "ok"
            error = None
            result = {
                "matches": [
                    {
                        "path": arguments["path"].rstrip("/") + "/nested.py",
                        "is_dir": False,
                    }
                ],
                "truncated": self.glob_error is not None,
                "partial_error": self.glob_error,
            }
        elif operation == "grep":
            call = (
                arguments["pattern"],
                physical_path,
                arguments["glob"],
                arguments["max_count"],
            )
            if is_async:
                self.async_grep_calls.append(call)
            else:
                self.grep_calls.append(call)
            status = "ok"
            error = None
            result = {
                "matches": [
                    {
                        "path": arguments["path"].rstrip("/") + "/match.py",
                        "line": 3,
                        "text": "needle",
                    }
                ],
                "truncated": self.grep_error is not None,
                "partial_error": self.grep_error,
            }
        else:
            raise AssertionError(f"unsupported fake rooted operation: {operation}")
        return ExecuteResponse(
            output=json.dumps(
                {
                    "request_id": request["request_id"],
                    "operation": operation,
                    "status": status,
                    "error": error,
                    "result": result,
                }
            ),
            exit_code=0,
        )

    def renew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    async def arenew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    def get_runtime_info(self) -> OpenSandboxRuntimeInfo:
        return self.runtime_info

    async def aget_runtime_info(self) -> OpenSandboxRuntimeInfo:
        return self.runtime_info

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        self.async_execute_calls.append((command, timeout))
        if "invalid_request" in command:
            return self._rooted_helper_response(command, is_async=True)
        return self.execute(command, timeout=timeout)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        self.download_calls.append(paths)
        return [
            FileDownloadResponse(
                path=path,
                content=None if self.download_error else path.encode(),
                error=self.download_error,
            )
            for path in paths
        ]

    async def adownload_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        self.async_download_calls.append(paths)
        return [
            FileDownloadResponse(
                path=path,
                content=None if self.download_error else path.encode(),
                error=self.download_error,
            )
            for path in paths
        ]

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        self.upload_calls.append(files)
        return [
            FileUploadResponse(path=path, error=self.upload_error) for path, _ in files
        ]

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        self.async_upload_calls.append(files)
        return [
            FileUploadResponse(path=path, error=self.upload_error) for path, _ in files
        ]

    async def _aupload_rooted_file(
        self,
        *,
        root: str,
        path: str,
        content: bytes,
    ) -> FileUploadResponse:
        self.rooted_upload_calls.append((root, path, content))
        if path in self.rooted_upload_exceptions:
            raise self.rooted_upload_exceptions[path]
        return FileUploadResponse(
            path=path,
            error=self.rooted_upload_errors.get(path),
        )

    async def _adownload_rooted_file(
        self,
        *,
        root: str,
        path: str,
    ) -> FileDownloadResponse:
        self.rooted_download_calls.append((root, path))
        if path in self.rooted_download_exceptions:
            raise self.rooted_download_exceptions[path]
        error = self.rooted_download_errors.get(path)
        return FileDownloadResponse(
            path=path,
            content=None if error is not None else path.encode(),
            error=error,
        )

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        self.read_calls.append((file_path, offset, limit))
        if self.read_error is not None:
            return ReadResult(error=self.read_error)
        return ReadResult(file_data={"content": "content", "encoding": "utf-8"})

    def write(self, file_path: str, content: str) -> WriteResult:
        self.write_calls.append((file_path, content))
        return WriteResult(path=file_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        self.edit_calls.append((file_path, old_string, new_string, replace_all))
        return EditResult(path=file_path, occurrences=1)

    def delete(self, file_path: str) -> DeleteResult:
        self.delete_calls.append(file_path)
        return DeleteResult(path=file_path)

    def ls(self, path: str) -> LsResult:
        self.ls_calls.append(path)
        return LsResult(
            error=self.ls_error,
            entries=[{"path": f"{path}/child.txt", "is_dir": False}],
        )

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        self.glob_calls.append((pattern, path))
        return GlobResult(
            error=self.glob_error,
            matches=[{"path": "nested.py", "is_dir": False}],
            truncated=self.glob_error is not None,
        )

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        self.grep_calls.append((pattern, path, glob, max_count))
        return GrepResult(
            error=self.grep_error,
            matches=[
                {
                    "path": f"{path}/match.py",
                    "line": 3,
                    "text": "needle",
                }
            ],
            truncated=self.grep_error is not None,
        )

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        self.async_read_calls.append((file_path, offset, limit))
        return ReadResult(file_data={"content": "content", "encoding": "utf-8"})

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        self.async_write_calls.append((file_path, content))
        return WriteResult(path=file_path)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        self.async_edit_calls.append((file_path, old_string, new_string, replace_all))
        return EditResult(path=file_path, occurrences=1)

    async def adelete(self, file_path: str) -> DeleteResult:
        self.async_delete_calls.append(file_path)
        return DeleteResult(path=file_path)

    async def als(self, path: str) -> LsResult:
        self.async_ls_calls.append(path)
        return LsResult(entries=[{"path": f"{path}/child.txt", "is_dir": False}])

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        self.async_glob_calls.append((pattern, path))
        return GlobResult(matches=[{"path": "nested.py", "is_dir": False}])

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        self.async_grep_calls.append((pattern, path, glob, max_count))
        return GrepResult(
            matches=[
                {
                    "path": f"{path}/match.py",
                    "line": 3,
                    "text": "needle",
                }
            ]
        )

    def execute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        self.offload_calls.append(
            (
                command,
                capture_path,
                max_inline_bytes,
                max_capture_bytes,
                timeout,
            )
        )
        return ExecuteOffloadResult(
            offloaded=True,
            response=ExecuteResponse(output="preview", exit_code=0),
        )

    async def aexecute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        self.async_offload_calls.append(
            (
                command,
                capture_path,
                max_inline_bytes,
                max_capture_bytes,
                timeout,
            )
        )
        return ExecuteOffloadResult(
            offloaded=True,
            response=ExecuteResponse(output="preview", exit_code=0),
        )

    async def _aexecute_rooted_offload(
        self,
        *,
        root: str,
        command: str,
        capture_path: str,
        max_inline_bytes: int,
        max_capture_bytes: int | None,
        timeout: int | None,
    ) -> ExecuteOffloadResult:
        self.rooted_offload_calls.append(
            (
                root,
                command,
                capture_path,
                max_inline_bytes,
                max_capture_bytes,
                timeout,
            )
        )
        return ExecuteOffloadResult(
            offloaded=True,
            response=ExecuteResponse(output="rooted preview", exit_code=0),
        )


class _LocalTransferBackend(BaseSandbox):
    """Run guard commands and real binary reads only inside ``tmp_path``."""

    def __init__(
        self,
        *,
        working_directory: Path | None = None,
        enable_capture_offload: bool = False,
    ) -> None:
        self.enable_capture_offload = enable_capture_offload
        self._use_internal_directory = False
        self._user_shell = LocalShellBackend(
            root_dir=working_directory or "/",
            virtual_mode=False,
            inherit_env=True,
        )
        self._internal_shell = LocalShellBackend(
            root_dir="/",
            virtual_mode=False,
            env={
                "PYTHONPATH": "",
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
            },
            inherit_env=True,
        )

    @property
    def id(self) -> str:
        return "local-sandbox"

    @contextmanager
    def _rooted_file_operation(self) -> Iterator[None]:
        previous = self._use_internal_directory
        self._use_internal_directory = True
        try:
            yield
        finally:
            self._use_internal_directory = previous

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        shell = (
            self._internal_shell if self._use_internal_directory else self._user_shell
        )
        return shell.execute(command, timeout=timeout)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                content = Path(path).read_bytes()
            except FileNotFoundError:
                responses.append(
                    FileDownloadResponse(path=path, content=None, error=FILE_NOT_FOUND)
                )
            else:
                responses.append(
                    FileDownloadResponse(path=path, content=content, error=None)
                )
        return responses

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            destination = Path(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            responses.append(FileUploadResponse(path=path, error=None))
        return responses


class _BlockingGuardBackend(_RecordingBackend):
    def __init__(self) -> None:
        super().__init__()
        self.guard_results.append([True])
        self.guard_started = threading.Event()
        self.guard_release = threading.Event()

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if "commonpath" in command:
            self.guard_started.set()
            if not self.guard_release.wait(timeout=2):
                raise TimeoutError("guard release timed out")
        return super().execute(command, timeout=timeout)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        return await asyncio.to_thread(self.execute, command, timeout=timeout)


def _rooted(backend: Any, *, root: str = "/workspace") -> RootedOpenSandboxBackend:
    return RootedOpenSandboxBackend(OpenSandboxHandle(backend), root=root)


def test_sync_rooted_file_operation_preserves_async_only_error() -> None:
    backend = OpenSandboxBackend(
        sandbox=cast(Sandbox, SimpleNamespace(id="sandbox-async-only"))
    )
    rooted = RootedOpenSandboxBackend(OpenSandboxHandle(backend))

    with pytest.raises(RuntimeError, match="asynchronous remote I/O only"):
        rooted.read("/file.txt")


def test_sync_rooted_mutating_transfers_reject_without_file_io(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    target = workspace / "target.bin"
    target.write_bytes(b"original")
    rooted = _rooted(_LocalTransferBackend(), root=str(workspace))

    for operation in (
        lambda: rooted.write("/target.bin", "changed"),
        lambda: rooted.upload_files([("/target.bin", b"changed")]),
        lambda: rooted.download_files(["/target.bin"]),
        lambda: rooted.execute_with_offload(
            "printf changed > target.bin",
            "/capture.txt",
            max_inline_bytes=1,
        ),
    ):
        with pytest.raises(RuntimeError, match="asynchronous remote I/O only"):
            operation()

    assert target.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_async_rooted_guard_preserves_transport_error() -> None:
    class GuardUnavailableBackend(_RecordingBackend):
        async def aexecute(
            self,
            command: str,
            *,
            timeout: int | None = None,
        ) -> ExecuteResponse:
            del command, timeout
            raise ConnectionError("guard unavailable")

    rooted = _rooted(GuardUnavailableBackend())

    with pytest.raises(ConnectionError, match="guard unavailable"):
        await rooted.aread("/file.txt")


def test_rooted_opensandbox_backend_is_public() -> None:
    assert hasattr(opensandbox, "RootedOpenSandboxBackend")


def test_rooted_backend_maps_virtual_file_path_to_relative_shell_path() -> None:
    rooted = _rooted(_RecordingBackend())

    assert (
        rooted.to_shell_path("/.tinkerfin/skills/.executions/content")
        == ".tinkerfin/skills/.executions/content"
    )


def test_rooted_backend_preserves_base_sandbox_execution_contract() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)

    response = rooted.execute("printf user-command", timeout=7)

    assert isinstance(rooted, BaseSandbox)
    assert rooted.id == "sandbox-1"
    assert rooted.enable_capture_offload is True
    assert response.output == "user output"
    assert backend.commands == [("printf user-command", 7)]


@pytest.mark.asyncio
async def test_rooted_backend_delegates_lifecycle_extensions_to_its_handle() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)
    timeout = timedelta(minutes=15)

    rooted.renew(timeout)
    await rooted.arenew(timeout)

    assert backend.renew_calls == [timeout, timeout]
    assert rooted.get_runtime_info() is backend.runtime_info
    assert await rooted.aget_runtime_info() is backend.runtime_info


@pytest.mark.parametrize(
    "root",
    [
        "workspace",
        "/",
        "//workspace",
        "/workspace/../etc",
        "/workspace\x00private",
    ],
)
def test_rooted_backend_rejects_unsafe_physical_roots(root: str) -> None:
    with pytest.raises(ValueError):
        _rooted(_RecordingBackend(), root=root)


@pytest.mark.asyncio
async def test_download_maps_virtual_paths_and_preserves_requested_paths() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend, root="/workspace/./projects/")

    responses = await rooted.adownload_files(["/a.bin", "b.bin", "./c.bin", "/a..b"])

    assert backend.rooted_download_calls == [
        ("/workspace/projects", "/a.bin"),
        ("/workspace/projects", "/b.bin"),
        ("/workspace/projects", "/c.bin"),
        ("/workspace/projects", "/a..b"),
    ]
    assert [response.path for response in responses] == [
        "/a.bin",
        "b.bin",
        "./c.bin",
        "/a..b",
    ]
    assert [response.error for response in responses] == [None, None, None, None]


@pytest.mark.asyncio
async def test_download_rejects_invalid_items_without_blocking_safe_items() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)

    responses = await rooted.adownload_files(
        [
            "../secret",
            "/safe.bin",
            "/a/../b",
            "nul\x00name",
            "//etc/passwd",
        ]
    )

    assert backend.rooted_download_calls == [("/workspace", "/safe.bin")]
    assert [response.error for response in responses] == [
        "invalid_path",
        None,
        "invalid_path",
        "invalid_path",
        "invalid_path",
    ]


def test_realpath_guard_allows_internal_link_and_rejects_external_links(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "inside.txt").write_bytes(b"inside")
    (outside / "secret.txt").write_bytes(b"secret")
    (workspace / "inside-link").symlink_to(workspace / "inside.txt")
    (workspace / "outside-link").symlink_to(outside / "secret.txt")
    (workspace / "outside-broken").symlink_to(outside / "missing.txt")
    rooted = _rooted(_LocalTransferBackend(), root=str(workspace))

    inside = rooted.read("/inside-link")
    outside_result = rooted.read("/outside-link")
    missing = rooted.read("/missing.txt")
    broken = rooted.read("/outside-broken")

    assert inside.file_data is not None
    assert inside.file_data["content"] == "inside"
    assert outside_result.error is not None
    assert "invalid_path" in outside_result.error
    assert missing.error == "File '/missing.txt': file_not_found"
    assert broken.error is not None
    assert "invalid_path" in broken.error


def test_realpath_guard_rejects_workspace_root_symlink(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    (real_workspace / "secret.txt").write_bytes(b"secret")
    workspace_link = tmp_path / "workspace-link"
    workspace_link.symlink_to(real_workspace, target_is_directory=True)
    rooted = _rooted(_LocalTransferBackend(), root=str(workspace_link))

    response = rooted.read("/secret.txt")

    assert response.file_data is None
    assert response.error is not None
    assert "invalid_path" in response.error


def test_realpath_guard_fails_closed_when_workspace_is_missing(
    tmp_path: Path,
) -> None:
    rooted = _rooted(_LocalTransferBackend(), root=str(tmp_path / "missing"))

    response = rooted.read("/file.txt")

    assert response.file_data is None
    assert response.error is not None
    assert "invalid_path" in response.error


def test_realpath_guard_ignores_workspace_json_module(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    (workspace / "outside-link").symlink_to(outside / "secret.txt")
    (workspace / "json.py").write_text(
        "def loads(_payload):\n"
        f"    return {{'root': {str(workspace)!r}, "
        f"'paths': [{str(workspace / 'outside-link')!r}]}}\n"
        "def dumps(_value):\n"
        "    return '{\"safe\": [true]}'\n",
        encoding="utf-8",
    )
    rooted = _rooted(
        _LocalTransferBackend(working_directory=workspace),
        root=str(workspace),
    )

    response = rooted.read("/outside-link")

    assert response.file_data is None
    assert response.error is not None
    assert "invalid_path" in response.error


def test_internal_file_commands_ignore_workspace_module_shadowing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    (workspace / "json.py").write_text(
        "raise RuntimeError('workspace json.py must not be imported')\n",
        encoding="utf-8",
    )
    rooted = _rooted(
        _LocalTransferBackend(working_directory=workspace),
        root=str(workspace),
    )

    result = rooted.read("/inside.txt")

    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["content"] == "inside"


def test_rooted_read_preserves_deep_agents_error_messages(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "directory").mkdir()
    rooted = _rooted(_LocalTransferBackend(), root=str(workspace))

    missing = rooted.read("/missing.txt")
    directory = rooted.read("/directory")

    assert missing.error == "File '/missing.txt': file_not_found"
    assert directory.error == "File '/directory': not_a_file"


def test_rooted_edit_stages_large_replacement_payload(tmp_path: Path) -> None:
    class CommandLimitedBackend(_LocalTransferBackend):
        def execute(
            self,
            command: str,
            *,
            timeout: int | None = None,
        ) -> ExecuteResponse:
            if len(command.encode("utf-8")) > 150_000:
                return ExecuteResponse(
                    output="command exceeds provider request limit",
                    exit_code=1,
                )
            return super().execute(command, timeout=timeout)

    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    old = "0123456789" * 30_000
    new = "abcdefghij" * 30_000
    target = workspace / "large.txt"
    target.write_text(old, encoding="utf-8")
    rooted = _rooted(CommandLimitedBackend(), root=str(workspace))

    result = rooted.edit("/large.txt", old, new)

    assert result.error is None
    assert result.path == "/large.txt"
    assert result.occurrences == 1
    assert target.read_text(encoding="utf-8") == new


def test_text_and_search_operations_map_paths_and_restore_results() -> None:
    backend = _RecordingBackend()
    backend.guard_results.extend([[True]] * 7)
    rooted = _rooted(backend)

    read_result = rooted.read("/docs/read.txt", offset=2, limit=5)
    edit_result = rooted.edit("/edit.txt", "old", "new", replace_all=True)
    delete_result = rooted.delete("/delete.txt")
    ls_result = rooted.ls("/docs")
    glob_result = rooted.glob("**/*.py", "/src")
    grep_result = rooted.grep("needle", None, "*.py", max_count=4)

    assert read_result.file_data == {"content": "content", "encoding": "utf-8"}
    assert backend.read_calls == [("/workspace/docs/read.txt", 2, 5)]
    assert edit_result.path == "/edit.txt"
    assert edit_result.occurrences == 1
    assert backend.edit_calls == [("/workspace/edit.txt", "old", "new", True)]
    assert delete_result.path == "/delete.txt"
    assert backend.delete_calls == ["/workspace/delete.txt"]
    assert ls_result.entries == [{"path": "/docs/child.txt", "is_dir": False}]
    assert backend.ls_calls == ["/workspace/docs"]
    assert glob_result.matches == [{"path": "/src/nested.py", "is_dir": False}]
    assert backend.glob_calls == [("**/*.py", "/workspace/src")]
    assert grep_result.matches == [{"path": "/match.py", "line": 3, "text": "needle"}]
    assert backend.grep_calls == [("needle", "/workspace", "*.py", 4)]


def test_text_operation_errors_do_not_expose_physical_root() -> None:
    backend = _RecordingBackend()
    backend.read_error = "permission_denied"
    rooted = _rooted(backend)

    result = rooted.read("/secret.txt")

    assert result.error == "File '/secret.txt': permission_denied"
    assert "/workspace" not in result.error


def test_error_restoration_only_rewrites_workspace_path_boundaries() -> None:
    backend = _RecordingBackend()
    backend.guard_results.append([True])
    backend.glob_error = "cannot read /workspace-old/file or /workspace/file"
    rooted = _rooted(backend)

    result = rooted.glob("*.py", "/")

    assert result.error == "cannot read /workspace-old/file or /file"


def test_delete_rejects_virtual_root_without_touching_backend() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)

    result = rooted.delete("/")

    assert result.path is None
    assert result.error is not None
    assert "invalid_path" in result.error
    assert backend.delete_calls == []


def test_search_rejects_traversal_in_path_patterns() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)

    glob_result = rooted.glob("../*.py", "/src")
    grep_result = rooted.grep("needle", "/src", "../*.py")

    assert glob_result.matches is None
    assert glob_result.error is not None
    assert "invalid_path" in glob_result.error
    assert grep_result.matches is None
    assert grep_result.error is not None
    assert "invalid_path" in grep_result.error
    assert backend.glob_calls == []
    assert backend.grep_calls == []


@pytest.mark.asyncio
async def test_upload_preserves_partial_success_and_hides_physical_errors() -> None:
    backend = _RecordingBackend()
    backend.rooted_upload_errors["/safe.bin"] = "cannot write /workspace/safe.bin"
    backend.rooted_upload_errors["/outside-link"] = "invalid_path"
    rooted = _rooted(backend)

    responses = await rooted.aupload_files(
        [
            ("/safe.bin", b"safe"),
            ("/outside-link", b"blocked"),
            ("../traversal", b"blocked"),
        ]
    )

    assert backend.rooted_upload_calls == [
        ("/workspace", "/safe.bin", b"safe"),
        ("/workspace", "/outside-link", b"blocked"),
    ]
    assert [response.path for response in responses] == [
        "/safe.bin",
        "/outside-link",
        "../traversal",
    ]
    assert responses[0].error == "cannot write /safe.bin"
    assert responses[1].error == "invalid_path"
    assert responses[2].error == "invalid_path"


@pytest.mark.asyncio
async def test_download_hides_physical_errors() -> None:
    backend = _RecordingBackend()
    backend.rooted_download_errors["/secret.bin"] = "cannot read /workspace/secret.bin"
    rooted = _rooted(backend)

    response = (await rooted.adownload_files(["/secret.bin"]))[0]

    assert response.error == "cannot read /secret.bin"
    assert "/workspace" not in response.error


@pytest.mark.asyncio
async def test_async_operations_use_native_backend_methods() -> None:
    backend = _RecordingBackend()
    backend.guard_results.extend([[True]] * 9)
    rooted = _rooted(backend)

    read_result = await rooted.aread("/read.txt", 1, 2)
    write_result = await rooted.awrite("/write.txt", "content")
    edit_result = await rooted.aedit("/edit.txt", "old", "new", True)
    delete_result = await rooted.adelete("/delete.txt")
    ls_result = await rooted.als("/docs")
    glob_result = await rooted.aglob("*.py", "/src")
    grep_result = await rooted.agrep("needle", None, "*.py", max_count=2)
    upload_result = await rooted.aupload_files([("/upload.bin", b"data")])
    download_result = await rooted.adownload_files(["/download.bin"])

    assert read_result.error is None
    assert write_result.path == "/write.txt"
    assert edit_result.path == "/edit.txt"
    assert delete_result.path == "/delete.txt"
    assert ls_result.entries == [{"path": "/docs/child.txt", "is_dir": False}]
    assert glob_result.matches == [{"path": "/src/nested.py", "is_dir": False}]
    assert grep_result.matches == [{"path": "/match.py", "line": 3, "text": "needle"}]
    assert upload_result[0].error is None
    assert download_result[0].error is None
    assert backend.read_calls == []
    assert backend.write_calls == []
    assert backend.edit_calls == []
    assert backend.delete_calls == []
    assert backend.ls_calls == []
    assert backend.glob_calls == []
    assert backend.grep_calls == []
    assert backend.upload_calls == []
    assert backend.download_calls == []
    assert backend.async_read_calls == [("/workspace/read.txt", 1, 2)]
    assert backend.async_write_calls == []
    assert backend.async_edit_calls == [("/workspace/edit.txt", "old", "new", True)]
    assert backend.async_delete_calls == ["/workspace/delete.txt"]
    assert backend.async_ls_calls == ["/workspace/docs"]
    assert backend.async_glob_calls == [("*.py", "/workspace/src")]
    assert backend.async_grep_calls == [("needle", "/workspace", "*.py", 2)]
    assert backend.async_upload_calls == []
    assert backend.async_download_calls == []
    assert backend.rooted_upload_calls == [
        ("/workspace", "/write.txt", b"content"),
        ("/workspace", "/upload.bin", b"data"),
    ]
    assert backend.rooted_download_calls == [("/workspace", "/download.bin")]


@pytest.mark.asyncio
async def test_async_upload_uses_ordered_rooted_descriptor_transfers() -> None:
    backend = _RecordingBackend()
    backend.guard_results.append([True, False])
    backend.rooted_upload_errors["/outside-link"] = "invalid_path"
    rooted = _rooted(backend)

    responses = await rooted.aupload_files(
        [
            ("../invalid", b"invalid"),
            ("/safe.bin", b"safe"),
            ("/outside-link", b"outside"),
            ("/later.bin", b"later"),
        ]
    )

    assert backend.rooted_upload_calls == [
        ("/workspace", "/safe.bin", b"safe"),
        ("/workspace", "/outside-link", b"outside"),
        ("/workspace", "/later.bin", b"later"),
    ]
    assert backend.async_upload_calls == []
    assert [(response.path, response.error) for response in responses] == [
        ("../invalid", "invalid_path"),
        ("/safe.bin", None),
        ("/outside-link", "invalid_path"),
        ("/later.bin", None),
    ]


@pytest.mark.asyncio
async def test_async_download_uses_ordered_rooted_descriptor_transfers() -> None:
    backend = _RecordingBackend()
    backend.rooted_download_errors["/outside-link"] = "invalid_path"
    rooted = _rooted(backend)

    responses = await rooted.adownload_files(
        ["../invalid", "/safe.bin", "/outside-link"]
    )

    assert backend.rooted_download_calls == [
        ("/workspace", "/safe.bin"),
        ("/workspace", "/outside-link"),
    ]
    assert backend.async_download_calls == []
    assert [
        (response.path, response.content, response.error) for response in responses
    ] == [
        ("../invalid", None, "invalid_path"),
        ("/safe.bin", b"/safe.bin", None),
        ("/outside-link", None, "invalid_path"),
    ]


@pytest.mark.asyncio
async def test_async_upload_preserves_duplicate_zero_and_large_payloads() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)
    large = b"\x00\xff" * (8 * 1024 * 1024)

    responses = await rooted.aupload_files([("/same.bin", b""), ("/same.bin", large)])

    assert [response.error for response in responses] == [None, None]
    assert backend.rooted_upload_calls[0] == (
        "/workspace",
        "/same.bin",
        b"",
    )
    assert backend.rooted_upload_calls[1][0:2] == (
        "/workspace",
        "/same.bin",
    )
    assert backend.rooted_upload_calls[1][2] is large


@pytest.mark.asyncio
async def test_async_upload_propagates_uncertain_failure_without_replay() -> None:
    backend = _RecordingBackend()
    backend.rooted_upload_exceptions["/uncertain.bin"] = ConnectionError(
        "upload response lost"
    )
    rooted = _rooted(backend)

    with pytest.raises(ConnectionError, match="response lost"):
        await rooted.aupload_files(
            [
                ("/committed.bin", b"committed"),
                ("/uncertain.bin", b"uncertain"),
                ("/not-started.bin", b"not started"),
            ]
        )

    assert backend.rooted_upload_calls == [
        ("/workspace", "/committed.bin", b"committed"),
        ("/workspace", "/uncertain.bin", b"uncertain"),
    ]


@pytest.mark.asyncio
async def test_execute_offload_maps_capture_path() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)

    result = await rooted.aexecute_with_offload(
        "generate-output",
        "/large_tool_results/call-1",
        max_inline_bytes=100,
        max_capture_bytes=200,
        timeout=3,
    )

    assert result.offloaded
    assert backend.rooted_offload_calls == [
        (
            "/workspace",
            "generate-output",
            "/large_tool_results/call-1",
            100,
            200,
            3,
        )
    ]


@pytest.mark.asyncio
async def test_execute_offload_falls_back_when_capture_path_is_unsafe() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)

    result = await rooted.aexecute_with_offload(
        "generate-output",
        "../invalid-capture",
        max_inline_bytes=100,
        timeout=3,
    )

    assert not result.offloaded
    assert result.response.output == "user output"
    assert backend.rooted_offload_calls == []
    assert backend.async_execute_calls == [("generate-output", 3)]


def test_execute_offload_rejects_external_exit_code_sidecar(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    capture_directory = workspace / "large_tool_results"
    capture_directory.mkdir(parents=True)
    outside.mkdir()
    outside_file = outside / "outside.txt"
    outside_file.write_text("unchanged", encoding="utf-8")
    capture_path = capture_directory / "call-1"
    Path(f"{capture_path}.ec").symlink_to(outside_file)
    request = _rooted_protocol._build_rooted_command(
        root=str(workspace),
        operation="offload",
        arguments={
            "path": "/large_tool_results/call-1",
            "command": "printf safe-output",
            "max_inline_bytes": 1,
            "max_capture_bytes": 1024,
            "working_directory": str(workspace),
            "command_env": {},
        },
    )

    result = _rooted_protocol._parse_rooted_response(
        _LocalTransferBackend().execute(request.command),
        request=request,
    )

    assert result.status == "ok"
    assert result.operation == "offload"
    assert result.result["offloaded"] is True
    assert capture_path.read_text(encoding="utf-8") == "safe-output"
    assert outside_file.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.asyncio
async def test_async_execute_offload_maps_capture_path() -> None:
    backend = _RecordingBackend()
    backend.guard_results.append([True, True])
    rooted = _rooted(backend)

    result = await rooted.aexecute_with_offload(
        "generate-output",
        "/large_tool_results/call-1",
        max_inline_bytes=100,
        timeout=3,
    )

    assert result.offloaded
    assert backend.offload_calls == []
    assert backend.async_offload_calls == []
    assert backend.rooted_offload_calls == [
        (
            "/workspace",
            "generate-output",
            "/large_tool_results/call-1",
            100,
            None,
            3,
        )
    ]


@pytest.mark.asyncio
async def test_async_execute_offload_uses_one_rooted_helper() -> None:
    backend = _RecordingBackend()
    backend.guard_results.append([True, True])
    rooted = _rooted(backend)

    result = await rooted.aexecute_with_offload(
        "generate-output",
        "/large_tool_results/call-1",
        max_inline_bytes=100,
        max_capture_bytes=200,
        timeout=3,
    )

    assert result.offloaded
    assert result.response.output == "rooted preview"
    assert backend.rooted_offload_calls == [
        (
            "/workspace",
            "generate-output",
            "/large_tool_results/call-1",
            100,
            200,
            3,
        )
    ]
    assert backend.async_offload_calls == []


@pytest.mark.asyncio
async def test_async_execute_uses_backend_coroutine_without_thread_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)

    async def reject_thread_bridge(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("rooted async operations must not use asyncio.to_thread")

    monkeypatch.setattr(asyncio, "to_thread", reject_thread_bridge)

    response = await rooted.aexecute("printf native", timeout=3)

    assert response.output == "user output"
    assert backend.async_execute_calls == [("printf native", 3)]


def test_invalid_only_batches_do_not_call_remote_backend() -> None:
    backend = _RecordingBackend()
    rooted = _rooted(backend)

    downloads = rooted.download_files(["../secret", "nul\x00name"])
    uploads = rooted.upload_files([("../secret", b"x"), ("nul\x00name", b"x")])
    empty_downloads = rooted.download_files([])
    empty_uploads = rooted.upload_files([])

    assert [response.error for response in downloads] == [
        "invalid_path",
        "invalid_path",
    ]
    assert [response.error for response in uploads] == [
        "invalid_path",
        "invalid_path",
    ]
    assert empty_downloads == []
    assert empty_uploads == []
    assert backend.commands == []
    assert backend.download_calls == []
    assert backend.upload_calls == []


def test_search_and_listing_keep_partial_results_while_hiding_root() -> None:
    backend = _RecordingBackend()
    backend.guard_results.extend([[True]] * 3)
    backend.ls_error = "child error under /workspace/docs"
    backend.glob_error = "glob timeout under /workspace/src"
    backend.grep_error = "context failed under /workspace"
    rooted = _rooted(backend)

    ls_result = rooted.ls("/docs")
    glob_result = rooted.glob("*.py", "/src")
    grep_result = rooted.grep("needle")

    assert ls_result.entries == [{"path": "/docs/child.txt", "is_dir": False}]
    assert ls_result.error == "child error under /docs"
    assert glob_result.matches == [{"path": "/src/nested.py", "is_dir": False}]
    assert glob_result.error == "glob timeout under /src"
    assert glob_result.truncated
    assert grep_result.matches == [{"path": "/match.py", "line": 3, "text": "needle"}]
    assert grep_result.error == "context failed under /"
    assert grep_result.truncated


def test_validation_and_operation_share_one_handle_lease() -> None:
    old_backend = _BlockingGuardBackend()
    new_backend = _RecordingBackend()
    handle = OpenSandboxHandle(cast(OpenSandboxBackend, old_backend))
    rooted = RootedOpenSandboxBackend(handle)

    with ThreadPoolExecutor(max_workers=1) as executor:
        operation = executor.submit(rooted.read, "/leased.txt")
        assert old_backend.guard_started.wait(timeout=1)
        replaced = handle._replace_backend(cast(OpenSandboxBackend, new_backend))
        old_backend.guard_release.set()
        result = operation.result(timeout=2)

    assert replaced is old_backend
    assert result.error is None
    assert old_backend.read_calls == [("/workspace/leased.txt", 0, 2000)]
    assert new_backend.read_calls == []


@pytest.mark.asyncio
async def test_queued_async_cancellation_never_starts_on_replacement_backend() -> None:
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    blocker_started = threading.Event()
    blocker_release = threading.Event()

    def occupy_only_worker() -> None:
        blocker_started.set()
        blocker_release.wait(timeout=2)

    blocker = loop.run_in_executor(None, occupy_only_worker)
    while not blocker_started.is_set():
        await asyncio.sleep(0)

    old_backend = _RecordingBackend()
    new_backend = _RecordingBackend()
    new_backend.guard_results.append([True])
    handle = OpenSandboxHandle(cast(OpenSandboxBackend, old_backend))
    rooted = RootedOpenSandboxBackend(handle)
    operation = asyncio.create_task(rooted.awrite("/late.txt", "content"))
    try:
        while not rooted._background_tasks:
            await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        _ = handle._replace_backend(cast(OpenSandboxBackend, new_backend))

        for _ in range(100):
            if not rooted._background_tasks:
                break
            await asyncio.sleep(0)
        assert not rooted._background_tasks

        blocker_release.set()
        await blocker
        assert old_backend.write_calls == []
        assert new_backend.write_calls == []
    finally:
        blocker_release.set()
        await blocker
        executor.shutdown(wait=True)
