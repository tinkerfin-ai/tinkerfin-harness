"""通过真实回环 HTTP 验证聊天模型与生图的地址授权和认证边界"""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from tinkerfin_studio.attachments.generation import generate_image_bytes
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.schemas import (
    AgentModelConfig,
)
from tinkerfin_studio.models.transport import ModelTransport


@pytest_asyncio.fixture
async def model_server() -> AsyncIterator[
    tuple[str, list[tuple[str, dict[str, str], bytes]]]
]:
    """提供测试独占服务，完成后等待连接处理结束并关闭监听端口"""
    requests: list[tuple[str, dict[str, str], bytes]] = []
    origin = ""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode().split("\r\n")
            headers = dict(line.lower().split(": ", 1) for line in lines[1:] if line)
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            requests.append((lines[0], headers, body))
            if "/chat/completions" in lines[0]:
                chunk = {
                    "id": "chat-local",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "local-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "本地回复"},
                            "finish_reason": None,
                        }
                    ],
                }
                result = (
                    "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
                ).encode()
                content_type = "text/event-stream"
            elif "/images/generations" in lines[0]:
                result = json.dumps({"data": [{"url": origin + "/image.png"}]}).encode()
                content_type = "application/json"
            else:
                result = b"local-image-bytes"
                content_type = "image/png"
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(result)}\r\nConnection: close\r\n\r\n".encode()
                + result
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with asyncio.TaskGroup() as tasks:
        server = await asyncio.start_server(
            lambda r, w: tasks.create_task(handle(r, w)), "127.0.0.1", 0
        )
        origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with server:
            yield origin, requests


def model_config(origin: str) -> AgentModelConfig:
    return AgentModelConfig(
        model_id="local",
        display_name="Local",
        provider="openai",
        model_name="local-model",
        base_url=origin + "/v1",
        api_key=SecretStr("ollama"),
        reasoning_enabled=False,
    )


async def test_chat_model_uses_allowed_local_http_client(model_server):
    origin, requests = model_server
    async with httpx.AsyncClient(
        transport=ModelTransport(allowed_origins=(origin,)),
        trust_env=False,
        follow_redirects=False,
        timeout=5,
    ) as client:
        model = create_chat_model(model_config(origin), http_async_client=client)
        result = await model.ainvoke("你好")
    assert result.content == "本地回复"
    assert len(requests) == 1
    method, headers, body = requests[0]
    assert method == "POST /v1/chat/completions HTTP/1.1"
    assert headers["authorization"] == "bearer ollama"
    assert json.loads(body)["messages"] == [{"role": "user", "content": "你好"}]


async def test_generation_and_download_use_allowed_local_http_without_key_leak(
    model_server,
):
    origin, requests = model_server
    result = await generate_image_bytes(
        model_config(origin), "a chart", allowed_origins=(origin,)
    )
    assert result == b"local-image-bytes"
    assert len(requests) == 2
    assert requests[0][0] == "POST /v1/images/generations HTTP/1.1"
    assert requests[0][1]["authorization"] == "bearer ollama"
    assert requests[1][0] == "GET /image.png HTTP/1.1"
    assert "authorization" not in requests[1][1]


async def test_unlisted_local_generation_never_reaches_server(model_server):
    origin, requests = model_server
    with pytest.raises(ValueError, match="MODEL_ALLOWED_ORIGINS"):
        await generate_image_bytes(model_config(origin), "a chart")
    assert not requests


async def test_allowed_localhost_connects_to_ipv4_only_service(model_server):
    origin, requests = model_server
    origin = origin.replace("127.0.0.1", "localhost")
    async with httpx.AsyncClient(
        transport=ModelTransport(allowed_origins=(origin,)), trust_env=False, timeout=2
    ) as client:
        assert (await client.get(origin + "/image.png")).status_code == 200
    assert len(requests) == 1
