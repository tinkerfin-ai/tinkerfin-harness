"""Own native sandbox processes without inheriting host credentials."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ConfigDict

from .errors import LocalError, LocalErrorCode

_T = TypeVar("_T")
# Resolve trusted bootstrap paths before workloads can change launcher aliases.
# The workspace denies writes to these installations and their source packages.
_PYTHON = str(Path(sys.executable).resolve(strict=True))
_SUPERVISOR = str(
    Path(__file__).resolve(strict=True).with_name("_process_supervisor.py")
)


async def finish_owned(operation: Awaitable[_T]) -> _T:
    """Settle owned work before propagating active or newly received cancellation."""
    task = asyncio.ensure_future(operation)
    cancellation = sys.exception()
    if not isinstance(cancellation, asyncio.CancelledError):
        cancellation = None
    while not task.done():
        try:
            # wait never forwards cancellation or supplier errors to the owned
            # task. Unlike shield on CPython 3.14, cancelled waiters also install
            # no callback that logs a later private cleanup exception.
            await asyncio.wait({task})
        except asyncio.CancelledError as error:
            cancellation = error
    try:
        result = task.result()
    except BaseException as error:
        if cancellation is not None and not isinstance(error, asyncio.CancelledError):
            raise cancellation from error
        raise
    if cancellation is not None:
        raise cancellation
    return result


def raise_cleanup_failures(failures: Sequence[BaseException]) -> None:
    """Preserve control exceptions after every owned cleanup has been attempted."""
    errors: list[Exception] = []
    for error in failures:
        if not isinstance(error, Exception):
            raise error
        errors.append(error)
    if errors:
        raise LocalError(
            LocalErrorCode.CLEANUP,
            "Local resources could not be fully released",
            cause=ExceptionGroup("local cleanup", errors),
        )


class _ExitStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    returncode: int


class LocalProcess:
    """Borrow bounded standard streams and the command's native exit status.

    Obtained from LocalSession.open_process. Its scope retains a live process
    group identity even after the command exits, and closes all streams on exit.
    Descendants that explicitly detach may outlive the scope; they retain their
    sandbox policy. Cancellation therefore cannot prove all external work stopped.
    """

    _leader: asyncio.subprocess.Process
    _streams: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
    _output_limit: int
    _returncode: int | None
    _closing: asyncio.Task[None] | None
    _exited: asyncio.Task[int]

    def __init__(self) -> None:
        """Reject direct construction; use LocalSession.open_process."""
        raise TypeError("LocalProcess is supplied by LocalSession")

    @classmethod
    def _create(
        cls,
        leader: asyncio.subprocess.Process,
        streams: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
        *,
        output_limit: int,
    ) -> LocalProcess:
        self = cls.__new__(cls)
        self._leader = leader
        self._streams = streams
        self._output_limit = output_limit
        self._returncode = None
        self._closing = None
        self._exited = asyncio.create_task(self._read_status())
        return self

    @property
    def pid(self) -> int:
        """Return the live group leader PID retained for this process scope."""
        return self._leader.pid

    @property
    def stdin(self) -> asyncio.StreamWriter:
        """Return the writable command input stream."""
        return self._streams[0][1]

    @property
    def stdout(self) -> asyncio.StreamReader:
        """Return the command output stream with bounded read buffering."""
        return self._streams[1][0]

    @property
    def stderr(self) -> asyncio.StreamReader:
        """Return the command diagnostic stream with bounded read buffering."""
        return self._streams[2][0]

    @property
    def returncode(self) -> int | None:
        """Return the reported exit status or scope-stop signal; None is pending.

        A negative scope-stop signal does not prove detached work has stopped.
        """
        return self._returncode

    async def _read_status(self) -> int:
        try:
            content = await self._streams[3][0].readline()
            if content:
                self._returncode = _ExitStatus.model_validate_json(content).returncode
            else:
                self._returncode = await self._leader.wait()
        except ValueError as error:
            raise LocalError(
                LocalErrorCode.PROTOCOL, "Local process status is invalid", cause=error
            ) from error
        return self._returncode

    async def wait(self) -> int:
        """Wait for the command to exit without ending its resource scope."""
        await asyncio.wait({self._exited})
        return self._exited.result()

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        """Exchange command input and collect output within the workspace byte limit."""
        result = await communicate(self, content=input, output_limit=self._output_limit)
        return result.stdout, result.stderr

    async def _stop(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await finish_owned(self._closing)

    async def _close(self) -> None:
        try:
            # The trusted anchor never exits after command completion. If an
            # external actor already killed it, do not reuse its reaped PID.
            if self._leader.returncode is None:
                try:
                    os.killpg(self._leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await self._leader.wait()
            await self._exited
        finally:
            await _close_streams(self._streams)


async def _close_streams(
    streams: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
) -> None:
    for _reader, writer in streams:
        # Shutdown must not wait for an unresponsive detached process to read
        # buffered stdin. Accepted command I/O has already completed or failed.
        # A graceful close may already have drained and released its event loop.
        # Abort only live or buffered transports; closing buffered stdin must
        # still stop immediately when the peer never consumes the pending bytes.
        if not writer.is_closing() or writer.transport.get_write_buffer_size():
            writer.transport.abort()
    for _reader, writer in streams:
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def start_process(
    arguments: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    output_limit: int = 4 * 1024 * 1024,
) -> LocalProcess:
    """Start a private group anchor with separately owned standard-stream sockets."""
    pairs = [socket.socketpair() for _ in range(4)]
    streams: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    leader: asyncio.subprocess.Process | None = None
    try:
        for parent, _child in pairs:
            parent.setblocking(False)
            streams.append(
                await asyncio.open_connection(
                    sock=parent, limit=64 * 1024 if len(streams) < 3 else 512
                )
            )
        leader = await asyncio.create_subprocess_exec(
            _PYTHON,
            "-I",
            "-B",
            _SUPERVISOR,
            str(pairs[3][1].fileno()),
            *arguments,
            cwd=cwd,
            env=env,
            stdin=pairs[0][1].fileno(),
            stdout=pairs[1][1].fileno(),
            stderr=pairs[2][1].fileno(),
            pass_fds=(pairs[3][1].fileno(),),
            start_new_session=True,
        )
        return LocalProcess._create(leader, streams, output_limit=output_limit)
    except BaseException:
        # Closing the control socket also stops a supervisor whose process
        # creation completed while the caller's opening task was cancelled.
        await _close_streams(streams)
        if leader is not None:
            await leader.wait()
        raise
    finally:
        for index, (parent, child) in enumerate(pairs):
            child.close()
            if index >= len(streams):
                parent.close()


async def stop_process(process: LocalProcess) -> None:
    """Close an owned group scope without signalling a reaped command PID."""
    await process._stop()


class ProcessOwner:
    """Retain process creation and exactly one settlement through caller cancellation."""

    def __init__(self, opening: asyncio.Task[LocalProcess]) -> None:
        self.opening = opening
        self._closing: asyncio.Task[None] | None = None

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await finish_owned(self._closing)

    async def _close(self) -> None:
        try:
            process = await self.opening
        except OSError:
            return
        await stop_process(process)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Capture bounded bytes and the actual native exit status."""

    stdout: bytes
    stderr: bytes
    exit_code: int


