"""模型服务地址、认证与响应限量的 HTTP 边界"""

import httpx
import pytest

from tinkerfin_studio.models.transport import (
    ModelResponseTooLarge,
    ModelTransport,
    validate_model_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1/models",
        "http://127.0.0.1:11434/v1/models",
        "http://[::1]:11434/v1/models",
        "http://ollama:11434/v1/models",
        "https://models.internal/v1/models",
        "http://192.168.1.2/v1/models",
        "https://models.example/v1/models",
    ],
)
async def test_http_transport_preserves_public_local_and_private_authority(
    monkeypatch, url
):
    requests: list[httpx.Request] = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(transport=ModelTransport()) as client:
        await client.get(url, headers={"Authorization": ""})
    assert str(requests[0].url) == url
    assert "Authorization" not in requests[0].headers


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/model",
        "ftp://example.com/model",
        "http://user:password@localhost/model",
        "localhost:11434",
    ],
)
def test_non_http_or_embedded_credentials_are_rejected(url):
    with pytest.raises(ValueError):
        validate_model_url(url)


@pytest.mark.parametrize(
    "failure", [httpx.ReadError, httpx.WriteError, httpx.ReadTimeout]
)
async def test_request_failure_does_not_resubmit(monkeypatch, failure):
    attempts = []

    async def handle(request):
        attempts.append(request)
        raise failure("request failed", request=request)

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(transport=ModelTransport()) as client:
        with pytest.raises(failure):
            await client.post(
                "http://localhost:11434/images/generations", json={"prompt": "chart"}
            )
    assert len(attempts) == 1


@pytest.mark.parametrize("compressed", [False, True])
async def test_response_budget_rejects_oversized_or_compressed_input_before_sdk(
    monkeypatch, compressed
):
    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"a" * 8
            yield b"b" * 8

        async def aclose(self):
            self.closed = True

    body = Body()

    async def handle(request):
        return httpx.Response(
            200, stream=body, headers={"content-encoding": "gzip"} if compressed else {}
        )

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(
        transport=ModelTransport(response_limit_bytes=10)
    ) as client:
        with pytest.raises(ValueError if compressed else ModelResponseTooLarge):
            await client.get("https://models.example/v1/models")
    assert body.closed
