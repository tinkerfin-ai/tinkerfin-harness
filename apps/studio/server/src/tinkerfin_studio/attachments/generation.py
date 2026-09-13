"""OpenAI 兼容图片生成与持久化前的有界下载"""

from __future__ import annotations

import base64
import binascii

import httpx
from pydantic import BaseModel, Field, ValidationError

from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.models.transport import ModelResponseTooLarge, ModelTransport


class ImageGenerationHTTPError(ValueError):
    """图片生成服务返回失败状态，不携带上游响应正文"""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            f"图片生成服务返回 HTTP {status_code}，请检查模型开通状态与配额"
        )


class _GeneratedImage(BaseModel):
    url: str | None = None
    b64_json: str | None = None


class _ImageResponse(BaseModel):
    data: list[_GeneratedImage] = Field(min_length=1, max_length=1)


async def generate_image_bytes(
    model: AgentModelConfig | None,
    prompt: str,
    *,
    allowed_origins: tuple[str, ...] = (),
) -> bytes:
    """调用一次兼容接口生图，下载官方存储结果，不自动重试收费请求

    下载请求不携带生成服务密钥，不跟随重定向，结果仅来自服务端返回的
    公网 HTTPS 或管理员允许的服务来源，并固定解析结果。
    临时 URL 不进入消息或 Trace。

    Args:
        model: 当前用户选择的图片生成服务
        prompt: 图片描述，长度为 1 到 4000 字符
        allowed_origins: 管理员允许的 HTTP、本机或内网服务来源

    Returns:
        不超过附件大小上限的图片字节

    Raises:
        ValueError: 配置、地址、响应格式或图片大小不符合要求
        httpx.HTTPError: 模型请求或图片下载发生网络错误
    """
    if model is None:
        raise ValueError("请先在模型配置中添加、启用并设定默认生图服务")
    if not prompt.strip() or len(prompt) > 4000:
        raise ValueError("图片描述须为 1 到 4000 字符")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120, connect=15),
        trust_env=False,
        headers={"Accept-Encoding": "identity"},
        follow_redirects=False,
        transport=ModelTransport(
            allowed_origins=allowed_origins, response_limit_bytes=15 * 1024 * 1024
        ),
    ) as client:
        async with client.stream(
            "POST",
            model.base_url.rstrip("/") + "/images/generations",
            headers={"Authorization": f"Bearer {model.api_key.get_secret_value()}"}
            if model.api_key.get_secret_value()
            else {},
            json={
                "model": model.model_name,
                "prompt": prompt,
                **model.generation_options,
            },
        ) as response:
            if response.is_error:
                raise ImageGenerationHTTPError(response.status_code)
            metadata = bytearray()
            async for chunk in response.aiter_bytes():
                metadata.extend(chunk)
                if len(metadata) > 15 * 1024 * 1024:
                    raise ModelResponseTooLarge("图片生成服务响应超限")
        try:
            result = _ImageResponse.model_validate_json(metadata)
        except ValidationError as error:
            raise ValueError(
                "生图服务响应格式不正确，需要单张图片的 url 或 b64_json"
            ) from error
        image = result.data[0]
        if image.b64_json is not None:
            if len(image.b64_json) > 14_000_000:
                raise ModelResponseTooLarge("生成图片超过 10 MiB")
            try:
                decoded = base64.b64decode(image.b64_json, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ValueError("生图服务返回的图片编码不正确") from error
            if len(decoded) > MAX_FILE_BYTES:
                raise ModelResponseTooLarge("生成图片超过 10 MiB")
            return decoded
        if not image.url:
            raise ValueError("图片服务未返回 url 或 b64_json")
        url = image.url
        data = bytearray()
        async with client.stream("GET", url) as download:
            if download.status_code != 200:
                raise ValueError("生成图片下载失败，未发布附件；不会自动重新生图")
            async for chunk in download.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_FILE_BYTES:
                    raise ModelResponseTooLarge("生成图片超过 10 MiB，请缩小生成尺寸")
        return bytes(data)
