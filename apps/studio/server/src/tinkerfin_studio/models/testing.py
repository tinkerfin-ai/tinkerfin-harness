"""当前模型草稿的独立检查，不保存配置或创建会话资源"""

from __future__ import annotations

import asyncio
import base64
import socket
import time

import anyio
import httpx
from anyio import to_thread
from langchain_core.messages import HumanMessage
from ollama import ResponseError

from tinkerfin_studio.attachments.generation import (
    ImageGenerationHTTPError,
    generate_image_bytes,
)
from tinkerfin_studio.attachments.processing import image_variant
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.discovery import discover_models
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import (
    AgentModelConfig,
    ModelTestCode,
    ModelTestImage,
    ModelTestKind,
    ModelTestRequest,
    ModelTestResult,
)
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_studio.models.transport import (
    ModelEndpointNotAllowed,
    ModelResponseTooLarge,
    ModelTransport,
)

TEST_TIMEOUT_SECONDS: dict[ModelTestKind, float] = {
    "basic": 10,
    "text": 30,
    "vision": 45,
    "image": 120,
}
_RESPONSE_LIMIT_BYTES = 256 * 1024
_PREVIEW_LIMITER = anyio.CapacityLimiter(2)
# 固定的 96×48 白底图片，左侧红方块、右侧蓝方块，用于人工核对看图回复
_VISION_IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAAAwCAIAAABhdOiYAAAAjElEQVR4nO3asQ3AIAwAQYiy/8pk"
    "AniliNLctW7Qy4UL5lprsHcdZgjUbFAQKAgUBAoChXs7mXN86uX99ddzbFAQKAgUBAoCBYGCQEGg"
    "IFAQKAgUBAoCBYGCQEGgIFAQKAgUBAoCBYGCQEGgIFAQKAgUBAoCBYGCQEGgIFAQKAgUBAoCBYGC"
    "QGH6J31mg4JAQaAgUBAoCBQEGmcPPnMKW0Yha/wAAAAASUVORK5CYII="
)


def _http_error_code(status: int) -> ModelTestCode:
    if status in {401, 403}:
        return "authentication_failed"
    if status == 429:
        return "rate_limited"
    return "service_error"


def _failure_code(error: Exception) -> ModelTestCode:
    # 模型 SDK 会封装网络异常；沿异常链读取类型，不把其消息传给浏览器
    cause: BaseException | None = error
    for _ in range(8):
        if isinstance(cause, ModelEndpointNotAllowed):
            return "endpoint_not_allowed"
        if isinstance(cause, ModelResponseTooLarge):
            return "response_too_large"
        if isinstance(cause, (TimeoutError, httpx.TimeoutException)):
            return "timeout"
        if isinstance(cause, httpx.HTTPStatusError):
            return _http_error_code(cause.response.status_code)
        if isinstance(cause, ImageGenerationHTTPError):
            return _http_error_code(cause.status_code)
        if isinstance(cause, (httpx.RequestError, socket.gaierror)):
            return "network_error"
        if cause is None:
            break
        cause = cause.__cause__
    if isinstance(error, ResponseError) and error.status_code is not None:
        return _http_error_code(error.status_code)
    if isinstance(error, (ValueError, OSError)):
        return "invalid_response"
    return "service_error"


async def _raise_http_error(response: httpx.Response) -> None:
    response.raise_for_status()


async def _check_model_list(
    config: AgentModelConfig, client: httpx.AsyncClient
) -> ModelTestResult:
    result = await discover_models(
        config.provider, config.base_url, config.api_key.get_secret_value(), client
    )
    if result.outcome != "success":
        return ModelTestResult.model_validate(
            {
                "kind": "basic",
                "outcome": "inconclusive",
                "code": result.code,
                "elapsed_ms": 0,
            }
        )
    listed = any(item.model_name == config.model_name for item in result.items)
    return ModelTestResult(
        kind="basic",
        outcome="success" if listed else "inconclusive",
        code="model_listed" if listed else "model_not_listed",
        elapsed_ms=0,
    )


