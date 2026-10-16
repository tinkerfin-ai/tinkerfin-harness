"""搜索和专用生图共用的有界同步 HTTP 调用"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES
from tinkerfin_studio.models.transport import (
    ModelResponseTooLarge,
    ModelTransport,
    validate_model_url,
)
from tinkerfin_studio.services.schemas import (
    HttpRequestConfig,
    ImageConfig,
    SearchConfig,
)
from tinkerfin_studio.services.service import ResolvedService

_SEARCH_LIMIT = 2 * 1024 * 1024
_IMAGE_RESPONSE_LIMIT = 15 * 1024 * 1024
_json_adapter = TypeAdapter(JsonValue)


class ServiceHTTPError(ValueError):
    """只暴露供应商 HTTP 状态，不传播响应正文"""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"外部服务返回 HTTP {status_code}")


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """Agent 可控制的搜索词和有限筛选参数"""

    query: str
    max_results: int = 5
    topic: Literal["general", "news"] = "general"


class SearchItem(BaseModel):
    """供 Agent 阅读的单条标准化搜索结果"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1)
    url: AnyHttpUrl
    content: str = ""
    score: float | None = None


class SearchResult(BaseModel):
    """网页搜索的稳定结果，与供应商字段无关"""

    answer: str | None = None
    results: list[SearchItem] = Field(default_factory=list)


def _client(limit: int) -> httpx.AsyncClient:
    """每次工具调用拥有并关闭自己的有界连接池"""
    return httpx.AsyncClient(
        transport=ModelTransport(response_limit_bytes=limit),
        timeout=httpx.Timeout(120, connect=15),
        trust_env=False,
        follow_redirects=False,
        headers={"Accept-Encoding": "identity"},
    )


def _bind(value: JsonValue, variables: dict[str, str | int]) -> JsonValue:
    """仅把完整变量占位符替换为原类型的值，不执行表达式"""
    if isinstance(value, str):
        if value.startswith("${") and value.endswith("}"):
            name = value[2:-1]
            if name not in variables:
                raise ValueError("请求参数使用了不支持的变量")
            return variables[name]
        if "${" in value:
            raise ValueError("变量须独立占据一个参数值")
    if isinstance(value, dict):
        return {key: _bind(child, variables) for key, child in value.items()}
    if isinstance(value, list):
        return [_bind(child, variables) for child in value]
    return value


def _pointer(value: JsonValue, pointer: str) -> JsonValue:
    """按 JSON Pointer 读取外部结果，不支持脚本或通配符"""
    current = value
    if not pointer:
        return current
    for raw in pointer.split("/")[1:]:
        segment = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif (
            isinstance(current, list)
            and segment.isdecimal()
            and int(segment) < len(current)
        ):
            current = current[int(segment)]
        else:
            raise ValueError("服务响应缺少配置的结果路径")
    return current


async def _request(
    client: httpx.AsyncClient,
    *,
    method: Literal["GET", "POST"],
    endpoint: str,
    headers: dict[str, str],
    parameters: dict[str, JsonValue],
    limit: int,
) -> bytes:
    validate_model_url(endpoint)
    query: dict[str, str] | None = None
    if method == "GET" and parameters:
        query = {}
        for key, value in parameters.items():
            if isinstance(value, (dict, list)):
                raise TypeError("GET 查询参数只支持标量")
            query[key] = (
                ""
                if value is None
                else str(value).lower()
                if isinstance(value, bool)
                else str(value)
            )
    async with client.stream(
        method,
        endpoint,
        headers=headers,
        params=query,
        json=parameters if method == "POST" else None,
    ) as response:
        if response.status_code < 200 or response.status_code >= 300:
            raise ServiceHTTPError(response.status_code)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise ModelResponseTooLarge("服务响应超过本次调用上限")
        return bytes(body)


def _json(body: bytes) -> JsonValue:
    try:
        return _json_adapter.validate_json(body)
    except ValidationError as error:
        raise ValueError("服务响应不是有效 JSON") from error


