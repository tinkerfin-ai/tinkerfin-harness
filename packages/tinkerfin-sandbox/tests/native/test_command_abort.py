"""Command failures settle host-owned service cleanup without replacing failures."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any, Literal

import pytest

from tinkerfin_sandbox import LocalError, LocalErrorCode, LocalLimits, LocalWorkspace


@pytest.mark.parametrize(
    ("mode", "ending", "cleanup_fails"),
    [
        ("buffer", "cancel", False),
        ("stream", "cancel", False),
        ("buffer", "timeout", False),
        ("stream", "timeout", False),
        ("stream", "cancel", True),
    ],
)
async def test_aborted_commands_wait_for_service_cleanup_after_process_exit(
    tmp_path: Path,
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["buffer", "stream"],
    ending: Literal["cancel", "timeout"],
    cleanup_fails: bool,
) -> None:
    ready, cleanup_started, release_cleanup = (asyncio.Event() for _ in range(3))
    processes: list[asyncio.subprocess.Process] = []
    handlers: set[asyncio.Task[None]] = set()
    original_spawn = asyncio.create_subprocess_exec
    cleanup_error = OSError("owned service cleanup failed")
    cleanup_calls = 0

    async def capture(*arguments: str, **options: Any) -> asyncio.subprocess.Process:
        process = await original_spawn(*arguments, **options)
        processes.append(process)
        return process

    async def connected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            assert await reader.readexactly(5) == b"ready"
            ready.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.add(asyncio.create_task(connected(reader, writer)))

    class Deadline:
        owner: asyncio.Task[object] | None = None
        expired = False

        async def __aenter__(self) -> None:
            self.owner = asyncio.current_task()

        async def __aexit__(
            self, kind: object, error: BaseException | None, traceback: object
        ) -> None:
            if self.expired and isinstance(error, asyncio.CancelledError):
                raise TimeoutError from error

        def expire(self) -> None:
            assert self.owner is not None
            self.expired = True
            self.owner.cancel()

    deadline = Deadline()
    timeout = asyncio.timeout

    def controlled_timeout(seconds: float | None) -> Deadline | asyncio.Timeout:
        return deadline if seconds == 9 else timeout(seconds)

    async def close_service() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        assert processes and all(
            process.returncode is not None for process in processes
        )
        cleanup_started.set()
        await release_cleanup.wait()
        if cleanup_fails:
            raise cleanup_error

    async def emit(_text: str) -> None:
        pass

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    script = f"import socket; peer=socket.create_connection(('127.0.0.1',{port})); peer.sendall(b'ready'); peer.recv(1)"
    try:
        async with (
            server,
            LocalWorkspace(
                tmp_path, sandbox_executable=sandbox, on_command_abort=close_service
            ) as workspace,
        ):
            async with workspace.open() as session:
                monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
                if ending == "timeout":
                    monkeypatch.setattr(asyncio, "timeout", controlled_timeout)
                command = "python -I -c " + shlex.quote(script)
                operation = asyncio.create_task(
                    session.aexecute(command, timeout=9)
                    if mode == "buffer"
                    else session.aexecute_stream(command, emit_output=emit, timeout=9)
                )
                entered = asyncio.create_task(ready.wait())
                cleaning = asyncio.create_task(cleanup_started.wait())
                try:
                    done, _ = await asyncio.wait(
                        (operation, entered, cleaning),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    assert entered in done
                    if ending == "cancel":
                        operation.cancel()
                    else:
                        deadline.expire()
                    done, _ = await asyncio.wait(
                        (operation, cleaning), return_when=asyncio.FIRST_COMPLETED
                    )
                    assert cleaning in done
                    assert not operation.done()
                    release_cleanup.set()
                    if ending == "cancel":
                        with pytest.raises(asyncio.CancelledError) as error:
                            await operation
                        assert operation.cancelled()
                        if cleanup_fails:
                            assert error.value.__cause__ is cleanup_error
                    else:
                        with pytest.raises(LocalError) as failure:
                            await operation
                        assert failure.value.code is LocalErrorCode.TIMEOUT
                    assert cleanup_calls == 1
                finally:
                    release_cleanup.set()
                    for task in (operation, entered, cleaning):
                        task.cancel()
                    await asyncio.gather(
                        operation, entered, cleaning, return_exceptions=True
                    )
    finally:
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.parametrize("mode", ["output", "consumer"])
async def test_abort_cleanup_failure_retains_the_original_command_error(
    tmp_path: Path, sandbox: Path, mode: Literal["output", "consumer"]
) -> None:
    cleanup_error = OSError("owned cleanup failure")
    consumer_error = RuntimeError("owned consumer failure")
    calls = 0

    async def close_service() -> None:
        nonlocal calls
        calls += 1
        raise cleanup_error

    async def emit(_text: str) -> None:
        raise consumer_error

    async with LocalWorkspace(
        tmp_path,
        sandbox_executable=sandbox,
        limits=LocalLimits(output_bytes=1024),
        on_command_abort=close_service,
    ) as workspace:
        async with workspace.open() as session:
            with pytest.raises(LocalError) as error:
                if mode == "output":
                    await session.aexecute("python -I -c \"print('x' * 2048)\"")
                else:
                    await session.aexecute_stream("printf output", emit_output=emit)
            assert error.value.code is (
                LocalErrorCode.LIMIT if mode == "output" else LocalErrorCode.PROTOCOL
            )
            assert error.value.__cause__ is cleanup_error
            if mode == "consumer":
                assert error.value.cause is consumer_error
            assert calls == 1


async def test_non_aborted_commands_and_other_capabilities_do_not_close_services(
    tmp_path: Path, sandbox: Path
) -> None:
    calls = 0

    async def close_service() -> None:
        nonlocal calls
        calls += 1

    async def emit(_text: str) -> None:
        pass

    async with LocalWorkspace(
        tmp_path,
        sandbox_executable=sandbox,
        limits=LocalLimits(file_bytes=8),
        on_command_abort=close_service,
    ) as workspace:
        async with workspace.open() as session:
            assert (await session.aexecute("printf normal")).exit_code == 0
            assert (await session.aexecute("exit 7")).exit_code == 7
            assert (
                await session.aexecute_stream("exit 7", emit_output=emit)
            ).exit_code == 7
            with pytest.raises(ValueError):
                await session.aexecute("true", timeout=0)
            with pytest.raises(ValueError):
                await session.aexecute_stream("true", emit_output=emit, timeout=0)
            with pytest.raises(LocalError):
                await session.aupload_files([("large", b"x" * 9)])
            assert (await session.awrite("small", "file")).error is None
            with pytest.raises(RuntimeError, match="owned body failure"):
                async with session.open_process(("/usr/bin/true",)):
                    raise RuntimeError("owned body failure")
        with pytest.raises(LocalError):
            await session.aexecute("true")
    assert calls == 0
