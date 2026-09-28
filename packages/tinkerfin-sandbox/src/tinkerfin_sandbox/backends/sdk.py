"""Adapt the native async OpenSandbox SDK to the Deep Agents Sandbox protocol.

This module converts command, file, and lifecycle calls without creating, binding, or
reusing Sandboxes. The backend owns an async ``Sandbox`` connection. Synchronous
protocol methods retain the Deep Agents shape but reject remote I/O explicitly.
"""

from __future__ import annotations

import asyncio
import math
import posixpath
import secrets
import shlex
from collections.abc import Awaitable, Callable, Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Literal, NoReturn, TypeVar

import httpx
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    IS_DIRECTORY,
    PERMISSION_DENIED,
    DeleteResult,
    ExecuteOffloadResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from opensandbox import Sandbox
from opensandbox.exceptions import (
    InvalidArgumentException,
    SandboxApiException,
    SandboxInternalException,
    SandboxUnhealthyException,
)
from opensandbox.models import OutputMessage, WriteEntry
from opensandbox.models.execd import (
    Execution,
    ExecutionHandlers,
    ExecutionInit,
    RunCommandOpts,
)

from ..errors import (
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    UnexpectedOpenSandboxBackendError,
)
from ..models import OpenSandboxRuntimeInfo, OpenSandboxUnavailableReason
from ._bounded_read import await_owned_read, read_binary, validate_read_limits
from ._operations import RemoteOperations, current_remote_operations
from ._rooted_protocol import (
    _build_rooted_command,
    _build_rooted_transfer_command,
    _parse_rooted_response,
    _parse_rooted_transfer_handshake,
    _RootedError,
    _RootedTransferCommand,
    _RootedTransferHandshake,
)

_TransferT = TypeVar("_TransferT")

_ROOTED_INTERNAL_COMMAND_ENV = {
    "PYTHONNOUSERSITE": "1",
    "PYTHONPATH": "",
    "PYTHONSAFEPATH": "1",
}
_ROOTED_TRANSFER_HOLD_SECONDS = 30
_ROOTED_TRANSFER_READY_SECONDS = 5.0
_ROOTED_TRANSFER_SETTLE_SECONDS = 5.0
_ROOTED_TRANSFER_POLL_SECONDS = 0.01
_ROOTED_TRANSFER_TERMINAL_LOG_SECONDS = 0.5
_ROOTED_TRANSFER_MAX_LOG_BYTES = 64 * 1024
_ROOTED_OFFLOAD_MAX_CAPTURE_BYTES = 10 * 1024 * 1024
_TERMINAL_SANDBOX_STATES = frozenset({"failed", "stopping", "terminated"})
_INACTIVE_SANDBOX_STATES = _TERMINAL_SANDBOX_STATES | {"pausing", "paused", "resuming"}
_COMMAND_SETTLEMENT_POLL_SECONDS = 0.05
_ASYNC_ONLY_MESSAGE = (
    "OpenSandboxBackend supports asynchronous remote I/O only; "
    "use the corresponding async method"
)


class _RootedTransferOperationError(Exception):
    """Carry one confirmed helper file-condition failure through settlement."""

    def __init__(self, error: _RootedError) -> None:
        super().__init__(error.message)
        self.error = error


def _rooted_transfer_descriptor(
    content: str,
    *,
    request: _RootedTransferCommand,
) -> str | None:
    """Parse accumulated helper logs into one descriptor or confirmed error."""

    if len(content.encode("utf-8")) > _ROOTED_TRANSFER_MAX_LOG_BYTES:
        raise ValueError("rooted transfer handshake exceeded log limit")
    record = _parse_rooted_transfer_handshake(content, request=request)
    if isinstance(record, _RootedError):
        raise _RootedTransferOperationError(record)
    if isinstance(record, _RootedTransferHandshake):
        return f"/proc/{record.pid}/fd/{record.fd}"
    return None


def _join_output_messages(messages: Iterable[OutputMessage | str]) -> str:
    """Join SDK output that can contain model objects and plain strings."""
    return "\n".join(
        message if isinstance(message, str) else message.text for message in messages
    )


def _is_absolute_sandbox_path(path: str) -> bool:
    """Apply minimal path validation without replacing in-Sandbox authorization."""
    return bool(path) and path.startswith("/") and "\x00" not in path