def _headers(
    request: HttpRequestConfig | None, key: str, *, preset: str = ""
) -> dict[str, str]:
    if request is None:
        return {"Authorization": ("Key " if preset == "fal" else "Bearer ") + key}
    if request.auth == "none":
        return {}
    if request.auth == "bearer":
        return {"Authorization": "Bearer " + key}
    return {request.header: request.prefix + key}


def _search_result(raw: JsonValue, config: SearchConfig, limit: int) -> SearchResult:
    if config.provider_id == "tavily":
        try:
            result = SearchResult.model_validate(raw)
        except ValidationError as error:
            raise ValueError("搜索服务响应格式不正确") from error
        return result.model_copy(update={"results": result.results[:limit]})
    request = config.request
    if request is None:
        raise ValueError("搜索请求缺少结果规则")
    items = _pointer(raw, request.items_pointer)
    if not isinstance(items, list):
        raise TypeError("搜索结果路径须指向列表")
    normalized: list[SearchItem] = []
    for item in items[:limit]:
        title = _pointer(item, request.title_pointer)
        url = _pointer(item, request.url_pointer)
        content = _pointer(item, request.value_pointer) if request.value_pointer else ""
        score = _pointer(item, request.score_pointer) if request.score_pointer else None
        try:
            normalized.append(
                SearchItem.model_validate(
                    {"title": title, "url": url, "content": content, "score": score}
                )
            )
        except ValidationError as error:
            raise ValueError("搜索服务结果字段不正确") from error
    return SearchResult(results=normalized)


async def search_web(service: ResolvedService, request: SearchRequest) -> SearchResult:
    """调用本次绑定的搜索服务一次，返回有界且标准化的结果

    Args:
        service: 已固定归属、配置和凭证的个人搜索服务
        request: 搜索词、最多结果数与主题

    Returns:
        标题、来源与摘要组成的搜索结果

    Raises:
        ValueError: 请求或供应商响应不符合当前配置
        httpx.HTTPError: 网络请求失败
    """
    config = service.configuration
    if not isinstance(config, SearchConfig):
        raise TypeError("当前服务不能用于网页搜索")
    if (
        len(request.query.strip()) < 2
        or len(request.query) > 500
        or not 1 <= request.max_results <= 10
    ):
        raise ValueError("搜索词或结果数量不符合要求")
    if config.provider_id == "tavily":
        endpoint = config.endpoint.rstrip("/") + "/search"
        parameters: dict[str, JsonValue] = {
            **config.extra,
            "query": request.query,
            "max_results": min(request.max_results, config.max_results),
            "topic": request.topic,
            "search_depth": config.depth,
        }
        method: Literal["GET", "POST"] = "POST"
    else:
        declaration = config.request
        if declaration is None:
            raise ValueError("自定义搜索缺少请求规则")
        endpoint = config.endpoint
        bound = _bind(
            declaration.parameters,
            {
                "query": request.query,
                "max_results": min(request.max_results, config.max_results),
                "topic": request.topic,
            },
        )
        if not isinstance(bound, dict):
            raise ValueError("搜索请求参数须为对象")
        parameters = bound
        method = declaration.method
    async with _client(_SEARCH_LIMIT) as client:
        body = await _request(
            client,
            method=method,
            endpoint=endpoint,
            headers=_headers(config.request, service.api_key),
            parameters=parameters,
            limit=_SEARCH_LIMIT,
        )
    return _search_result(
        _json(body), config, min(request.max_results, config.max_results)
    )


