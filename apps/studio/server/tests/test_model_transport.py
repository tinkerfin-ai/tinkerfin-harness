"""模型服务的精确授权、HTTP 支持和连接目标校验"""

import socket

import httpx
import pytest
from pydantic import ValidationError

from tinkerfin_studio.config.settings import Settings, load_settings
from tinkerfin_studio.models.transport import ModelTransport


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("http://localhost:11434/v1/models", "127.0.0.1"),
        ("http://127.0.0.1:11434/v1/models", "127.0.0.1"),
        ("http://[::1]:11434/v1/models", "::1"),
        ("http://ollama:11434/v1/models", "10.0.0.2"),
        ("https://models.internal/v1/models", "192.168.1.2"),
        ("http://models.internal/v1/models", "192.168.1.2"),
    ],
)
async def test_allowed_service_uses_resolved_address_and_original_authority(
    monkeypatch, url, address
):
    parsed = httpx.URL(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    requests: list[httpx.Request] = []

    async def resolve(host, resolved_port, **kwargs):
        assert host == parsed.host and resolved_port == port
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, port))]

    async def handle(request):
        requests.append(request)
        return httpx.Response(200)

    monkeypatch.setattr("tinkerfin_studio.models.transport.anyio.getaddrinfo", resolve)
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(handle)
    )
    origin = str(parsed.copy_with(path="/"))
    async with httpx.AsyncClient(
        transport=ModelTransport(allowed_origins=(origin,))
    ) as client:
        assert (await client.get(url)).status_code == 200
    assert requests[0].url.host == address
    assert requests[0].url.scheme == parsed.scheme
    assert requests[0].url.port == parsed.port
    assert requests[0].headers["host"] == parsed.netloc.decode()
    assert requests[0].extensions["sni_hostname"] == parsed.host


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11435/v1/models",
        "http://127.0.0.1:11434/v1/models",
        "http://localhost.evil:11434/v1/models",
        "https://localhost:11434/v1/models",
        "http://user:password@localhost:11434/v1/models",
    ],
)
async def test_allowed_origin_does_not_authorize_other_destinations(monkeypatch, url):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 11434))]

    def must_not_connect(**kwargs):
        pytest.fail("未授权来源不能建立连接")

    monkeypatch.setattr("tinkerfin_studio.models.transport.anyio.getaddrinfo", resolve)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", must_not_connect)
    async with httpx.AsyncClient(
        transport=ModelTransport(allowed_origins=("http://localhost:11434",))
    ) as client:
        with pytest.raises(ValueError):
            await client.get(url)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:11434/v1",
        "http://localhost:11434?x=1",
        "http://localhost:11434#fragment",
        "http://user:pass@localhost:11434",
        "http://*.internal:11434",
        "file:///tmp/model",
        "localhost:11434",
    ],
)
def test_invalid_origin_is_rejected_at_configuration_boundary(origin):
    with pytest.raises((ValidationError, httpx.InvalidURL)):
        Settings.model_validate(
            {
                "s3_storage_access_key": "test-access",
                "s3_storage_secret_key": "test-secret",
                "s3_storage_bucket": "test-attachments",
                "business_database_url": "mysql+asyncmy://test:test@localhost/test",
                "components_database_url": "mysql+asyncmy://u:p@db/components",
                "model_allowed_origins": (origin,),
            }
        )


def test_settings_load_json_origins_and_normalize_default_ports(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_ALLOWED_ORIGINS", raising=False)
    path = tmp_path / "model.env"
    path.write_text(
        'COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\nBUSINESS_DATABASE_URL=mysql+asyncmy://test:test@localhost/test\nMODEL_ALLOWED_ORIGINS=["http://LOCALHOST:80", "http://localhost/", "http://ollama:11434"]\n'
    )
    assert load_settings(env_file=path).model_allowed_origins == (
        "http://localhost/",
        "http://ollama:11434/",
    )


@pytest.mark.parametrize(
    "failure", [httpx.ReadError, httpx.WriteError, httpx.ReadTimeout]
)
async def test_request_errors_do_not_resend_to_another_resolved_address(
    monkeypatch, failure
):
    attempts = []

    async def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 11434))
            for ip in ("127.0.0.1", "127.0.0.2")
        ]

    async def handle(request):
        attempts.append(request.url.host)
        raise failure("request failed", request=request)

    monkeypatch.setattr("tinkerfin_studio.models.transport.anyio.getaddrinfo", resolve)
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(
        transport=ModelTransport(allowed_origins=("http://localhost:11434",))
    ) as client:
        with pytest.raises(failure):
            await client.post(
                "http://localhost:11434/images/generations", json={"prompt": "chart"}
            )
    assert attempts == ["127.0.0.1"]


@pytest.mark.parametrize("compressed", [False, True])
async def test_response_budget_rejects_oversized_or_compressed_input_before_sdk(
    monkeypatch, compressed
):
    from tinkerfin_studio.models.transport import ModelResponseTooLarge

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"a" * 8
            yield b"b" * 8

        async def aclose(self):
            self.closed = True

    body = Body()

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]

    async def handle(request):
        return httpx.Response(
            200, stream=body, headers={"content-encoding": "gzip"} if compressed else {}
        )

    monkeypatch.setattr("tinkerfin_studio.models.transport.anyio.getaddrinfo", resolve)
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(
        transport=ModelTransport(response_limit_bytes=10)
    ) as client:
        with pytest.raises(ValueError if compressed else ModelResponseTooLarge):
            await client.get("https://models.example/v1/models")
    assert body.closed