async def _check_chat(
    config: AgentModelConfig,
    kind: ModelTestKind,
    client: httpx.AsyncClient,
    transport: ModelTransport | None = None,
) -> ModelTestResult:
    model = create_chat_model(
        config,
        http_async_client=client,
        http_async_transport=transport,
        timeout=TEST_TIMEOUT_SECONDS[kind],
        max_tokens=512,
        max_retries=0,
    )
    image = None
    message = HumanMessage(content="Reply briefly with OK to confirm you can respond.")
    if kind == "vision":
        image = ModelTestImage(mime_type="image/png", data_base64=_VISION_IMAGE_BASE64)
        message = HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": "Describe the colors and positions of the shapes in this image in one short sentence.",
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + _VISION_IMAGE_BASE64
                    },
                },
            ]
        )
    response = await model.ainvoke([message])
    text = response.text.strip()[:2000]
    key = config.api_key.get_secret_value()
    if key:
        text = text.replace(key, "[redacted]")
    return ModelTestResult(
        kind=kind,
        outcome="success" if text else "inconclusive",
        elapsed_ms=0,
        code=("vision_response_received" if kind == "vision" else "text_received")
        if text
        else "empty_response",
        text=text or None,
        image=image,
    )


async def _image_preview(data: bytes) -> bytes:
    # 原生任务取消不会等待 AnyIO 线程完成；本次预览明确拥有并收回处理任务
    processing = asyncio.create_task(
        to_thread.run_sync(image_variant, data, 512, limiter=_PREVIEW_LIMITER)
    )
    try:
        return await asyncio.shield(processing)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            while not processing.done():
                try:
                    await asyncio.shield(processing)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 - 已取消时只回收处理异常，保留原取消语义
                    break
        if not processing.cancelled():
            processing.exception()
        raise


async def run_model_test(
    database: Database,
    *,
    user_id: int,
    payload: ModelTestRequest,
    allowed_origins: tuple[str, ...] = (),
) -> ModelTestResult:
    """测试本人当前草稿，短暂读取密钥后归还数据库连接，再访问模型服务

    每次只执行一种测试。总网络时限按类型为 10、30、45、120 秒，不重试。
    客户端随本次测试关闭，取消向调用方传播。图片缩放使用两个线程的容量
    限制；取消时等待已开始的有界缩放完成，避免遗留后台图片处理。

    Args:
        database: 用于读取本人已保存密钥的应用数据库
        user_id: 通过认证的用户标识
        payload: 当前未保存的配置和测试类型
        allowed_origins: 管理员允许的本机、HTTP 或内网模型服务来源

    Returns:
        有界且不携带供应商报错正文的测试结果

    Raises:
        BusinessException: 草稿缺少密钥或试图跨服务地址复用密钥
    """
    async with database.session() as session:
        config = await AgentModelService(
            AgentModelRepository(session, user_id=user_id)
        ).resolve_draft(payload.configuration)
    started = time.monotonic()
    kind = payload.kind
    try:
        with anyio.fail_after(TEST_TIMEOUT_SECONDS[kind]):
            if kind == "image":
                data = await generate_image_bytes(
                    config,
                    "A simple blue circle on a white background, no text.",
                    allowed_origins=allowed_origins,
                )
                preview = await _image_preview(data)
                if len(preview) > _RESPONSE_LIMIT_BYTES:
                    raise ModelResponseTooLarge("图片测试预览超过上限")
                result = ModelTestResult(
                    kind=kind,
                    outcome="success",
                    code="image_received",
                    elapsed_ms=0,
                    image=ModelTestImage(
                        mime_type="image/jpeg",
                        data_base64=base64.b64encode(preview).decode("ascii"),
                    ),
                )
            else:
                transport = ModelTransport(
                    allowed_origins=allowed_origins,
                    response_limit_bytes=_RESPONSE_LIMIT_BYTES,
                )
                async with httpx.AsyncClient(
                    timeout=TEST_TIMEOUT_SECONDS[kind],
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                    transport=transport,
                    event_hooks={"response": [_raise_http_error]}
                    if kind != "basic"
                    else {},
                ) as client:
                    result = (
                        await _check_model_list(config, client)
                        if kind == "basic"
                        else await _check_chat(config, kind, client, transport)
                    )
    except Exception as error:  # noqa: BLE001 - 不可信供应商与 SDK 的异常仅转换为固定错误码
        result = ModelTestResult(
            kind=kind, outcome="failed", code=_failure_code(error), elapsed_ms=0
        )
    return result.model_copy(
        update={"elapsed_ms": max(0, round((time.monotonic() - started) * 1000))}
    )
