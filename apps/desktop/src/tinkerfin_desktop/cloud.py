"""设备认证的有界 HTTP 与 WebSocket 通信，不拥有 Agent 执行"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Generic, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter
from websockets.asyncio.client import ClientConnection, connect

from tinkerfin_contracts.media import Attachment
from tinkerfin_remote import WorkspaceRequest

from .assets import FileContent, SkillDownload
from .configuration import (
    CompanionConfiguration,
    DeviceId,
    ExecutionConfiguration,
    PairedDevice,
)
from .lifecycle import settle

Value = TypeVar("Value")


class StudioResponse(BaseModel, Generic[Value]):
    """Studio 当前 HTTP 响应包络"""

    code: int
    message: str
    data: Value | None = None


class CloudError(Exception):
    """云端拒绝或传输失败，保留状态码和安全错误说明"""

    def __init__(
        self, status: int, code: int | None = None, *, detail: str | None = None
    ) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"Studio request failed: status={status}, code={code}")


class DeviceCapabilities(BaseModel):
    """本机当前可用能力，不包含目录、凭证或持久授权配置"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    browser: bool = False
    mcp: bool = False
    desktop: bool = False
    desktop_capture: bool = False
    open_url: bool = False


class _ConnectedDevice(PairedDevice):
    model_config = ConfigDict(extra="forbid", frozen=True)
    connection_id: DeviceId


