"""更新检查、安装准备和授权交接的真实原生边界"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.companion_integration
@pytest.mark.parametrize("stage", ["check", "stop"])
async def test_update_retains_committed_identity_and_revokes_pending_approval(
    swift_contracts, tmp_path, stage
):
    authorized = asyncio.Event()
    children: set[asyncio.Task] = set()
    revoked: list[str] = []

    async def respond(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.partition(b":")[2])
                    for line in header.lower().split(b"\r\n")
                    if line.startswith(b"content-length:")
                ),
                0,
            )
            await reader.readexactly(length)
            path = header.split(b" ", 2)[1]
            if path.endswith((b"/status", b"/cancel")):
                if path.endswith(b"/status"):
                    await authorized.wait()
                data = {
                    "authorization_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                    "state": "authorized",
                    "device": {
                        "device_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                        "namespace": "ns_1",
                        "name": "Contract Mac",
                    },
                }
            elif path.endswith(b"/projects"):
                data = {"account_name": "contract", "items": [], "next_cursor": None}
            elif path.endswith(b"/current"):
                revoked.append(header.decode())
                data = None
            else:
                raise AssertionError(path)
            content = json.dumps({"code": 0, "data": data}).encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode()
                + content
            )
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    def connected(reader, writer):
        children.add(asyncio.create_task(respond(reader, writer)))

    async def release_host(reader, writer):
        try:
            assert await reader.readline() == b"stopping\n"
            await release.wait()
            writer.write(b"exit\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def host_connected(reader, writer):
        children.add(asyncio.create_task(release_host(reader, writer)))

    release = asyncio.Event()
    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    host_server = await asyncio.start_server(host_connected, "127.0.0.1", 0)
    (tmp_path / "fixture.json").write_text(
        json.dumps(
            {
                "server": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                "oldAccount": str(uuid4()),
                "newAccount": str(uuid4()),
            }
        )
    )
    resources = tmp_path / "resources"
    executable = resources / "python/bin/python3"
    executable.parent.mkdir(parents=True)
    source = Path(__file__).parent / "native/authorization_update_host.py"
    executable.write_text(f"#!{Path(sys.executable).resolve()}\n" + source.read_text())
    executable.chmod(0o700)
    (resources / "control.json").write_text(
        json.dumps({"port": host_server.sockets[0].getsockname()[1]})
    )
    mode = f"authorization-update-{stage}"
    process = await asyncio.create_subprocess_exec(
        str(swift_contracts),
        mode,
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None and process.stdin is not None
        assert await process.stdout.readline() == b"checkpoint old-online\n"
        authorized.set()
        process.stdin.write(b"old-online\n")
        await process.stdin.drain()
        assert await process.stdout.readline() == b"checkpoint maintenance-entered\n"
        release.set()
        process.stdin.write(b"maintenance-entered\n")
        await process.stdin.drain()
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.decode().strip() == f"verified {mode}"
        assert len(revoked) == 1
        assert "Bearer bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb." in revoked[0]
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        release.set()
        server.close()
        host_server.close()
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        await server.wait_closed()
        await host_server.wait_closed()
        cleanup = await asyncio.create_subprocess_exec(
            str(swift_contracts), "authorization-update-cleanup", str(tmp_path)
        )
        assert await cleanup.wait() == 0