def _single_image(raw: JsonValue, config: ImageConfig) -> tuple[str, str]:
    if config.provider_id == "openai":
        items = _pointer(raw, "/data")
        pointer = ""
    elif config.provider_id == "fal":
        items = _pointer(raw, "/images")
        pointer = "/url"
    else:
        declaration = config.request
        if declaration is None:
            raise ValueError("自定义生图缺少结果规则")
        items = _pointer(raw, declaration.items_pointer)
        pointer = declaration.value_pointer
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("生图服务须返回恰好一张图片")
    image = items[0]
    if config.provider_id == "openai":
        if not isinstance(image, dict):
            raise ValueError("生图服务响应格式不正确")
        encoded = image.get("b64_json")
        if isinstance(encoded, str):
            return "base64", encoded
        url = image.get("url")
        if isinstance(url, str):
            return "url", url
        raise ValueError("生图服务未返回图片 URL 或 Base64")
    value = _pointer(image, pointer)
    if not isinstance(value, str):
        raise TypeError("生图服务图片结果须为字符串")
    if config.provider_id == "fal":
        return "url", value
    declaration = config.request
    if declaration is None:
        raise ValueError("自定义生图缺少结果规则")
    return declaration.response_type, value


def _decode_image(encoded: str) -> bytes:
    if len(encoded) > 14_000_000:
        raise ModelResponseTooLarge("生成图片超过 10 MiB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("生图服务返回的图片编码不正确") from error
    if len(data) > MAX_FILE_BYTES:
        raise ModelResponseTooLarge("生成图片超过 10 MiB")
    return data


def image_extension(data: bytes) -> Literal["png", "jpg", "webp"]:
    """在工作文件或测试结果发布前确认图片原图格式"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    raise ValueError("生图服务须返回 PNG、JPEG 或 WebP 图片")


async def generate_image_bytes(service: ResolvedService, prompt: str) -> bytes:
    """调用本次绑定的生图服务一次，取回一张有界原图

    Args:
        service: 已固定归属、配置和凭证的个人生图服务
        prompt: 一到四千字符的图片描述

    Returns:
        经过大小限制的原图字节

    Raises:
        ValueError: 请求或服务响应不符合配置
        httpx.HTTPError: 网络请求或图片下载失败
    """
    config = service.configuration
    if not isinstance(config, ImageConfig):
        raise TypeError("当前服务不能用于图片生成")
    if not prompt.strip() or len(prompt) > 4000:
        raise ValueError("图片描述须为 1 到 4000 字符")
    if config.provider_id == "openai":
        endpoint = config.endpoint.rstrip("/") + "/images/generations"
        parameters: dict[str, JsonValue] = {
            **config.extra,
            "model": config.model,
            "prompt": prompt,
            "n": 1,
        }
        if config.size:
            parameters["size"] = config.size
        method: Literal["GET", "POST"] = "POST"
    elif config.provider_id == "fal":
        endpoint = config.endpoint
        parameters = {**config.extra, "prompt": prompt, "num_images": 1}
        if config.size:
            parameters["image_size"] = config.size
        method = "POST"
    else:
        declaration = config.request
        if declaration is None:
            raise ValueError("自定义生图缺少请求规则")
        endpoint = config.endpoint
        bound = _bind(declaration.parameters, {"prompt": prompt})
        if not isinstance(bound, dict):
            raise ValueError("生图请求参数须为对象")
        parameters = bound
        method = declaration.method
    async with _client(_IMAGE_RESPONSE_LIMIT) as client:
        body = await _request(
            client,
            method=method,
            endpoint=endpoint,
            headers=_headers(
                config.request, service.api_key, preset=config.provider_id
            ),
            parameters=parameters,
            limit=_IMAGE_RESPONSE_LIMIT,
        )
    if (
        config.provider_id == "custom"
        and config.request is not None
        and config.request.response_type == "binary"
    ):
        if len(body) > MAX_FILE_BYTES:
            raise ModelResponseTooLarge("生成图片超过 10 MiB")
        return body
    kind, value = _single_image(_json(body), config)
    if kind == "base64":
        return _decode_image(value)
    validate_model_url(value)
    async with _client(MAX_FILE_BYTES) as client:
        downloaded = await _request(
            client,
            method="GET",
            endpoint=value,
            headers={},
            parameters={},
            limit=MAX_FILE_BYTES,
        )
        return downloaded