class StudioClient:
    """借用应用客户端，所有请求固定到已配对的 Studio 地址"""

    def __init__(
        self, configuration: CompanionConfiguration, client: httpx.AsyncClient
    ) -> None:
        self.configuration = configuration
        self._client = client
        self._cpu = asyncio.Semaphore(2)
        self._connecting = False
        self._connection_id: str | None = None

    def _execution_url(self, request: WorkspaceRequest, resource: str) -> str:
        """资源仅指向当前设备已认证的执行授权，不接受任意 URL"""
        authorization = request.authorization
        if (
            request.target != self.configuration.device_id
            or request.identity.namespace != self.configuration.namespace
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                authorization,
            )
            is None
            or resource not in {"configuration", "skills", "files"}
        ):
            raise ValueError("执行不属于当前设备或授权身份无效")
        return f"{self.configuration.server}/api/devices/executions/{authorization}/{resource}"

    @staticmethod
    def _identity_params(request: WorkspaceRequest) -> dict[str, str]:
        return {
            "thread_id": request.identity.thread_id,
            "run_id": request.identity.run_id,
        }

    def _asset_url(
        self, request: WorkspaceRequest, resource: str, identifier: str
    ) -> str:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier) is None:
            raise ValueError("文件或技能身份无效")
        return self._execution_url(request, resource) + "/" + identifier

    async def _request(
        self,
        method: str,
        url: str,
        *,
        limit: int = 128 * 1024,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        json_body: dict[str, JsonValue] | None = None,
    ) -> bytes:
        async with self._client.stream(
            method,
            url,
            params=params,
            content=content,
            json=json_body,
            follow_redirects=False,
            headers={
                "Authorization": self.configuration.authorization,
                "Accept-Encoding": "identity",
            },
        ) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > limit:
                    raise CloudError(502)
                body.extend(chunk)
            if response.status_code != 200:
                try:
                    failure = StudioResponse[JsonValue].model_validate_json(body)
                except ValueError:
                    raise CloudError(response.status_code) from None
                raise CloudError(
                    response.status_code, failure.code, detail=failure.message[:2000]
                )
            return bytes(body)

    async def _decode(
        self, schema: type[StudioResponse[Value]], content: bytes
    ) -> Value:
        # 大包解析最多使用两个工作线程；取消等到已开始的解析返回
        async with self._cpu:
            payload = await settle(
                asyncio.to_thread(schema.model_validate_json, content)
            )
        if payload.code != 0 or payload.data is None:
            raise CloudError(400, payload.code)
        return payload.data

    async def configuration_for(
        self, request: WorkspaceRequest
    ) -> ExecutionConfiguration:
        """核对当前云端运行的项目授权，设备和账号仍以原生配置为准"""
        selected = await self._decode(
            StudioResponse[ExecutionConfiguration],
            await self._request(
                "GET",
                self._execution_url(request, "configuration"),
                params=self._identity_params(request),
            ),
        )
        if selected.authorization != request.authorization:
            raise ValueError("执行配置与当前授权不一致")
        return selected

    async def skill(
        self, request: WorkspaceRequest, installation_id: str
    ) -> SkillDownload:
        """下载当前执行已授权的技能，调用方核对摘要后才发布只读文件"""
        return await self._decode(
            StudioResponse[SkillDownload],
            await self._request(
                "GET",
                self._asset_url(request, "skills", installation_id),
                params=self._identity_params(request),
                limit=48 * 1024 * 1024,
            ),
        )

    async def attachments(self, request: WorkspaceRequest) -> list[Attachment]:
        """列出当前云端运行所属会话的附件"""
        return await self._decode(
            StudioResponse[list[Attachment]],
            await self._request(
                "GET",
                self._execution_url(request, "files"),
                params=self._identity_params(request),
            ),
        )

    async def deliver(
        self,
        request: WorkspaceRequest,
        invocation_id: str,
        name: str,
        content: bytes,
        *,
        file_key: str,
    ) -> Attachment:
        """保存文件并返回云端收据，稳定文件身份允许跨调用重传"""
        if not content or len(content) > 10 * 1024 * 1024:
            raise ValueError("交付文件必须非空且不超过 10 MiB")
        return await self._decode(
            StudioResponse[Attachment],
            await self._request(
                "POST",
                self._execution_url(request, "files"),
                params={
                    **self._identity_params(request),
                    "invocation_id": invocation_id,
                    "file_key": file_key,
                    "name": name,
                },
                content=content,
            ),
        )

    async def read(self, request: WorkspaceRequest, attachment: Attachment) -> bytes:
        """读取已授权附件原件；模型预览由云端 Agent 直接处理"""
        payload = await self._decode(
            StudioResponse[FileContent],
            await self._request(
                "GET",
                self._asset_url(request, "files", attachment.id),
                params=self._identity_params(request),
                limit=14 * 1024 * 1024,
            ),
        )
        async with self._cpu:
            data = await settle(
                asyncio.to_thread(base64.b64decode, payload.content, validate=True)
            )
        if len(data) > 10 * 1024 * 1024:
            raise CloudError(502)
        return data

    async def update_capabilities(self, capabilities: DeviceCapabilities) -> bool:
        """只更新当前连接的能力，离线时由下一次握手提供最新状态"""
        connection_id = self._connection_id
        if connection_id is None:
            return False
        payload: dict[str, JsonValue] = {
            "connection_id": connection_id,
            "capabilities": capabilities.model_dump(mode="json"),
        }
        accepted = await self._decode(
            StudioResponse[DeviceCapabilities],
            await self._request(
                "PUT",
                self.configuration.server + "/api/devices/presence",
                json_body=payload,
            ),
        )
        if accepted != capabilities:
            raise CloudError(502)
        return True

    @asynccontextmanager
    async def connect(
        self, capabilities: DeviceCapabilities
    ) -> AsyncIterator[ClientConnection]:
        """建立主动连接并校验握手；退出仅释放 Socket，不取消已接纳的工具执行"""
        if self._connecting:
            raise RuntimeError("同一本机客户端只能建立一条连接")
        self._connecting = True
        try:
            url = httpx.URL(self.configuration.server + "/api/devices/connect")
            url = url.copy_with(scheme="wss" if url.scheme == "https" else "ws")
            async with connect(
                str(url),
                additional_headers={"Authorization": self.configuration.authorization},
                max_size=8 * 1024 * 1024,
                max_queue=8,
                open_timeout=10,
                close_timeout=10,
                proxy=None,
            ) as socket:
                payload: dict[str, JsonValue] = {
                    "capabilities": capabilities.model_dump(mode="json"),
                }
                await socket.send(
                    TypeAdapter(dict[str, JsonValue]).dump_json(payload).decode()
                )
                response = await socket.recv()
                if not isinstance(response, str) or len(response.encode()) > 64 * 1024:
                    raise CloudError(502)
                device = _ConnectedDevice.model_validate_json(response)
                if (
                    device.device_id != self.configuration.device_id
                    or device.namespace != self.configuration.namespace
                ):
                    raise CloudError(401)
                self._connection_id = device.connection_id
                yield socket
        finally:
            self._connection_id = None
            self._connecting = False
