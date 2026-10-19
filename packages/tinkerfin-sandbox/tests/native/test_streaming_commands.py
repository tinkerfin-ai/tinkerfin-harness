"""Incremental command output, consumer backpressure, and owned cleanup."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Literal

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_sandbox import (
    LocalAccess,
    LocalError,
    LocalErrorCode,
    LocalLimits,
    LocalWorkspace,
)

IDENTITY = RunIdentity(namespace="stream", thread_id="thread", run_id="run")


async def test_stream_delivers_before_exit_and_decodes_split_utf8_with_backpressure(
    tmp_path: Path, sandbox: Path
) -> None:
    release_path = tmp_path / "release"
    os.mkfifo(release_path)
    descriptor = os.open(release_path, os.O_RDWR | os.O_NONBLOCK)
    (tmp_path / "emit.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.stdout.buffer.write(b'A\\xf0\\x9f')\n"
        "sys.stdout.buffer.flush()\n"
        "with open('release', 'rb') as gate: gate.read(1)\n"
        "sys.stdout.buffer.write(b'\\x98\\x80B')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(b'E\\xff')\n"
        "sys.stderr.buffer.flush()\n"
        "Path('finished').write_text('done')\n"
        "sys.exit(7)\n"
    )
    received, release_consumer = asyncio.Event(), asyncio.Event()
    chunks: list[str] = []

    async def emit(text: str) -> None:
        chunks.append(text)
        if len(chunks) == 1:
            received.set()
            await release_consumer.wait()

    try:
        async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
            async with workspace.prepare(IDENTITY) as prepared:
                operation = asyncio.create_task(
                    prepared.workspace.aexecute_stream(
                        "python emit.py", emit_output=emit
                    )
                )
                signal = asyncio.create_task(received.wait())
                try:
                    await asyncio.wait(
                        (operation, signal), return_when=asyncio.FIRST_COMPLETED
                    )
                    if not received.is_set():
                        operation.result()
                        pytest.fail(
                            "command exited without delivering its first output"
                        )
                    assert chunks == ["A"] and not (tmp_path / "finished").exists()
                    os.write(descriptor, b"x")
                    assert not operation.done()
                    release_consumer.set()
                    result = await operation
                    assert result.output == "".join(chunks)
                    assert result.output in {"A😀BE�", "AE�😀B"}
                    assert result.exit_code == 7 and result.truncated is False
                finally:
                    release_consumer.set()
                    os.write(descriptor, b"x")
                    operation.cancel()
                    signal.cancel()
                    await asyncio.gather(operation, signal, return_exceptions=True)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("ending", ["cancel", "timeout", "consumer"])
@pytest.mark.parametrize("network", ["normal", "restricted"])
async def test_stream_failure_joins_readers_consumer_and_owned_process(
    tmp_path: Path,
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
    network: Literal["normal", "restricted"],
) -> None:
    (tmp_path / "wait.py").write_text(
        "import os, signal\n"
        "assert os.environ['WORKSPACE_TEST_SECRET'] == 'explicit-workload-value'\n"
        "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})\n"
        "print('ready', flush=True)\n"
        "signal.sigwait({signal.SIGUSR1})\n"
    )
    entered, release = asyncio.Event(), asyncio.Event()
    consumer_closed = asyncio.Event()
    created: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*arguments: str, **options: Any) -> asyncio.subprocess.Process:
        process = await spawn(*arguments, **options)
        created.append(process)
        return process

    class Deadline:
        def __init__(self) -> None:
            self.owner: asyncio.Task[object] | None = None
            self.expired = False

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

    def selected_deadline(seconds: float | None) -> Deadline | asyncio.Timeout:
        return deadline if seconds == 30 else timeout(seconds)

    async def emit(text: str) -> None:
        assert text == "ready\n"
        entered.set()
        try:
            await release.wait()
            if ending == "consumer":
                raise RuntimeError("private delivery diagnostic")
        finally:
            consumer_closed.set()

    async with LocalWorkspace(
        tmp_path,
        access=LocalAccess(network=network),
        environment={"WORKSPACE_TEST_SECRET": "explicit-workload-value"},
        sandbox_executable=sandbox,
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
            if ending == "timeout":
                monkeypatch.setattr(asyncio, "timeout", selected_deadline)
            operation = asyncio.create_task(
                prepared.workspace.aexecute_stream(
                    "python wait.py", emit_output=emit, timeout=30
                )
            )
            signal = asyncio.create_task(entered.wait())
            try:
                await asyncio.wait(
                    (operation, signal), return_when=asyncio.FIRST_COMPLETED
                )
                if not entered.is_set():
                    operation.result()
                    pytest.fail("command ended before the controlled consumer")
                if ending == "cancel":
                    operation.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await operation
                else:
                    if ending == "timeout":
                        deadline.expire()
                    else:
                        release.set()
                    with pytest.raises(LocalError) as failure:
                        await operation
                    assert failure.value.code == (
                        LocalErrorCode.TIMEOUT
                        if ending == "timeout"
                        else LocalErrorCode.PROTOCOL
                    )
                    assert "private delivery diagnostic" not in str(failure.value)
                    if ending == "consumer":
                        assert isinstance(failure.value.cause, RuntimeError)
                assert consumer_closed.is_set()
                assert created and all(
                    process.returncode is not None for process in created
                )
                assert (
                    await prepared.workspace.aexecute("printf healthy")
                ).output == "healthy"
            finally:
                release.set()
                operation.cancel()
                signal.cancel()
                await asyncio.gather(operation, signal, return_exceptions=True)


async def test_stream_enforces_combined_byte_budget_and_rejects_invalid_deadlines(
    tmp_path: Path, sandbox: Path
) -> None:
    chunks: list[str] = []

    async def emit(text: str) -> None:
        chunks.append(text)

    async with LocalWorkspace(
        tmp_path, sandbox_executable=sandbox, limits=LocalLimits(output_bytes=1024)
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            for timeout in (-1, 0, 3601):
                with pytest.raises(ValueError):
                    await prepared.workspace.aexecute_stream(
                        "printf unexpected", emit_output=emit, timeout=timeout
                    )
            assert chunks == []
            with pytest.raises(LocalError) as failure:
                await prepared.workspace.aexecute_stream(
                    'python -c \'import os; os.write(1, b"x" * 800); os.write(2, b"y" * 800)\'',
                    emit_output=emit,
                )
            assert failure.value.code == LocalErrorCode.LIMIT
            assert sum(len(chunk.encode()) for chunk in chunks) <= 1024
            assert (
                await prepared.workspace.aexecute("printf healthy")
            ).output == "healthy"