def _is_sandbox_root_path(path: str) -> bool:
    """Return whether lexical normalization resolves an absolute path to root."""
    return posixpath.normpath("/" + path.lstrip("/")) == "/"


def _file_error(exc: Exception, *, default: str) -> str:
    """Normalize transport-specific SDK failures into Deep Agents error codes."""
    if isinstance(exc, FileNotFoundError):
        return FILE_NOT_FOUND
    if isinstance(exc, PermissionError):
        return PERMISSION_DENIED
    if isinstance(exc, IsADirectoryError):
        return IS_DIRECTORY
    if isinstance(exc, SandboxApiException):
        if exc.status_code == 404:
            return FILE_NOT_FOUND
        if exc.status_code == 403:
            return PERMISSION_DENIED

    # The SDK does not always preserve file errors as
    # SandboxApiException, so HTTP wrappers and execd text remain fallbacks.
    message = str(exc).lower()
    if "not found" in message or "no such file" in message:
        return FILE_NOT_FOUND
    if "permission" in message or "read-only" in message:
        return PERMISSION_DENIED
    if "is a directory" in message:
        return IS_DIRECTORY
    return default


def _rooted_transfer_file_error(
    error: _RootedError,
    *,
    default: str,
) -> str:
    """Map only helper-confirmed target conditions into file response codes."""
    return {
        "invalid_path": INVALID_PATH,
        "not_found": FILE_NOT_FOUND,
        "permission_denied": PERMISSION_DENIED,
        "not_a_file": IS_DIRECTORY,
        "not_directory": INVALID_PATH,
    }.get(error.code, default)


def unavailable_reason(exc: Exception) -> OpenSandboxUnavailableReason:
    """Confirm remote absence only from the SDK's structured HTTP status."""
    if isinstance(exc, SandboxApiException) and exc.status_code == 404:
        return "not_found"
    return "unreachable"


def _connection_failure_reason(error: Exception) -> str | None:
    """Separate transient failures from authorization and protocol rejections."""
    if isinstance(error, SandboxInternalException) and isinstance(
        error.__cause__, Exception
    ):
        # The locked SDK wraps HTTPX network errors in SandboxInternalException.
        # Follow its typed cause, never its provider-controlled message text.
        return _connection_failure_reason(error.__cause__)
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.NetworkError):
        return "unreachable"
    if isinstance(error, SandboxApiException):
        status = error.status_code
        if status == 404:
            return "not_found"
        if status in {408, 429, 500, 502, 503, 504}:
            return "unreachable"
        if status == 401:
            return "authentication"
        if status == 403:
            return "permission"
        return "provider_rejected"
    if isinstance(error, PermissionError):
        return "permission"
    if isinstance(error, ConnectionError):
        return "unreachable"
    if isinstance(error, SandboxUnhealthyException):
        return "unhealthy"
    return None


def _request_was_rejected(error: BaseException) -> bool:
    """Recognize explicit refusal without treating timeouts as non-execution."""
    if isinstance(error, SandboxApiException):
        status = error.status_code
        return status is not None and 400 <= status < 500 and status != 408
    return isinstance(
        error,
        (
            InvalidArgumentException,
            FileNotFoundError,
            IsADirectoryError,
            PermissionError,
        ),
    )


