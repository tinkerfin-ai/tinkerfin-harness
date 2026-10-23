"""启动取消仅结算其拥有的操作，不打断已接受的配置提交"""

import asyncio
import json

import pytest


@pytest.mark.companion_integration
@pytest.mark.parametrize("owner", ["startup", "accepted"])
async def test_model_cancels_only_caller_owned_network_work(
    swift_contracts, tmp_path, owner
):
    received = asyncio.Event()
    respond = asyncio.Event()
    disconnected = asyncio.Event()
    children: set[asyncio.Task] = set()

    async def serve(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            assert header.startswith(b"GET /api/devices/projects ")
            received.set()
            if owner == "startup":
                assert await reader.read() == b""
                disconnected.set()
            else:
                await respond.wait()
                content = json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "account_name": "contract",
                            "items": [],
                            "next_cursor": None,
                        },
                    }
                ).encode()
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode()
                    + content
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def connected(reader, writer):
        children.add(asyncio.create_task(serve(reader, writer)))

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    (tmp_path / "server.txt").write_text(f"http://127.0.0.1:{port}")
    mode = f"model-{owner}-cancel"
    process = await asyncio.create_subprocess_exec(
        str(swift_contracts),
        mode,
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        assert await process.stdout.readline() == b"checkpoint request-started\n"
        await received.wait()
        process.stdin.write(b"request-started\n")
        await process.stdin.drain()
        assert await process.stdout.readline() == b"checkpoint caller-cancelled\n"
        if owner == "startup":
            await disconnected.wait()
        else:
            respond.set()
        process.stdin.write(b"caller-cancelled\n")
        await process.stdin.drain()
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.decode().strip() == f"verified {mode}"
        await asyncio.gather(*children)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        server.close()
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        await server.wait_closed()


@pytest.mark.companion_integration
@pytest.mark.parametrize("step", ["restore", "connect"])
async def test_cancelled_startup_preserves_the_preceding_accepted_operation(
    swift_contracts, tmp_path, step
):
    mode = f"startup-queued-{step}"
    process = await asyncio.create_subprocess_exec(
        str(swift_contracts),
        mode,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.decode().strip() == f"verified {mode}"
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