async def communicate(
    process: LocalProcess,
    *,
    content: bytes | None,
    output_limit: int,
) -> ProcessResult:
    """Drain both pipes concurrently with one combined byte budget."""
    total = 0

    async def read(stream: asyncio.StreamReader | None) -> bytes:
        nonlocal total
        assert stream is not None
        result = bytearray()
        while chunk := await stream.read(64 * 1024):
            total += len(chunk)
            if total > output_limit:
                raise LocalError(
                    LocalErrorCode.LIMIT, "Local output exceeds its byte limit"
                )
            result.extend(chunk)
        return bytes(result)

    async def write() -> None:
        assert process.stdin is not None
        try:
            if content:
                process.stdin.write(content)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    # Tasks belong to this exchange; the context joins or cancels every reader
    # and writer before its caller closes the process group.
    try:
        async with asyncio.TaskGroup() as group:
            out = group.create_task(read(process.stdout))
            err = group.create_task(read(process.stderr))
            group.create_task(write())
            exited = group.create_task(process.wait())
    except ExceptionGroup as errors:
        for error in errors.exceptions:
            if isinstance(error, LocalError):
                raise error from errors
        raise LocalError(
            LocalErrorCode.PROTOCOL, "Local process exchange failed", cause=errors
        ) from errors
    return ProcessResult(out.result(), err.result(), exited.result())


def safe_environment(executable: str) -> dict[str, str]:
    """Give the trusted launcher only system configuration and its tool search path."""
    from pathlib import Path

    values = {
        "PATH": os.pathsep.join(
            dict.fromkeys(
                (
                    str(Path(executable).parent),
                    "/usr/local/bin",
                    "/opt/homebrew/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                )
            )
        ),
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "UTF-8",
    }
    return values


def command_environment(
    *, home: str, user_home: str, executable: str, additional: Mapping[str, str]
) -> dict[str, str]:
    """Construct an explicit child environment after sandbox activation.

    The trusted launcher never uses project-installed binaries or Python paths.
    HOME retains native tilde semantics; temporary files and managed Python
    commands use a separate private directory. Host secrets are not inherited.
    """
    from pathlib import Path

    values = {
        "HOME": user_home,
        "TMPDIR": home,
        "PATH": os.pathsep.join(
            (
                str(Path(home) / "bin"),
                str(Path(executable).parent),
                "/usr/local/bin",
                "/opt/homebrew/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            )
        ),
        "LANG": "en_US.UTF-8",
        "PYTHONNOUSERSITE": "1",
        # macOS curl defaults to /etc/ssl/cert.pem. Seatbelt's canonical read
        # grant covers /private/etc; using that same bundle avoids opening the
        # /etc alias or weakening verification (native network contract).
        "SSL_CERT_FILE": "/private/etc/ssl/cert.pem",
        "CURL_CA_BUNDLE": "/private/etc/ssl/cert.pem",
        **additional,
    }
    for key, value in values.items():
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError(
                "environment keys and values must be valid process strings"
            )
    return values
