"""用户配置模型地址的网络访问边界"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import AsyncIterator

import anyio
import httpx


class ModelEndpointNotAllowed(ValueError):
    """模型地址不符合管理员允许的网络访问边界"""


class ModelResponseTooLarge(ValueError):
    """模型响应超过本次调用允许接收的字节数"""


class _BoundedModelStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        async for chunk in self._stream:
            size += len(chunk)
            if size > self._limit:
                raise ModelResponseTooLarge("模型响应超过本次调用上限")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def validate_model_url(value: str) -> httpx.URL:
    """校验模型服务和图片下载使用的不含账户信息的 HTTP/HTTPS 地址"""
    url = httpx.URL(value)
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ValueError("请使用不含账户信息的 HTTP 或 HTTPS 服务地址")
    return url


def normalize_model_origin(value: str) -> str:
    """规范化管理员允许的服务来源，只接受协议、主机和端口"""
    url = validate_model_url(value)
    if url.raw_path != b"/" or url.fragment or "*" in url.host:
        raise ValueError(
            "MODEL_ALLOWED_ORIGINS 只能包含协议、主机和端口，不能包含路径或通配符"
        )
    return str(url.copy_with(path="/"))


class ModelTransport(httpx.AsyncBaseTransport):
    """连接公网 HTTPS 或管理员明确允许的模型服务，并固定解析结果

    allowed_origins 按协议、主机和端口精确授权 HTTP、本机及内网访问，
    同时适用于模型请求和生成图片下载；未列出的来源只允许公网 HTTPS。
    每个协议、域名与解析地址使用独立连接池，保留原始 Host 和 TLS 服务名，
    避免检查 DNS 后再次解析，或跨域名复用同一 IP 的 TLS 连接。
    宿主 AsyncClient 拥有连接池，并在关闭时等待全部池释放。
    指定 response_limit_bytes 时只接收未压缩响应，调用方应请求 identity 编码。
    """

    def __init__(
        self,
        *,
        allowed_origins: tuple[str, ...] = (),
        response_limit_bytes: int | None = None,
    ) -> None:
        self._closed = False
        self._response_limit_bytes = response_limit_bytes
        self._allowed_origins = frozenset(
            normalize_model_origin(value) for value in allowed_origins
        )
        self._transports: dict[tuple[str, str, str, int], httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """校验服务来源并解析目的地，把请求发往已确认的地址"""
        if self._closed:
            raise RuntimeError("模型连接池已关闭")
        # 免密连接显式覆盖 SDK 认证后，网络请求不携带空认证头
        if request.headers.get("Authorization") == "":
            request.headers.pop("Authorization", None)
        url = validate_model_url(str(request.url))
        host = url.host
        port = url.port or (443 if url.scheme == "https" else 80)
        origin = str(url.copy_with(path="/", query=None, fragment=None))
        explicitly_allowed = origin in self._allowed_origins
        if url.scheme != "https" and not explicitly_allowed:
            raise ModelEndpointNotAllowed(
                "HTTP 模型或图片服务需由管理员加入 MODEL_ALLOWED_ORIGINS"
            )
        with anyio.fail_after(15):
            addresses = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = [str(item[4][0]) for item in addresses]
        if not ips:
            raise ValueError("模型服务或图片地址无法解析")
        if not explicitly_allowed and any(
            not ipaddress.ip_address(ip).is_global for ip in ips
        ):
            raise ModelEndpointNotAllowed(
                "本机与私有网络服务需由管理员加入 MODEL_ALLOWED_ORIGINS"
            )
        candidates = list(dict.fromkeys(ips))
        # 本地服务可能只监听 IPv4 或 IPv6；仅在请求尚未发送的连接失败时换地址
        for address in candidates[:-1]:
            try:
                return await self._send_to_address(request, address, port)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                continue
        return await self._send_to_address(request, candidates[-1], port)

    async def _send_to_address(
        self, request: httpx.Request, address: str, port: int
    ) -> httpx.Response:
        host = request.url.host
        key = (request.url.scheme, host, address, port)
        transport = self._transports.get(key)
        if transport is None:
            if len(self._transports) >= 64:
                raise ValueError("模型服务连接域名数量达到当前进程上限")
            transport = httpx.AsyncHTTPTransport(retries=0)
            self._transports[key] = transport
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        response = await transport.handle_async_request(pinned)
        if self._response_limit_bytes is not None:
            # 有界调用只接收未压缩响应，避免压缩体绕过内存限制
            if response.headers.get("content-encoding", "identity") != "identity":
                await response.aclose()
                raise ValueError("有界模型请求不接受压缩响应")
            if not isinstance(response.stream, httpx.AsyncByteStream):
                await response.aclose()
                raise TypeError("模型服务未返回异步响应")
            response.stream = _BoundedModelStream(
                response.stream, self._response_limit_bytes
            )
        return response

    async def aclose(self) -> None:
        """关闭本次客户端拥有的全部连接池"""
        self._closed = True
        with anyio.CancelScope(shield=True):
            for transport in self._transports.values():
                await transport.aclose()
        self._transports.clear()
