"""从当前模型连接获取候选 ID，不推断服务未声明的能力"""

import socket

import httpx
from pydantic import BaseModel, Field, ValidationError

from tinkerfin_studio.models.schemas import (
    DiscoveredModel,
    ModelDiscoveryFailureCode,
    ModelDiscoveryResult,
    ModelProvider,
)
from tinkerfin_studio.models.transport import (
    ModelEndpointNotAllowed,
    ModelResponseTooLarge,
)


def discovery_failure_code(error: Exception) -> ModelDiscoveryFailureCode:
    """将模型目录请求的异常归为固定原因，不向浏览器传递供应商正文"""
    cause: BaseException | None = error
    for _ in range(8):
        if isinstance(cause, ModelEndpointNotAllowed):
            return "endpoint_not_allowed"
        if isinstance(cause, ModelResponseTooLarge):
            return "response_too_large"
        if isinstance(cause, (TimeoutError, httpx.TimeoutException)):
            return "timeout"
        if isinstance(cause, httpx.HTTPStatusError):
            status = cause.response.status_code
            if status in {401, 403}:
                return "authentication_failed"
            return "rate_limited" if status == 429 else "service_error"
        if isinstance(cause, (httpx.RequestError, socket.gaierror)):
            return "network_error"
        if cause is None:
            break
        cause = cause.__cause__
    return (
        "invalid_response"
        if isinstance(error, (ValueError, OSError))
        else "service_error"
    )


class _OpenAIModel(BaseModel):
    id: str = Field(min_length=1, max_length=128)


class _OpenAIModels(BaseModel):
    data: list[_OpenAIModel] = Field(max_length=2000)


class _OllamaModel(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class _OllamaModels(BaseModel):
    models: list[_OllamaModel] = Field(max_length=2000)


async def discover_models(
    provider: ModelProvider, base_url: str, api_key: str, client: httpx.AsyncClient
) -> ModelDiscoveryResult:
    """读取指定连接的模型目录，客户端由调用方关闭

    列表不完整、服务未提供列表或请求失败时保留明确状态，用户仍可手动添加。
    返回的模型名只用于展示和选择，不用于拼接后续 URL。

    Args:
        provider: 已选择的服务接口
        base_url: 已授权连接的基础地址
        api_key: 当前用户的密钥，无需认证时为空
        client: 具有超时和响应大小限制的客户端

    Returns:
        去重后的模型候选项或固定的失败原因
    """
    path = "/api/tags" if provider == "ollama" else "/models"
    response = await client.get(
        base_url.rstrip("/") + path,
        headers={"Authorization": "Bearer " + api_key} if api_key else {},
    )
    if response.status_code != 200:
        code = (
            "authentication_failed"
            if response.status_code in {401, 403}
            else "rate_limited"
            if response.status_code == 429
            else "models_unavailable"
        )
        return ModelDiscoveryResult(outcome="inconclusive", code=code)
    try:
        names = (
            [
                item.name
                for item in _OllamaModels.model_validate_json(response.content).models
            ]
            if provider == "ollama"
            else [
                item.id
                for item in _OpenAIModels.model_validate_json(response.content).data
            ]
        )
    except ValidationError:
        return ModelDiscoveryResult(outcome="inconclusive", code="models_unavailable")
    return ModelDiscoveryResult(
        outcome="success",
        code="models_received",
        items=[
            DiscoveredModel(model_name=name, display_name=name)
            for name in dict.fromkeys(names)
        ],
    )
