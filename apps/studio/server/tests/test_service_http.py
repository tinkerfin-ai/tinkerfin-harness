"""搜索与生图的同步 HTTP 请求和结果契约"""

import asyncio
import base64
import json

import httpx
import pytest

from tinkerfin_studio.models.transport import ModelResponseTooLarge
from tinkerfin_studio.services import http as service_http
from tinkerfin_studio.services.http import SearchRequest, ServiceHTTPError
from tinkerfin_studio.services.schemas import (
    HttpRequestConfig,
    ImageConfig,
    SearchConfig,
)
from tinkerfin_studio.services.service import ResolvedService


def client_for(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        service_http,
        "_client",
        lambda limit: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_tavily_search_preserves_parameters_and_safe_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "answer": "找到资料",
                "results": [
                    {
                        "title": "示例",
                        "url": "https://example.com/a",
                        "content": "摘要",
                        "score": 0.7,
                    }
                ],
            },
        )

    client_for(monkeypatch, handler)
    config = SearchConfig(
        depth="advanced", max_results=3, extra={"include_raw_content": False}
    )
    service = ResolvedService("service-1", config, "fingerprint", "secret")
    result = await service_http.search_web(
        service, SearchRequest("科研进展", max_results=8)
    )
    assert result.results[0].title == "示例"
    assert result.results[0].score == 0.7
    assert len(seen) == 1
    assert seen[0].url == "https://api.tavily.com/search"
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert json.loads(seen[0].content) == {
        "query": "科研进展",
        "max_results": 3,
        "topic": "general",
        "search_depth": "advanced",
        "include_raw_content": False,
    }


async def test_custom_get_search_binds_scalars_without_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "hits": [
                    {"name": "标题", "link": "https://example.com", "text": "摘要"},
                    {
                        "name": "额外结果",
                        "link": "https://example.com/b",
                        "text": "摘要",
                    },
                ]
            },
        )

    client_for(monkeypatch, handler)
    config = SearchConfig(
        provider_id="custom",
        endpoint="http://127.0.0.1:9001/search",
        request=HttpRequestConfig(
            method="GET",
            auth="none",
            parameters={"q": "${query}", "limit": "${max_results}"},
            items_pointer="/hits",
            title_pointer="/name",
            url_pointer="/link",
            value_pointer="/text",
        ),
    )
    service = ResolvedService("service-2", config, "fingerprint", "")
    result = await service_http.search_web(
        service, SearchRequest("测试查询", max_results=1)
    )
    assert len(result.results) == 1
    assert result.results[0].content == "摘要"
    assert seen[0].method == "GET"
    assert seen[0].url.params["limit"] == "1"
    assert seen[0].content == b""
    assert "Authorization" not in seen[0].headers


async def test_fal_generation_downloads_without_reusing_generation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "images": [
                        {
                            "url": "https://fal.run/result.png?signature=one-time&expires=123"
                        }
                    ]
                },
                headers={"set-cookie": "session=private; Path=/"},
            )
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nimage")

    client_for(monkeypatch, handler)
    config = ImageConfig(
        provider_id="fal",
        endpoint="https://fal.run/fal-ai/flux/schnell",
        model="fal-ai/flux/schnell",
    )
    service = ResolvedService("service-3", config, "fingerprint", "fal-secret")
    data = await service_http.generate_image_bytes(service, "一只猫")
    assert data.startswith(b"\x89PNG")
    assert len(calls) == 2
    assert calls[0].headers["Authorization"] == "Key fal-secret"
    assert json.loads(calls[0].content)["num_images"] == 1
    assert "Authorization" not in calls[1].headers
    assert "Cookie" not in calls[1].headers
    assert calls[1].url.query == b"signature=one-time&expires=123"


async def test_openai_base64_and_custom_binary_use_one_generation_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(b"image").decode()}]}
            )
        return httpx.Response(200, content=b"binary-image")

    client_for(monkeypatch, handler)
    openai = ResolvedService(
        "openai", ImageConfig(model="image-model"), "fingerprint", "key"
    )
    custom = ResolvedService(
        "custom",
        ImageConfig(
            provider_id="custom",
            endpoint="https://example.com/generate",
            request=HttpRequestConfig(
                auth="none", parameters={"prompt": "${prompt}"}, response_type="binary"
            ),
        ),
        "fingerprint",
        "",
    )
    assert await service_http.generate_image_bytes(openai, "一只猫") == b"image"
    assert await service_http.generate_image_bytes(custom, "一只狗") == b"binary-image"
    assert len(calls) == 2
    assert json.loads(calls[0].content)["model"] == "image-model"
    assert json.loads(calls[1].content)["prompt"] == "一只狗"


async def test_supplier_error_has_no_body_and_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="secret response body")

    client_for(monkeypatch, handler)
    service = ResolvedService("search", SearchConfig(), "fingerprint", "key")
    with pytest.raises(ServiceHTTPError) as error:
        await service_http.search_web(service, SearchRequest("测试查询"))
    assert error.value.status_code == 429
    assert "secret response body" not in str(error.value)
    assert calls == 1


async def test_response_limit_and_multiple_images_reject_invalid_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_for(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
    )
    search = ResolvedService("search", SearchConfig(), "fingerprint", "key")
    with pytest.raises(ModelResponseTooLarge):
        await service_http.search_web(search, SearchRequest("测试查询"))

    client_for(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {"url": "https://example.com/1.png"},
                    {"url": "https://example.com/2.png"},
                ]
            },
        ),
    )
    image = ResolvedService(
        "image", ImageConfig(model="image-model"), "fingerprint", "key"
    )
    with pytest.raises(ValueError, match="恰好一张"):
        await service_http.generate_image_bytes(image, "一只猫")


async def test_network_timeout_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out")

    client_for(monkeypatch, handler)
    search = ResolvedService("search", SearchConfig(), "fingerprint", "key")
    with pytest.raises(httpx.ReadTimeout):
        await service_http.search_web(search, SearchRequest("测试查询"))
    assert calls == 1


async def test_cancellation_closes_owned_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class WaitingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("请求应被取消")

        async def aclose(self) -> None:
            closed.set()

    monkeypatch.setattr(
        service_http,
        "_client",
        lambda limit: httpx.AsyncClient(transport=WaitingTransport()),
    )
    service = ResolvedService("search", SearchConfig(), "fingerprint", "key")
    task = asyncio.create_task(
        service_http.search_web(service, SearchRequest("测试查询"))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
