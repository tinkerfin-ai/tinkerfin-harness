"""Native MCP protocol, secret isolation, cancellation and byte limits."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_sandbox import LocalError, LocalErrorCode, LocalLimits, LocalWorkspace

IDENTITY = RunIdentity(namespace="local", thread_id="task", run_id="run")


@pytest.fixture
def command(tmp_path: Path) -> tuple[str, ...]:
    pytest.importorskip("mcp")
    script = tmp_path / "server.py"
    script.write_bytes(Path(__file__).with_name("mcp_server.py").read_bytes())
    return "python", str(script)


async def test_real_mcp_tools_preserve_arguments_and_do_not_inherit_host_credentials(
    tmp_path: Path,
    sandbox: Path,
    command: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TINKERFIN_TEST_SECRET", "must-stay-in-host")
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            tools = await prepared.workspace.mcp_tools(command)
            assert any(
                tool["name"] == "echo" and "inputSchema" in tool for tool in tools
            )
            response = await prepared.workspace.call_mcp(
                command, "echo", {"value": "hello"}
            )
    content = response["content"]
    assert isinstance(content, list) and isinstance(content[0], dict)
    text = content[0]["text"]
    assert isinstance(text, str)
    assert json.loads(text) == {"value": "hello", "host_secret": None}
    assert (tmp_path / "effects").read_text() == "echo"


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
async def test_mcp_interruption_reaps_the_owned_process_using_explicit_signals(
    tmp_path: Path,
    sandbox: Path,
    command: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    fifo = tmp_path / "started"
    os.mkfifo(fifo)
    descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def notified() -> None:
        os.read(descriptor, 4096)
        entered.set()

    loop.add_reader(descriptor, notified)
    created: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        created.append(process)
        return process

    class Deadline:
        def __init__(self) -> None:
            self.owner: asyncio.Task[object] | None = None
            self.expired = False

        async def __aenter__(self) -> None:
            self.owner = asyncio.current_task()

        async def __aexit__(self, kind, error, traceback) -> None:
            if self.expired and isinstance(error, asyncio.CancelledError):
                raise TimeoutError from error

        def expire(self) -> None:
            assert self.owner is not None
            self.expired = True
            self.owner.cancel()

    deadline = Deadline()
    timeout = asyncio.timeout
    try:
        async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
            async with workspace.prepare(IDENTITY) as prepared:
                monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
                if ending == "timeout":
                    monkeypatch.setattr(
                        asyncio,
                        "timeout",
                        lambda seconds: deadline if seconds == 30 else timeout(seconds),
                    )
                operation = asyncio.create_task(
                    prepared.workspace.call_mcp(command, "blocked", {}, timeout=30)
                )
                signal = asyncio.create_task(entered.wait())
                try:
                    await asyncio.wait(
                        (operation, signal), return_when=asyncio.FIRST_COMPLETED
                    )
                    if not entered.is_set():
                        operation.result()
                        pytest.fail("server ended without reaching the controlled tool")
                    if ending == "timeout":
                        deadline.expire()
                        with pytest.raises(LocalError) as error:
                            await operation
                        assert error.value.code == LocalErrorCode.TIMEOUT
                    else:
                        operation.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await operation
                finally:
                    operation.cancel()
                    signal.cancel()
                    await asyncio.gather(operation, signal, return_exceptions=True)
                assert created and all(
                    process.returncode is not None for process in created
                )
    finally:
        loop.remove_reader(descriptor)
        os.close(descriptor)


async def test_mcp_protocol_and_output_failures_close_the_server(
    tmp_path: Path, sandbox: Path, command: tuple[str, ...]
) -> None:
    async with LocalWorkspace(
        tmp_path, sandbox_executable=sandbox, limits=LocalLimits(output_bytes=8192)
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            with pytest.raises(LocalError) as output:
                await prepared.workspace.call_mcp(command, "large_output", {})
            assert output.value.code == LocalErrorCode.LIMIT
            with pytest.raises(LocalError) as invalid:
                await prepared.workspace.mcp_tools(
                    ("/usr/bin/printf", "invalid-json\n")
                )
            assert invalid.value.code == LocalErrorCode.PROTOCOL
            assert (
                await prepared.workspace.aexecute("printf available")
            ).output == "available"
