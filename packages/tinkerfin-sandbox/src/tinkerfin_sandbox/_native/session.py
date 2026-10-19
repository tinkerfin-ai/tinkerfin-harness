"""Native command and filesystem access within a revocable resource scope."""

from __future__ import annotations

import asyncio
import codecs
import json
import math
import sysconfig
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)
from pydantic import JsonValue, TypeAdapter, ValidationError

from ._environment import PAYLOAD_KEY
from ._file_request import FileRequest
from ._files import LocalFiles
from ._paths import local_path
from ._process import (
    LocalProcess,
    ProcessOwner,
    command_environment,
    communicate,
    finish_owned,
    raise_cleanup_failures,
    safe_environment,
    start_process,
)
from .errors import LocalError, LocalErrorCode
from .policy import LocalLimits

_RESOLVED_PATH = TypeAdapter[str | None](str | None)
_ENVIRONMENT = str(Path(__file__).resolve(strict=True).with_name("_environment.py"))


class LocalSession(SandboxBackendProtocol):
    """Borrow isolated native files and commands within an explicit resource scope.

    Instances are supplied by LocalWorkspace.open or prepare and expire when that context
    exits. Relative file paths and shell commands use directory as their working
    directory; absolute paths and ~/ paths address the native user filesystem.
    Synchronous backend operations are deliberately unsupported.
    """

    _directory: Path
    _command_prefix: tuple[str, ...]
    _python: Path
    _home: Path
    _user_home: Path
    _identity: str
    _limits: LocalLimits
    _environment: dict[str, str]
    _on_command_abort: Callable[[], Awaitable[None]] | None
    _closed: bool
    _closing: asyncio.Task[None] | None
    _processes: set[ProcessOwner]
    _files: LocalFiles

    def __init__(self) -> None:
        """Reject direct construction; borrow a session from LocalWorkspace."""
        raise TypeError("LocalSession is supplied by LocalWorkspace.open")

    @classmethod
    def _create(
        cls,
        *,
        directory: Path,
        command_prefix: tuple[str, ...],
        python: Path,
        home: Path,
        user_home: Path,
        identity: str,
        limits: LocalLimits,
        environment: Mapping[str, str],
        on_command_abort: Callable[[], Awaitable[None]] | None,
    ) -> LocalSession:
        """Bind prepared, host-owned paths without starting a process."""
        self = cls.__new__(cls)
        self._directory = directory
        self._command_prefix = command_prefix
        self._python = python
        self._home = home
        self._user_home = user_home
        self._identity = identity
        self._limits = limits
        self._environment = dict(environment)
        self._on_command_abort = on_command_abort
        self._closed = False
        self._closing = None
        self._processes = set()
        self._files = LocalFiles(self, directory)

        return self

    @property
    def directory(self) -> Path:
        """Return the default working directory for this borrow."""
        return self._directory

    @property
    def id(self) -> str:
        """Identify this isolated borrow without exposing its host directory."""
        return self._identity

    def _require_open(self) -> None:
        if self._closed:
            raise LocalError(LocalErrorCode.CLOSED, "Local workspace access has ended")

    def to_shell_path(self, file_path: str) -> str:
        """Expand a tool path to its native absolute command path.

        Relative paths start at directory; ~/ starts at the user's home, not
        the workload's private environment. Resolution performs no disk access
        and does not grant permission to read or modify the resulting path.
        """
        self._require_open()
        return str(
            local_path(file_path, directory=self.directory, user_home=self._user_home)
        )

    async def resolve_path(
        self, path: str | None, *, operation: Literal["read", "write"]
    ) -> str:
        """Resolve a canonical native path within this session's permissions.

        Existing symlinks and parent traversal follow native filesystem order.
        Resolution creates no files or directories and accepts missing trailing
        components when their existing parent can be resolved. The result is a
        path observed now, not a reservation or an access grant; use this same
        session for subsequent I/O so its permissions are enforced again.

        Args:
            path: Native absolute path, project-relative path, or ~/ path rooted
                at the user's home. None selects the session's default directory.
            operation: Intended subsequent read or write, without testing or
                granting that access.

        Returns:
            An absolute native path with symlinks and traversal resolved.

        Raises:
            ValueError: The operation is invalid or the path cannot be resolved
                within the active filesystem permissions.
            LocalError: The session is closed or the bounded resolver fails.
            asyncio.CancelledError: Resolution is cancelled after its owned
                process has been stopped and reaped.
        """
        # Resolve symlinks under the same kernel policy as the eventual operation.
        # A write probe would create effects before approval; access is therefore
        # enforced again by the actual file operation, never inferred from access().
        self._require_open()
        if operation not in {"read", "write"}:
            raise ValueError("file operation must be read or write")
        request = FileRequest(
            operation="resolve",
            root=str(self.directory),
            user_home=str(self._user_home),
            path="." if path is None else path,
            max_file_bytes=self._limits.file_bytes,
        )
        try:
            resolved = _RESOLVED_PATH.validate_json(
                await self._request(request), strict=True
            )
        except ValidationError as error:
            raise LocalError(
                LocalErrorCode.PROTOCOL,
                "Isolated path resolution is invalid",
                cause=error,
            ) from error
        if resolved is None:
            raise ValueError("The local file path cannot be resolved")
        if not Path(resolved).is_absolute():
            raise LocalError(
                LocalErrorCode.PROTOCOL, "Isolated path resolution is not absolute"
            )
        return resolved

    @asynccontextmanager
    async def open_process(
        self, arguments: Sequence[str]
    ) -> AsyncGenerator[LocalProcess]:
        """Open owned, sandboxed standard streams for a command or local service.

        Args:
            arguments: Exact executable and arguments; no host shell expansion.

        Yields:
            Borrowed process with piped stdin/stdout/stderr. The caller drains
            output with a bounded consumer; leaving the context stops the owned
            process group, including on cancellation or early exit.

        Raises:
            LocalError: The workspace is closed or a process cannot be created.
        """
        self._require_open()
        if not arguments or any("\0" in item for item in arguments):
            raise ValueError("arguments require a valid executable")
        if len(self._processes) >= self._limits.processes:
            raise LocalError(LocalErrorCode.LIMIT, "Local process capacity is full")
        command = (
            *self._command_prefix,
            str(self._python),
            "-I",
            "-B",
            _ENVIRONMENT,
            *arguments,
        )
        environment = safe_environment(self._command_prefix[0])
        # Launchers receive workload variables only as inert data, not argv or
        # active loader configuration. The fixed helper applies them in-sandbox.
        environment[PAYLOAD_KEY] = json.dumps(
            command_environment(
                home=str(self._home),
                user_home=str(self._user_home),
                executable=str(self._python),
                additional=self._environment,
            ),
            ensure_ascii=False,
        )
        opening = asyncio.create_task(
            start_process(
                command,
                cwd=self.directory,
                env=environment,
                output_limit=self._limits.output_bytes,
            )
        )
        owner = ProcessOwner(opening)
        self._processes.add(owner)
        try:
            try:
                await asyncio.wait({opening})
                process = opening.result()
            except OSError as error:
                raise LocalError(
                    LocalErrorCode.UNAVAILABLE,
                    "Local process could not start",
                    cause=error,
                ) from error
            self._require_open()
            yield process
        finally:
            try:
                await finish_owned(owner.close())
            finally:
                self._processes.discard(owner)

    async def _run(
        self,
        arguments: Sequence[str],
        *,
        content: bytes | None = None,
        output_limit: int | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes, int]:
        selected_timeout = self._command_timeout(timeout)
        try:
            async with self.open_process(arguments) as process:
                async with asyncio.timeout(selected_timeout):
                    result = await communicate(
                        process,
                        content=content,
                        output_limit=output_limit or self._limits.output_bytes,
                    )
                return result.stdout, result.stderr, result.exit_code
        except TimeoutError as error:
            raise LocalError(
                LocalErrorCode.TIMEOUT,
                "Local command exceeded its deadline",
                cause=error,
            ) from error

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Reject synchronous execution; use aexecute in asynchronous applications."""
        raise NotImplementedError("LocalSession requires aexecute")

    def _command_timeout(self, timeout: float | None) -> float:
        selected = self._limits.command_timeout if timeout is None else timeout
        if not math.isfinite(selected) or selected <= 0:
            raise ValueError("timeout must be positive")
        if selected > self._limits.command_timeout:
            raise ValueError("timeout exceeds the workspace command limit")
        return selected

    async def _command_aborted(self, error: BaseException) -> None:
        if self._on_command_abort is None:
            return
        try:
            await finish_owned(self._on_command_abort())
        except BaseException as cleanup:
            # Preserve the command failure and its private cleanup cause. A
            # new control exception still takes precedence over ordinary errors.
            if cleanup is error or (
                isinstance(error, Exception) and not isinstance(cleanup, Exception)
            ):
                raise
            raise error from cleanup

    async def aexecute(
        self, command: str, *, timeout: int | None = None
    ) -> ExecuteResponse:
        """Run a shell command under the captured workspace permissions.

        Args:
            command: Shell command evaluated within this session's permissions.
            timeout: Deadline in seconds, at most LocalLimits.command_timeout.
                None uses the workspace limit.

        Returns:
            Captured output and exit status. Nonzero exits are normal results.

        Raises:
            ValueError: The timeout is invalid; no command cleanup is invoked.
            LocalError: Execution fails after owned processes and the optional
                on_command_abort callback settle. Callback failures remain causes
                of the original failure rather than replacing it.
            asyncio.CancelledError: The caller cancelled; process cleanup and the
                optional callback are awaited before cancellation propagates.
        """
        self._require_open()
        selected_timeout = self._command_timeout(timeout)
        try:
            out, err, code = await self._run(
                ("/bin/sh", "-c", command), timeout=selected_timeout
            )
        except BaseException as error:
            await self._command_aborted(error)
            raise
        return ExecuteResponse(
            output=(out + err).decode("utf-8", errors="replace"),
            exit_code=code,
            truncated=False,
        )

    async def aexecute_stream(
        self,
        command: str,
        *,
        emit_output: Callable[[str], Awaitable[None]],
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Run a command and await delivery of incremental UTF-8 output.

        Stdout and stderr retain their own byte order and decoder state. Their
        chunks are delivered in the order observed by concurrent readers; the
        final output is the concatenation of those chunks. Invalid UTF-8 uses
        replacement characters. The combined raw-byte limit applies before
        decoding, and a bounded queue propagates consumer backpressure.

        Args:
            command: Shell command evaluated within this session's permissions.
            emit_output: Async consumer, awaited serially for every nonempty
                chunk. Its wait counts toward the command deadline. A consumer
                failure stops the owned process group and becomes LocalError.
            timeout: Command deadline in seconds, at most LocalLimits.command_timeout.
                None uses the workspace limit.

        Returns:
            Observed output and the native exit status after both pipes and
            all consumer calls finish. Nonzero exit codes remain normal results.

        Raises:
            ValueError: The timeout is outside the workspace's allowed range.
            LocalError: Startup, output capacity, consumer delivery, or deadline
                failure. The optional on_command_abort callback settles after
                process cleanup. Output already delivered cannot be withdrawn.
            asyncio.CancelledError: The caller cancelled; owned processes and
                reader tasks are closed and on_command_abort is awaited before
                cancellation propagates. Without host cleanup, independently
                owned services can retain external effects.
        """
        self._require_open()
        selected_timeout = self._command_timeout(timeout)
        chunks: list[str] = []
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=8)
        total = 0

        async def read(stream: asyncio.StreamReader) -> None:
            nonlocal total
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while raw := await stream.read(64 * 1024):
                total += len(raw)
                if total > self._limits.output_bytes:
                    raise LocalError(
                        LocalErrorCode.LIMIT, "Local output exceeds its byte limit"
                    )
                text = decoder.decode(raw)
                if text:
                    await queue.put(text)
            trailing = decoder.decode(b"", final=True)
            if trailing:
                await queue.put(trailing)
            await queue.put(None)

        async def deliver() -> None:
            ended = 0
            while ended < 2:
                text = await queue.get()
                if text is None:
                    ended += 1
                    continue
                chunks.append(text)
                try:
                    await emit_output(text)
                except Exception as error:
                    raise LocalError(
                        LocalErrorCode.PROTOCOL,
                        "Local output consumer failed",
                        cause=error,
                    ) from error

        try:
            async with self.open_process(("/bin/sh", "-c", command)) as process:
                process.stdin.close()
                async with asyncio.timeout(selected_timeout):
                    # This exchange owns both readers and its sole consumer. An
                    # exception cancels and joins them before open_process closes
                    # the group, so neither buffered output nor delivery is detached.
                    try:
                        async with asyncio.TaskGroup() as group:
                            group.create_task(read(process.stdout))
                            group.create_task(read(process.stderr))
                            group.create_task(deliver())
                            exited = group.create_task(process.wait())
                    except ExceptionGroup as errors:
                        for error in errors.exceptions:
                            if isinstance(error, LocalError):
                                raise error from errors
                        raise LocalError(
                            LocalErrorCode.PROTOCOL,
                            "Local streamed output failed",
                            cause=errors,
                        ) from errors
                return ExecuteResponse(
                    output="".join(chunks), exit_code=exited.result(), truncated=False
                )
        except TimeoutError as error:
            failure = LocalError(
                LocalErrorCode.TIMEOUT,
                "Local command exceeded its deadline",
                cause=error,
            )
            await self._command_aborted(failure)
            raise failure from error
        except BaseException as error:
            await self._command_aborted(error)
            raise

    async def _request(self, request: FileRequest) -> bytes:
        payload = request.model_dump_json().encode()
        limit = (self._limits.file_bytes * 4 // 3) + self._limits.output_bytes
        if len(payload) > limit:
            raise LocalError(
                LocalErrorCode.LIMIT, "Local file request exceeds its byte limit"
            )
        # A venv launcher can traverse an interpreter-manager alias outside
        # the runtime grant. Execute its canonical binary and load only this
        # application's trusted site directory; never grant the alias parent.
        bootstrap = (
            "import site, runpy; site.addsitedir("
            + repr(sysconfig.get_path("purelib"))
            + "); runpy.run_module('tinkerfin_sandbox._native._file_worker', run_name='__main__')"
        )
        out, _err, code = await self._run(
            (str(self._python), "-I", "-B", "-c", bootstrap),
            content=payload,
            output_limit=limit,
        )
        if code != 0:
            raise LocalError(
                LocalErrorCode.PROTOCOL,
                "Isolated file operation failed",
                context={"exit_code": code},
            )
        return out

    async def als(self, path: str) -> LsResult:
        """List a local directory permitted by this session's policy."""
        self._require_open()
        return await self._files.als(path)

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        """Read a bounded slice from an authorized native file path."""
        self._require_open()
        return await self._files.aread(file_path, offset=offset, limit=limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Write to a native file path subject to the workspace's access policy."""
        self._require_open()
        return await self._files.awrite(file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Apply an exact-match edit within the active file permissions."""
        self._require_open()
        return await self._files.aedit(
            file_path, old_string, new_string, replace_all=replace_all
        )

    async def adelete(self, file_path: str) -> DeleteResult:
        """Delete a local file permitted by this session's policy."""
        self._require_open()
        return await self._files.adelete(file_path)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find accessible local paths using bounded glob semantics."""
        self._require_open()
        return await self._files.aglob(pattern, path)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Search local file contents with an explicit match-count bound."""
        self._require_open()
        return await self._files.agrep(pattern, path, glob, max_count=max_count)

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """Write bounded binary files using the same native paths as file tools."""
        self._require_open()
        return await self._files.aupload_files(files)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read bounded binary files permitted by the session's policy."""
        self._require_open()
        return await self._files.adownload_files(paths)

    def _mcp_timeout(self, timeout: float) -> None:
        self._require_open()
        if (
            not math.isfinite(timeout)
            or not 0 < timeout <= self._limits.command_timeout
        ):
            raise ValueError(
                "MCP timeout must be positive and within the workspace limit"
            )

    async def mcp_tools(
        self, command: Sequence[str], *, timeout: float = 60
    ) -> tuple[dict[str, JsonValue], ...]:
        """List tools from a host-authorized MCP command inside this sandbox.

        Each operation opens and closes its own server session. Servers must
        keep durable business state outside a connection. Install the mcp extra;
        host credentials and environment are not inherited. Timeout includes
        startup and all catalog pages, and cannot exceed the workspace limit.

        Args:
            command: Trusted executable and arguments authorized by the host.
            timeout: Maximum operation time in seconds.

        Returns:
            At most 256 MCP tool descriptors, including their input schemas.

        Raises:
            LocalError: Missing capability, invalid protocol, excess output or timeout.
        """
        self._mcp_timeout(timeout)
        try:
            from ._mcp import list_tools
        except ImportError as error:
            raise LocalError(
                LocalErrorCode.UNAVAILABLE,
                "Install tinkerfin-sandbox[mcp] to use MCP",
                cause=error,
            ) from error
        return await list_tools(self, command, timeout)

    async def call_mcp(
        self,
        command: Sequence[str],
        name: str,
        arguments: dict[str, JsonValue],
        *,
        timeout: float = 60,
    ) -> dict[str, JsonValue]:
        """Call one MCP tool in an isolated, bounded server session.

        Args:
            command: Host-authorized executable and arguments, never model-selected code.
            name: Tool name obtained from the server's catalog.
            arguments: JSON inputs described by that tool's MCP schema.
            timeout: Maximum operation time in seconds, including startup and shutdown.

        Returns:
            The MCP tool result, including its content and error indicator.

        Raises:
            LocalError: Missing capability, invalid protocol, excess output or timeout.
        """
        self._mcp_timeout(timeout)
        try:
            from ._mcp import call_tool
        except ImportError as error:
            raise LocalError(
                LocalErrorCode.UNAVAILABLE,
                "Install tinkerfin-sandbox[mcp] to use MCP",
                cause=error,
            ) from error
        return await call_tool(self, command, name, arguments, timeout)

    async def aclose(self) -> None:
        """Revoke access and join owned processes, preserving caller cancellation.

        Repeated callers join the same cleanup. The authorized directory remains
        caller-owned. Cleanup failures raise LocalError with private causes.
        """
        self._closed = True
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await finish_owned(self._closing)

    async def _close(self) -> None:
        results = await asyncio.gather(
            *(owner.close() for owner in tuple(self._processes)),
            return_exceptions=True,
        )
        raise_cleanup_failures(
            [result for result in results if isinstance(result, BaseException)]
        )
