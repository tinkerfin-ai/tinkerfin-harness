"""Normal IPC uses native services; restricted access retains explicit grants."""

import asyncio
import shlex
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tinkerfin_sandbox import LocalAccess, LocalWorkspace


@pytest.mark.parametrize(
    ("name", "graphics", "allowed"),
    [
        ("com.google.chrome.for.testing.MachPortRendezvousServer.{pid}", True, True),
        ("com.google.Chrome.MachPortRendezvousServer.{pid}", True, True),
        ("org.chromium.Chromium.MachPortRendezvousServer.{pid}", True, True),
        ("com.google.chrome.for.testing.MachPortRendezvousServer.{pid}", False, False),
        ("com.google.Chrome.MachPortRendezvousServer.{pid}", False, False),
        ("org.chromium.Chromium.MachPortRendezvousServer.{pid}", False, False),
        ("prefix.com.google.Chrome.MachPortRendezvousServer.{pid}", True, False),
        ("org.chromium.Chromium.MachPortRendezvousServer.{pid}.extra", True, False),
        ("com.google.Chrome.MachPortRendezvousServer.name{pid}", True, False),
        ("comXgoogle.Chrome.MachPortRendezvousServer.{pid}", True, False),
        ("com.google.chrome.MachPortRendezvousServer.{pid}", True, False),
        ("com.tinkerfin.unrelated.{pid}", True, False),
    ],
)
async def test_graphics_grants_only_exact_browser_rendezvous_names(
    tmp_path: Path, sandbox: Path, name: str, graphics: bool, allowed: bool
) -> None:
    source = (
        "import ctypes, os; lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib'); "
        "bootstrap=ctypes.c_uint.in_dll(lib,'bootstrap_port').value; "
        "port=ctypes.c_uint(); "
        f"name={name!r}.format(pid=os.getpid()).encode(); "
        "result=lib.bootstrap_check_in(bootstrap,name,ctypes.byref(port)); "
        "lookup=lib.bootstrap_look_up(bootstrap,name,ctypes.byref(port)); "
        "print(result,lookup)"
    )
    async with LocalWorkspace(
        tmp_path,
        sandbox_executable=sandbox,
        access=LocalAccess(network="restricted", graphics=graphics),
    ) as workspace:
        async with workspace.open() as session:
            result = await session.aexecute("python -I -c " + shlex.quote(source))
            assert result.exit_code == 0
            assert result.output == ("0 0\n" if allowed else "1100 1100\n")


@pytest.fixture
def host_socket_path() -> Iterator[Path]:
    # Darwin sockaddr_un has a fixed path bound independent of filesystem grants.
    with tempfile.TemporaryDirectory(prefix="native-ipc-", dir="/tmp") as directory:
        yield Path(directory).resolve() / "host.sock"


@pytest.mark.parametrize("graphics", [False, True])
@pytest.mark.parametrize("network", ["normal", "restricted"])
async def test_private_unix_socket_follows_the_selected_access(
    tmp_path: Path, sandbox: Path, graphics: bool, network
) -> None:
    source = (
        "import os, socket; "
        "connection=socket.socket(socket.AF_UNIX); "
        "connection.bind(os.path.join(os.environ['TMPDIR'],'owned.sock')); "
        "connection.close(); print('bound')"
    )
    async with LocalWorkspace(
        tmp_path,
        sandbox_executable=sandbox,
        access=LocalAccess(network=network, graphics=graphics),
    ) as workspace:
        async with workspace.open() as session:
            result = await session.aexecute("python -I -c " + shlex.quote(source))
            allowed = network == "normal" or graphics
            assert (result.exit_code == 0) is allowed
            assert (result.output == "bound\n") is allowed


@pytest.mark.parametrize("network", ["normal", "restricted"])
async def test_host_unix_sockets_follow_the_selected_access(
    tmp_path: Path, sandbox: Path, host_socket_path: Path, network
) -> None:
    path = host_socket_path

    def reject(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_unix_server(reject, path)
    try:
        source = (
            "import socket; connection=socket.socket(socket.AF_UNIX); "
            f"connection.connect({str(path)!r})"
        )
        async with LocalWorkspace(
            tmp_path,
            sandbox_executable=sandbox,
            access=LocalAccess(filesystem="user", network=network, graphics=True),
        ) as workspace:
            async with workspace.open() as session:
                result = await session.aexecute("python -I -c " + shlex.quote(source))
                if network == "normal":
                    assert result.exit_code == 0, result
                else:
                    assert result.exit_code != 0
                    assert "Operation not permitted" in result.output
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)
