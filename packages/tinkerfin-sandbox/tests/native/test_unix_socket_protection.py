"""Local Unix grants preserve private paths and project isolation through aliases."""

import json
import os
import socket
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

import pytest

from tinkerfin_sandbox import LocalAccess, LocalWorkspace

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS")

PROBE = """
import json, socket, sys
print('ready', flush=True)
endpoints = json.loads(sys.stdin.readline())
result = {}
for name, endpoint in endpoints.items():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            client.connect(endpoint)
        except PermissionError:
            result[name] = False
        else:
            result[name] = True
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
    try:
        client.connect(('127.0.0.1', int(sys.argv[1])))
    except PermissionError:
        result['loopback'] = False
    else:
        result['loopback'] = True
print(json.dumps(result), flush=True)
"""


@pytest.mark.parametrize("filesystem", ["workspace", "user"])
@pytest.mark.parametrize("network", ["normal", "restricted"])
async def test_unix_connections_follow_protected_and_isolated_real_paths(
    tmp_path: Path,
    sandbox: Path,
    filesystem: Literal["workspace", "user"],
    network: Literal["normal", "restricted"],
) -> None:
    managed = tmp_path / "managed"
    active = managed / "current"
    private = active / ("private-" + "p" * 64)
    sibling = managed / ("sibling-" + "s" * 64)
    outside = tmp_path / "ordinary-service"
    immutable = tmp_path / "read-only-service"
    with (
        tempfile.TemporaryDirectory(prefix="tf-ipc-", dir="/tmp") as name,
        ExitStack() as resources,
    ):
        aliases = Path(name).resolve()

        def endpoint(directory: Path, label: str) -> str:
            directory.mkdir(parents=True)
            alias = aliases / label
            alias.symlink_to(directory, target_is_directory=True)
            listener = resources.enter_context(
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            )
            address = str(alias / "service.sock")
            listener.bind(address)
            listener.listen(16)
            return address

        own = endpoint(active, "own")
        endpoints = {
            "own": own,
            "private": endpoint(private, "private"),
            "sibling": endpoint(sibling, "sibling"),
            "ordinary": endpoint(outside, "ordinary"),
            "read_only": endpoint(immutable, "read-only"),
        }
        assert len(str(private / "service.sock").encode()) > 104
        directory_alias = aliases / "different-private-alias"
        directory_alias.symlink_to(private, target_is_directory=True)
        socket_alias = aliases / "socket-alias"
        socket_alias.symlink_to(private / "service.sock")
        hardlink = aliases / "socket-hardlink"
        os.link(private / "service.sock", hardlink)
        endpoints.update(
            private_directory_alias=str(directory_alias / "service.sock"),
            private_socket_alias=str(socket_alias),
            private_hardlink=str(hardlink),
        )
        tcp = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(4)
        async with LocalWorkspace(
            active,
            sandbox_executable=sandbox,
            access=LocalAccess(
                filesystem=filesystem,
                network=network,
                isolated_roots=(managed,),
                protected_paths=(private,),
                read_directories=(aliases, outside, immutable),
                read_only_paths=(immutable,),
                unix_socket_paths=(
                    active,
                    private,
                    sibling,
                    immutable,
                    managed / "future-sibling",
                ),
            ),
        ) as workspace:
            async with workspace.open() as session:
                async with session.open_process(
                    ("python", "-I", "-c", PROBE, str(tcp.getsockname()[1]))
                ) as process:
                    assert await process.stdout.readline() == b"ready\n"
                    endpoints["future_sibling"] = endpoint(
                        managed / "future-sibling", "future"
                    )
                    output, error = await process.communicate(
                        (json.dumps(endpoints) + "\n").encode()
                    )
                    assert process.returncode == 0, error.decode()
                    assert json.loads(output) == {
                        "own": True,
                        "private": False,
                        "sibling": False,
                        "ordinary": network == "normal",
                        "read_only": True,
                        "private_directory_alias": False,
                        "private_socket_alias": False,
                        "private_hardlink": False,
                        "future_sibling": False,
                        "loopback": network == "normal",
                    }


@pytest.mark.parametrize("grant", ["file", "directory"])
async def test_restricted_unix_grant_allows_binding_and_connecting_only_authorized_paths(
    tmp_path: Path, sandbox: Path, grant: Literal["file", "directory"]
) -> None:
    control = tmp_path / ("control-" + "c" * 64)
    other = tmp_path / "other-control"
    control.mkdir()
    other.mkdir()
    with tempfile.TemporaryDirectory(prefix="tf-bind-", dir="/tmp") as name:
        aliases = Path(name).resolve()
        authorized = aliases / "authorized"
        unauthorized = aliases / "unauthorized"
        authorized.symlink_to(control, target_is_directory=True)
        unauthorized.symlink_to(other, target_is_directory=True)
        first, second = authorized / "first.sock", authorized / "second.sock"
        outside = unauthorized / "outside.sock"
        paths = {"first": str(first), "second": str(second), "outside": str(outside)}
        source = """
import json, socket, sys
results = {}
for name, path in json.loads(sys.argv[1]).items():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        try:
            server.bind(path)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)
        except PermissionError:
            results[name] = False
        else:
            results[name] = True
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    try:
        server.bind(('127.0.0.1', 0))
    except PermissionError:
        results['loopback'] = False
    else:
        results['loopback'] = True
print(json.dumps(results))
"""
        async with LocalWorkspace(
            tmp_path,
            sandbox_executable=sandbox,
            access=LocalAccess(
                filesystem="user",
                network="restricted",
                unix_socket_paths=(first if grant == "file" else authorized,),
                read_only_paths=(aliases,),
            ),
        ) as workspace:
            async with workspace.open() as session:
                async with session.open_process(
                    ("python", "-I", "-c", source, json.dumps(paths))
                ) as process:
                    output, error = await process.communicate()
                    assert process.returncode == 0, error.decode()
                    assert json.loads(output) == {
                        "first": True,
                        "second": grant == "directory",
                        "outside": False,
                        "loopback": False,
                    }


def test_unix_grants_cannot_authorize_all_paths() -> None:
    with pytest.raises(ValueError, match="filesystem root"):
        LocalAccess(network="restricted", unix_socket_paths=(Path("/"),))


async def test_unix_grants_reject_a_root_alias(tmp_path: Path, sandbox: Path) -> None:
    alias = tmp_path / "root-alias"
    alias.symlink_to("/", target_is_directory=True)
    async with LocalWorkspace(
        tmp_path,
        sandbox_executable=sandbox,
        access=LocalAccess(network="restricted", unix_socket_paths=(alias,)),
    ) as workspace:
        with pytest.raises(ValueError, match="filesystem root"):
            async with workspace.open():
                pytest.fail("a root alias cannot authorize all Unix services")
