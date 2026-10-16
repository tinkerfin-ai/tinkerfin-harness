"""模型与个人服务的 HTTP 地址校验、响应限量和连接池归属"""

from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
import httpx


class ModelResponseTooLarge(ValueError):
    """上游响应超过本次调用允许接收的字节数"""


class _BoundedModelStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        async for chunk in self._stream:
            size += len(chunk)
            if size > self._limit:
                raise ModelResponseTooLarge("服务响应超过本次调用上限")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def validate_model_url(value: str) -> httpx.URL:
    """接受后端可达的不含账户信息的 HTTP/HTTPS 服务地址"""
    url = httpx.URL(value)
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ValueError("请使用不含账户信息的 HTTP 或 HTTPS 服务地址")
    return url


class ModelTransport(httpx.AsyncBaseTransport):
    """拥有有界 HTTP 连接池，允许公网、本机及内网地址

    HTTPX 负责 DNS、Host 与 TLS 校验，关闭宿主客户端时释放连接池。
    有界响应仅接受未压缩内容，避免压缩体绕过接收上限。
    """

    def __init__(self, *, response_limit_bytes: int | None = None) -> None:
        self._limit = response_limit_bytes
        self._transport = httpx.AsyncHTTPTransport(
            retries=0,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=20),
        )
        self._closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._closed:
            raise RuntimeError("服务连接池已关闭")
        validate_model_url(str(request.url))
        if request.headers.get("Authorization") == "":
            request.headers.pop("Authorization", None)
        response = await self._transport.handle_async_request(request)
        if self._limit is not None:
            if response.headers.get("content-encoding", "identity") != "identity":
                await response.aclose()
                raise ValueError("有界服务请求不接受压缩响应")
            if not isinstance(response.stream, httpx.AsyncByteStream):
                await response.aclose()
                raise TypeError("服务未返回异步响应")
            response.stream = _BoundedModelStream(response.stream, self._limit)
        return response

    async def aclose(self) -> None:
        self._closed = True
        with anyio.CancelScope(shield=True):
            await self._transport.aclose()
