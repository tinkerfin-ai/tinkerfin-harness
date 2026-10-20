"""桌面发现只读取用户选择的服务，不携带凭证或追随重定向"""

import asyncio
import json

import pytest
from test_native_client import run_contract


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, b'{"api_url":"https://api.example.com/root/"}', None),
        (404, b"missing", "无法识别 API 地址，请展开填写"),
        (200, b"<html>Studio</html>", "无法识别 API 地址，请展开填写"),
        (302, b"", "无法识别 API 地址，请展开填写"),
        (200, b"x" * 4097, "连接配置超过容量"),
        (
            200,
            b'{"api_url":"http://remote.example.com"}',
            "连接配置必须使用 HTTPS；HTTP 仅用于本机开发服务",
        ),
    ],
)
async def test_service_discovery_response_and_redirect_boundary(
    swift_contracts, status, body, expected
):
    requests = []
    handlers = set()

    async def handle(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            headers = (
                f"HTTP/1.1 {status} Response\r\nContent-Length: {len(body)}\r\n"
                "Content-Type: application/json\r\n"
                "Location: /unexpected\r\nConnection: close\r\n\r\n"
            ).encode()
            writer.write(headers + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def connected(reader, writer):
        task = asyncio.create_task(handle(reader, writer))
        handlers.add(task)

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    try:
        address = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/studio"
        output = await run_contract(swift_contracts, "service-discovery", address)
        if expected is None:
            assert json.loads(output) == {
                "studio_url": address,
                "api_url": "https://api.example.com/root",
            }
        else:
            assert output == expected
        assert len(requests) == 1
        assert requests[0].startswith(b"GET /studio/tinkerfin-connection.json ")
        assert b"authorization:" not in requests[0].lower()
        assert b"cookie:" not in requests[0].lower()
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tuple(handlers), return_exceptions=True)


async def test_explicit_api_address_requires_no_discovery(swift_contracts):
    output = await run_contract(
        swift_contracts, "service-manual", "https://studio.invalid/root/"
    )
    assert json.loads(output) == {
        "studio_url": "https://studio.invalid/root",
        "api_url": "https://api.example.com/root",
    }


async def test_discovery_cancellation_closes_the_request(swift_contracts):
    requested = asyncio.Event()
    disconnected = asyncio.Event()
    handlers = set()

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            requested.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    def connected(reader, writer):
        handlers.add(asyncio.create_task(handle(reader, writer)))

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    process = None
    try:
        address = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        process = await asyncio.create_subprocess_exec(
            str(swift_contracts),
            "service-cancel",
            address,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stdin is not None
        assert await process.stdout.readline() == b"checkpoint discovery-request\n"
        await requested.wait()
        process.stdin.write(b"discovery-request\n")
        await process.stdin.drain()
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.strip() == b"verified service-cancel"
        await disconnected.wait()
    finally:
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()
        server.close()
        await server.wait_closed()
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
