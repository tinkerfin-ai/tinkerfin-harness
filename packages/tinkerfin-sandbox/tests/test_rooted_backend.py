"""Virtual-root view contracts for OpenSandbox."""

import asyncio
import base64
import json
import shlex
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from contextvars import Context
from datetime import timedelta
from typing import Any, cast

import pytest
from deepagents.backends.protocol import (
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

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxHandle,
    OpenSandboxRuntimeInfo,
    RootedOpenSandboxBackend,
)


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


def _rooted(backend: Any, *, root: str = "/workspace") -> RootedOpenSandboxBackend:
    return RootedOpenSandboxBackend(OpenSandboxHandle(backend), root=root)


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
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cancelled_upload_settles_before_caller_can_continue(
    cleanup_fails: bool,
) -> None:
    class UploadBackend(_RecordingBackend):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release_upload = asyncio.Event()
            self.release_cleanup = asyncio.Event()
            self.finished = asyncio.Event()
            self.cleanup_finished = False
            self.files: dict[str, bytes] = {}

        async def _aupload_rooted_file(
            self, *, root: str, path: str, content: bytes
        ) -> FileUploadResponse:
            self.started.set()
            try:
                await self.release_upload.wait()
                self.files[path] = content
                return FileUploadResponse(path=path, error=None)
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release_cleanup.wait()
                self.cleanup_finished = True
                if cleanup_fails:
                    raise OSError("upload cleanup failed") from None
                raise
            finally:
                self.finished.set()

    backend = UploadBackend()
    rooted = _rooted(backend)
    operation = asyncio.create_task(rooted.aupload_files([("/pending.bin", b"data")]))
    cancellation = asyncio.create_task(backend.cancelled.wait())
    try:
        await backend.started.wait()
        operation.cancel("caller cancelled")
        await asyncio.wait(
            (operation, cancellation), return_when=asyncio.FIRST_COMPLETED
        )
        assert backend.cancelled.is_set()
        assert not operation.done()
        operation.cancel("repeated cancellation")
        backend.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await operation
        assert raised.value.args == ("caller cancelled",)
        assert backend.cleanup_finished
        assert backend.finished.is_set()
        assert backend.files == {}
        if cleanup_fails:
            assert isinstance(raised.value.__cause__, OSError)
        backend.release_upload.set()
        response = await rooted.aupload_files([("/replacement.bin", b"replacement")])
        assert response[0].error is None
        assert backend.files == {"/replacement.bin": b"replacement"}
    finally:
        backend.release_upload.set()
        backend.release_cleanup.set()
        await backend.finished.wait()
        if not operation.done():
            operation.cancel()
        cancellation.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_pending_async_call_cannot_use_a_replacement_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    queued: asyncio.Future[None] = loop.create_future()
    release = asyncio.Event()
    create_task = loop.create_task
    owned: list[asyncio.Task[WriteResult]] = []
    old_backend = _RecordingBackend()
    new_backend = _RecordingBackend()
    handle = OpenSandboxHandle(cast(OpenSandboxBackend, old_backend))
    rooted = RootedOpenSandboxBackend(handle)

    def queue(
        coroutine: Coroutine[None, None, WriteResult],
        *,
        name: str | None = None,
        context: Context | None = None,
    ) -> asyncio.Task[WriteResult]:
        async def delayed() -> WriteResult:
            queued.set_result(None)
            await release.wait()
            return await coroutine

        child = create_task(delayed(), name=name, context=context)
        # The callback closes the accepted coroutine even if the wrapper is
        # cancelled before its first step. The test owns both task and input.
        child.add_done_callback(lambda _task: coroutine.close())
        owned.append(child)
        return child

    operation = asyncio.create_task(rooted.awrite("/late.txt", "content"))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(loop, "create_task", queue)
            done, _pending = await asyncio.wait(
                (operation, queued), return_when=asyncio.FIRST_COMPLETED
            )
            if operation in done:
                await operation
                pytest.fail("The operation completed before its call was queued")
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        handle._replace_backend(cast(OpenSandboxBackend, new_backend))
        release.set()
        await asyncio.gather(*owned, return_exceptions=True)
        assert old_backend.rooted_upload_calls == []
        assert new_backend.rooted_upload_calls == []
    finally:
        queued.cancel()
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, *owned, return_exceptions=True)