async def _backend_call(
    operation: str,
    awaitable: Awaitable[_TransferT],
) -> _TransferT:
    try:
        return await awaitable
    except OpenSandboxBackendError:
        raise
    except TimeoutError as error:
        translated = OpenSandboxBackendTimeoutError(
            f"OpenSandbox {operation} timed out",
            diagnostic_context={
                "implementation": "opensandbox_sdk",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error
    except SandboxApiException as error:
        translated = OpenSandboxBackendUnavailableError(
            f"OpenSandbox is unavailable for {operation}",
            context={"reason": _connection_failure_reason(error)},
            diagnostic_context={
                "implementation": "opensandbox_sdk",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        raise
    except (TypeError, ValueError) as error:
        translated = OpenSandboxBackendProtocolError(
            f"OpenSandbox {operation} returned an invalid response",
            diagnostic_context={
                "implementation": "opensandbox_sdk",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error
    except OSError as error:
        translated = OpenSandboxBackendUnavailableError(
            f"OpenSandbox is unavailable for {operation}",
            context={"reason": _connection_failure_reason(error)},
            diagnostic_context={
                "implementation": "opensandbox_sdk",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error
    except Exception as error:
        reason = _connection_failure_reason(error)
        if reason is not None:
            raise OpenSandboxBackendUnavailableError(
                f"OpenSandbox is unavailable for {operation}",
                context={"reason": reason},
                diagnostic_context={
                    "implementation": "opensandbox_sdk",
                    "operation": operation,
                },
                cause=error,
            ) from error
        translated = UnexpectedOpenSandboxBackendError(
            f"OpenSandbox {operation} failed",
            diagnostic_context={
                "implementation": "opensandbox_sdk",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error


class OpenSandboxBackend(BaseSandbox):
    """Adapt one connected asynchronous OpenSandbox instance to ``BaseSandbox``.

    This object owns the local SDK connection, while the caller decides ownership of
    the remote Sandbox. Consequently, :meth:`aclose` releases only the local
    connection and :meth:`akill` destroys the remote instance. Command failures keep
    their SDK semantics; batch file operations preserve input order and per-file
    partial success.
    """

    def __init__(
        self,
        *,
        sandbox: Sandbox,
        default_timeout: int = 60,
        command_env: dict[str, str] | None = None,
        working_directory: str | None = None,
        health_command: str = "printf ok",
        enable_capture_offload: bool = False,
    ) -> None:
        """Initialize the protocol adapter.

        Args:
            sandbox: Created or reconnected asynchronous OpenSandbox instance.
            default_timeout: Command timeout in seconds when no override is supplied;
                zero disables the SDK deadline.
            command_env: Environment variables injected into every command.
            working_directory: Default Sandbox working directory for commands.
            health_command: Lightweight data-plane probe used by runtime details.
            enable_capture_offload: Whether Deep Agents may request source-side
                output offload.

        Raises:
            ValueError: ``default_timeout`` is negative.
        """
        if default_timeout < 0:
            raise ValueError("default_timeout must not be negative")
        self._sandbox = sandbox
        self._default_timeout = default_timeout
        self._command_env = dict(command_env or {})
        self._working_directory = working_directory
        self._is_rooted_file_operation = ContextVar(
            f"opensandbox_rooted_file_operation_{id(self)}",
            default=False,
        )
        self._health_command = health_command
        self.enable_capture_offload = enable_capture_offload
        self._remote_operations = RemoteOperations()

    @contextmanager
    def _rooted_file_operation(self) -> Generator[None]:
        """Keep framework file commands outside user-writable import paths."""
        token = self._is_rooted_file_operation.set(True)
        try:
            yield
        finally:
            self._is_rooted_file_operation.reset(token)

    @property
    def id(self) -> str:
        """Return the remote Sandbox ID."""
        return self._sandbox.id

    @staticmethod
    def _reject_sync() -> NoReturn:
        """Reject implicit bridging of async remote operations to sync I/O."""
        raise OpenSandboxBackendError(_ASYNC_ONLY_MESSAGE)

    def _command_options(self, timeout: int | None) -> RunCommandOpts:
        """Build SDK options for one command invocation."""
        effective_timeout = self._default_timeout if timeout is None else timeout
        if effective_timeout < 0:
            raise ValueError("timeout must not be negative")
        sdk_timeout = (
            None if effective_timeout == 0 else timedelta(seconds=effective_timeout)
        )
        is_rooted_file_operation = self._is_rooted_file_operation.get()
        command_env = dict(self._command_env)
        if is_rooted_file_operation:
            # BaseSandbox implements file methods with python3 -c and system tools;
            # the user-writable workspace must not affect module resolution there.
            command_env.update(_ROOTED_INTERNAL_COMMAND_ENV)
        return RunCommandOpts(
            timeout=sdk_timeout,
            envs=command_env or None,
            working_directory=(
                "/" if is_rooted_file_operation else self._working_directory
            ),
        )

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Reject synchronous remote commands; use :meth:`aexecute`."""
        del command, timeout
        return self._reject_sync()

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute once and retain uncertain remote work after cancellation or failure.

        Cancellation returns to the caller while a bounded owned check interrupts
        and verifies any identified command. Unknown outcomes remain non-idle for
        lifecycle coordination. Successful commands need no extra remote request.
        """
        options = self._command_options(timeout)
        operations = current_remote_operations(self._remote_operations)
        execution_id: str | None = None

        async def initialized(event: object) -> None:
            nonlocal execution_id
            # SDK ExecutionEventDispatcher._handle_init supplies ExecutionInit,
            # although ExecutionHandlers annotates its callback input as Any.
            if not isinstance(event, ExecutionInit) or not event.id.strip():
                raise ValueError("OpenSandbox command returned an invalid execution ID")
            execution_id = event.id

        async def execute_once() -> Execution:
            try:
                execution = await self._sandbox.commands.run(
                    command,
                    opts=options,
                    handlers=ExecutionHandlers(on_init=initialized),
                )
            except BaseException as error:
                if execution_id is not None:
                    command_id = execution_id
                    operations.start_settlement(
                        lambda: self._settle_command(command_id)
                    )
                elif not _request_was_rejected(error):
                    operations.mark_unresolved()
                raise
            if execution.exit_code is None:
                # An exhausted SSE stream alone is not a terminal event. In
                # particular, SDK execution_complete for background commands only
                # confirms submission; this path uses foreground commands.
                command_id = execution_id or execution.id
                if command_id:
                    operations.start_settlement(
                        lambda: self._settle_command(command_id)
                    )
                else:
                    operations.mark_unresolved()
            return execution

        result = await _backend_call(
            "command execution",
            execute_once(),
        )
        stdout = _join_output_messages(result.logs.stdout)
        stderr = _join_output_messages(result.logs.stderr).strip()
        output_parts = [stdout] if stdout else []
        if stderr:
            output_parts.append(f"<stderr>{stderr}</stderr>")
        return ExecuteResponse(
            output="\n".join(output_parts),
            exit_code=result.exit_code,
            truncated=False,
        )

    async def _settle_command(self, execution_id: str) -> None:
        """Interrupt at most once and require the SDK's remote terminal evidence.

        RemoteOperations owns the total deadline. An interrupt response does not
        itself prove that the command has stopped, and missing status is unresolved.
        """
        status = await self._sandbox.commands.get_command_status(execution_id)
        if status.running is False or (
            status.running is None and status.exit_code is not None
        ):
            return
        await self._sandbox.commands.interrupt(execution_id)
        while True:
            status = await self._sandbox.commands.get_command_status(execution_id)
            if status.running is False or (
                status.running is None and status.exit_code is not None
            ):
                return
            await asyncio.sleep(_COMMAND_SETTLEMENT_POLL_SECONDS)

    async def _file_request(self, request: Awaitable[_TransferT]) -> _TransferT:
        """Retain uncertainty when a file request ends without a confirmed outcome.

        The SDK request is directly awaited through completion or cancellation
        cleanup. File operations expose no remote request ID for later status
        reconciliation, so an unconfirmed response cannot authorize pause.
        """
        operations = current_remote_operations(self._remote_operations)
        try:
            return await request
        except BaseException as error:
            if not _request_was_rejected(error):
                operations.mark_unresolved()
            raise

    async def _await_rooted_transfer_handshake(
        self,
        *,
        execution_id: str,
        request: _RootedTransferCommand,
    ) -> str:
        """Wait for one authenticated descriptor handshake with a finite budget."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ROOTED_TRANSFER_READY_SECONDS
        cursor: int | None = None
        content = ""
        while loop.time() < deadline:
            # Direct awaits preserve an outer cancellation when the response and
            # deadline coincide (CPython 3.11 asyncio.wait_for can swallow it).
            # Covered by test_bounded_binary_read_timeout_closes_response_and_helper.
            async with asyncio.timeout_at(deadline):
                logs = await self._sandbox.commands.get_background_command_logs(
                    execution_id,
                    cursor=cursor,
                )
            if logs.content:
                content += logs.content
                descriptor = _rooted_transfer_descriptor(content, request=request)
                if descriptor is not None:
                    return descriptor
            cursor = logs.cursor
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            async with asyncio.timeout_at(deadline):
                status = await self._sandbox.commands.get_command_status(execution_id)
            if status.running is False or (
                status.running is None and status.exit_code is not None
            ):
                # The execd service can publish terminal command status before
                # the final log cursor is visible. Drain only that cursor within
                # a bounded grace period; the contract is fixed by
                # test_rooted_descriptor_drains_final_logs_after_terminal_status.
                terminal_deadline = min(
                    deadline,
                    loop.time() + _ROOTED_TRANSFER_TERMINAL_LOG_SECONDS,
                )
                while loop.time() < terminal_deadline:
                    await asyncio.sleep(
                        min(
                            _ROOTED_TRANSFER_POLL_SECONDS,
                            max(terminal_deadline - loop.time(), 0),
                        )
                    )
                    terminal_remaining = terminal_deadline - loop.time()
                    if terminal_remaining <= 0:
                        break
                    async with asyncio.timeout_at(terminal_deadline):
                        final_logs = (
                            await self._sandbox.commands.get_background_command_logs(
                                execution_id,
                                cursor=cursor,
                            )
                        )
                    cursor = final_logs.cursor
                    if not final_logs.content:
                        continue
                    content += final_logs.content
                    descriptor = _rooted_transfer_descriptor(
                        content,
                        request=request,
                    )
                    if descriptor is not None:
                        return descriptor
                raise ValueError("rooted transfer helper exited before handshake")
            await asyncio.sleep(
                min(_ROOTED_TRANSFER_POLL_SECONDS, max(deadline - loop.time(), 0))
            )
        raise TimeoutError("rooted transfer helper handshake timed out")

    async def _settle_rooted_transfer(
        self,
        execution_id: str,
        *,
        prefer_natural_exit: bool,
    ) -> None:
        """Interrupt one helper exactly once and wait for its terminal status."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ROOTED_TRANSFER_SETTLE_SECONDS
        natural_exit_deadline = min(deadline, loop.time() + 0.5)
        initial_status = None
        while prefer_natural_exit and loop.time() < natural_exit_deadline:
            try:
                initial_status = await asyncio.wait_for(
                    self._sandbox.commands.get_command_status(execution_id),
                    timeout=natural_exit_deadline - loop.time(),
                )
            except Exception:  # noqa: BLE001 - fall through to interrupt
                break
            if initial_status.running is False or (
                initial_status.running is None and initial_status.exit_code is not None
            ):
                return
            await asyncio.sleep(
                min(
                    _ROOTED_TRANSFER_POLL_SECONDS,
                    max(natural_exit_deadline - loop.time(), 0),
                )
            )
        try:
            if initial_status is None:
                initial_status = await asyncio.wait_for(
                    self._sandbox.commands.get_command_status(execution_id),
                    timeout=min(0.5, max(deadline - loop.time(), 0)),
                )
        except Exception:  # noqa: BLE001 - interrupt when status is unavailable
            initial_status = None
        if initial_status is not None and (
            initial_status.running is False
            or (initial_status.running is None and initial_status.exit_code is not None)
        ):
            return
        await asyncio.wait_for(
            self._sandbox.commands.interrupt(execution_id),
            timeout=max(deadline - loop.time(), 0),
        )
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            status = await asyncio.wait_for(
                self._sandbox.commands.get_command_status(execution_id),
                timeout=remaining,
            )
            if status.running is False or (
                status.running is None and status.exit_code is not None
            ):
                return
            await asyncio.sleep(
                min(_ROOTED_TRANSFER_POLL_SECONDS, max(deadline - loop.time(), 0))
            )
        raise TimeoutError("rooted transfer helper did not settle")

    async def _run_rooted_transfer(
        self,
        *,
        root: str,
        path: str,
        mode: Literal["upload", "download"],
        transfer: Callable[[str], Awaitable[_TransferT]],
        hold_seconds: int = _ROOTED_TRANSFER_HOLD_SECONDS,
    ) -> _TransferT:
        """Own one background helper from startup through descriptor settlement."""
        request = _build_rooted_transfer_command(
            root=root,
            path=path,
            mode=mode,
            token=secrets.token_hex(32),
            hold_seconds=hold_seconds,
        )
        execution_id: str | None = None
        operations = current_remote_operations(self._remote_operations)
        result: _TransferT | None = None
        primary: BaseException | None = None

        async def initialized(event: object) -> None:
            nonlocal execution_id
            if not isinstance(event, ExecutionInit) or not event.id.strip():
                raise ValueError("rooted transfer helper returned no execution ID")
            execution_id = event.id

        try:
            execution = await _backend_call(
                "rooted transfer command",
                self._sandbox.commands.run(
                    request.command,
                    opts=RunCommandOpts(
                        background=True,
                        working_directory="/",
                        timeout=timedelta(seconds=hold_seconds + 5),
                        envs=dict(_ROOTED_INTERNAL_COMMAND_ENV),
                    ),
                    handlers=ExecutionHandlers(on_init=initialized),
                ),
            )
            if not isinstance(execution.id, str) or not execution.id.strip():
                raise ValueError("rooted transfer helper returned no execution ID")
            execution_id = execution.id
            descriptor_path = await self._await_rooted_transfer_handshake(
                execution_id=execution_id,
                request=request,
            )
            result = await transfer(descriptor_path)
        except BaseException as exc:  # noqa: BLE001 - settle before propagation
            primary = exc
            if execution_id is None:
                cause = exc.cause if isinstance(exc, OpenSandboxBackendError) else exc
                if cause is None or not _request_was_rejected(cause):
                    operations.mark_unresolved()

        if execution_id is not None:
            settlement = asyncio.create_task(
                self._settle_rooted_transfer(
                    execution_id,
                    prefer_natural_exit=isinstance(
                        primary,
                        _RootedTransferOperationError,
                    ),
                ),
                name=f"tinkerfin-rooted-transfer-settlement:{execution_id}",
            )
            while not settlement.done():
                try:
                    await asyncio.shield(settlement)
                except asyncio.CancelledError as repeated_cancellation:
                    if primary is None:
                        primary = repeated_cancellation
                    else:
                        primary.add_note(
                            "Rooted descriptor settlement also received caller "
                            f"cancellation: {repeated_cancellation}"
                        )
                    continue
                except BaseException:  # noqa: BLE001 - read from owned task below
                    break
            try:
                settlement.result()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve primary
                operations.mark_unresolved()
                if primary is None:
                    primary = cleanup_error
                else:
                    primary.add_note(
                        "Rooted descriptor helper settlement also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )

        if primary is not None:
            raise primary.with_traceback(primary.__traceback__)
        if result is None:
            raise RuntimeError("rooted transfer completed without a result")
        return result

    async def _aupload_rooted_file(
        self,
        *,
        root: str,
        path: str,
        content: bytes,
    ) -> FileUploadResponse:
        """Upload once through a helper-owned descriptor under ``root``."""

        async def transfer(descriptor_path: str) -> FileUploadResponse:
            await _backend_call(
                "rooted file upload",
                self._file_request(
                    self._sandbox.files.write_file(
                        descriptor_path,
                        content,
                        mode=644,
                    ),
                ),
            )
            return FileUploadResponse(path=path, error=None)

        try:
            return await self._run_rooted_transfer(
                root=root,
                path=path,
                mode="upload",
                transfer=transfer,
            )
        except _RootedTransferOperationError as exc:
            return FileUploadResponse(
                path=path,
                error=_rooted_transfer_file_error(
                    exc.error,
                    default="upload_failed",
                ),
            )

    async def aread_bytes(
        self, path: str, *, max_bytes: int, timeout: float = 30
    ) -> bytes:
        """Read one binary file, rejecting excess content without returning a prefix.

        Args:
            path: Absolute Sandbox file path; this raw backend does not confine paths.
            max_bytes: Maximum accepted file size in bytes, including zero for empty files.
            timeout: Total read deadline in seconds, greater than zero and at most 290.
                Response cleanup is awaited before cancellation or failure propagates.

        Returns:
            Complete binary content, with retained content bounded by ``max_bytes``
            plus one transport chunk and the final bytes copy.

        Raises:
            ValueError: The path or limits are invalid.
            FileNotFoundError: The file does not exist.
            PermissionError: File access is denied.
            OpenSandboxFileTooLargeError: The file exceeds ``max_bytes``.
            OpenSandboxBackendError: Download or protocol validation failed.
            OpenSandboxBackendTimeoutError: The read deadline expired.
        """
        validate_read_limits(max_bytes, timeout)
        if not _is_absolute_sandbox_path(path):
            raise ValueError("path must be an absolute Sandbox path")

        async def read() -> bytes:
            async with asyncio.timeout(timeout):
                return await read_binary(self._sandbox, path, max_bytes=max_bytes)

        return await await_owned_read(lambda: _backend_call("binary file read", read()))

    async def _aread_rooted_bytes(
        self, *, root: str, path: str, max_bytes: int, timeout: float
    ) -> bytes:
        """Pin a regular file and close the HTTP response before releasing its helper."""
        validate_read_limits(max_bytes, timeout)

        async def read() -> bytes:
            try:
                async with asyncio.timeout(timeout):
                    return await self._run_rooted_transfer(
                        root=root,
                        path=path,
                        mode="download",
                        hold_seconds=math.ceil(timeout) + 5,
                        transfer=lambda descriptor: read_binary(
                            self._sandbox, descriptor, max_bytes=max_bytes
                        ),
                    )
            except _RootedTransferOperationError as error:
                code = error.error.code
                if code == "not_found":
                    raise FileNotFoundError("Sandbox file was not found") from error
                if code == "permission_denied":
                    raise PermissionError("Sandbox file access was denied") from error
                if code == "not_a_file":
                    raise IsADirectoryError(
                        "Sandbox path is not a regular file"
                    ) from error
                raise OpenSandboxBackendProtocolError(
                    "Sandbox file path was rejected", context={"reason": code}
                ) from error

        return await await_owned_read(
            lambda: _backend_call("rooted binary file read", read())
        )

    async def _adownload_rooted_file(
        self,
        *,
        root: str,
        path: str,
    ) -> FileDownloadResponse:
        """Download once through a helper-owned descriptor under ``root``."""

        async def transfer(descriptor_path: str) -> FileDownloadResponse:
            content = await _backend_call(
                "rooted file download",
                self._file_request(self._sandbox.files.read_bytes(descriptor_path)),
            )
            return FileDownloadResponse(path=path, content=content, error=None)

        try:
            return await self._run_rooted_transfer(
                root=root,
                path=path,
                mode="download",
                transfer=transfer,
            )
        except _RootedTransferOperationError as exc:
            return FileDownloadResponse(
                path=path,
                content=None,
                error=_rooted_transfer_file_error(
                    exc.error,
                    default="download_failed",
                ),
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
        """Execute once while the helper owns atomic capture publication."""
        if not self.enable_capture_offload:
            response = await self.aexecute(command, timeout=timeout)
            return ExecuteOffloadResult(offloaded=False, response=response)
        request = _build_rooted_command(
            root=root,
            operation="offload",
            arguments={
                "path": capture_path,
                "command": command,
                "max_inline_bytes": max_inline_bytes,
                "max_capture_bytes": (
                    _ROOTED_OFFLOAD_MAX_CAPTURE_BYTES
                    if max_capture_bytes is None
                    else max_capture_bytes
                ),
                "working_directory": self._working_directory,
                "command_env": dict(self._command_env),
            },
        )
        with self._rooted_file_operation():
            raw = await self.aexecute(request.command, timeout=timeout)
        parsed = _parse_rooted_response(raw, request=request)
        if parsed.status == "error":
            raise ValueError(
                "rooted offload helper failed: "
                f"{parsed.error.code}: {parsed.error.message}"
            )
        if parsed.operation != "offload":
            raise ValueError("rooted helper returned a non-offload result")
        result = parsed.result
        return ExecuteOffloadResult(
            offloaded=result["offloaded"],
            preview_has_truncation_marker=result["preview_has_truncation_marker"],
            response=ExecuteResponse(
                output=result["output"],
                exit_code=result["exit_code"],
                truncated=result["truncated"],
            ),
        )

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Reject synchronous downloads; use :meth:`adownload_files`."""
        del paths
        return self._reject_sync()

    async def adownload_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Asynchronously download files in order with per-file failure isolation."""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if not _is_absolute_sandbox_path(path):
                responses.append(
                    FileDownloadResponse(path=path, content=None, error=INVALID_PATH)
                )
                continue
            try:
                content = await self._file_request(self._sandbox.files.read_bytes(path))
            except Exception as exc:  # noqa: BLE001 - per-file SDK isolation
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        content=None,
                        error=_file_error(exc, default="download_failed"),
                    )
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
        """Reject synchronous uploads; use :meth:`aupload_files`."""
        del files
        return self._reject_sync()

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Asynchronously upload files in order with per-file failure isolation."""
        responses: list[FileUploadResponse] = []
        for path, content in files:
            if not _is_absolute_sandbox_path(path):
                responses.append(FileUploadResponse(path=path, error=INVALID_PATH))
                continue
            try:
                parent = str(PurePosixPath(path).parent)
                if parent != "/":
                    await self._file_request(
                        self._sandbox.files.create_directories(
                            # The SDK expects octal permissions as decimal digits;
                            # Python's 0o755 serializes as "493", which execd rejects.
                            [WriteEntry(path=parent, mode=755)]
                        )
                    )
                await self._file_request(
                    self._sandbox.files.write_file(path, content, mode=644)
                )
            except Exception as exc:  # noqa: BLE001 - per-file SDK isolation
                responses.append(
                    FileUploadResponse(
                        path=path,
                        error=_file_error(exc, default="upload_failed"),
                    )
                )
            else:
                responses.append(FileUploadResponse(path=path, error=None))
        return responses

    async def adelete(self, file_path: str) -> DeleteResult:
        """Delete one absolute non-root path through native asynchronous commands."""
        if not _is_absolute_sandbox_path(file_path) or _is_sandbox_root_path(file_path):
            return DeleteResult(error=INVALID_PATH)
        quoted = shlex.quote(file_path)
        exists = await self.aexecute(f"test -e {quoted} || test -L {quoted}")
        if exists.exit_code is not None and exists.exit_code != 0:
            return DeleteResult(error=f"Error: '{file_path}' not found")
        result = await self.aexecute(f"rm -rf {quoted}")
        if result.exit_code == 0:
            return DeleteResult(path=file_path)
        message = result.output.strip() or "unknown error"
        return DeleteResult(error=f"Error deleting file '{file_path}': {message}")

    def renew(self, timeout: timedelta) -> None:
        """Reject synchronous renewal; use :meth:`arenew`."""
        del timeout
        self._reject_sync()

    async def arenew(self, timeout: timedelta) -> None:
        """Asynchronously extend remote expiry from the current time."""
        await _backend_call("renew", self._sandbox.renew(timeout))

    def close(self) -> None:
        """Reject synchronous connection closure; use :meth:`aclose`."""
        self._reject_sync()

    async def aclose(self) -> None:
        """Settle standalone remote checks, then close only the local connection.

        Managed handles own their activated trackers and await them separately.
        Unconfirmed outcomes remain recorded even after bounded checks finish.
        """
        await self._remote_operations.wait()
        await _backend_call("close", self._sandbox.close())

    def kill(self) -> None:
        """Reject synchronous remote destruction; use :meth:`akill`."""
        self._reject_sync()

    async def akill(self) -> None:
        """Destroy the remote Sandbox without implicitly closing the SDK connection."""
        await _backend_call("destroy", self._sandbox.kill())

    def get_runtime_info(self) -> OpenSandboxRuntimeInfo:
        """Reject synchronous runtime queries; use :meth:`aget_runtime_info`."""
        return self._reject_sync()

    async def aget_runtime_info(self) -> OpenSandboxRuntimeInfo:
        """Asynchronously read lifecycle details and probe data-plane health."""
        try:
            info = await self._sandbox.get_info()
        except Exception as exc:  # noqa: BLE001 - runtime info has a stable unavailable form
            return OpenSandboxRuntimeInfo.unavailable(
                self.id,
                unavailable_reason(exc),
            )

        # Terminal and suspended lifecycle states cannot serve data-plane probes.
        # In particular, a paused Sandbox keeps its lifecycle status instead of
        # waiting for a health-command timeout or implicitly resuming execution.
        if info.status.state.casefold() in _INACTIVE_SANDBOX_STATES:
            return OpenSandboxRuntimeInfo.from_sdk(info, healthy=False)

        try:
            healthy = (await self.aexecute(self._health_command)).exit_code == 0
        except Exception:  # noqa: BLE001 - health failures map to an unhealthy result
            healthy = False
        return OpenSandboxRuntimeInfo.from_sdk(info, healthy=healthy)
