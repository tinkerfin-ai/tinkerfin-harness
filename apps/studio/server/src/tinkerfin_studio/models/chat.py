"""按当前用户连接构建聊天模型，并声明本次可直接使用的媒体输入"""

from collections.abc import Callable, Mapping
from typing import Literal

import httpx
from langchain_core.language_models import BaseChatModel, ModelProfile

from tinkerfin_studio.models.providers import (
    ChatModelOptions,
    create_deepseek_model,
    create_ollama_model,
    create_openai_model,
)
from tinkerfin_studio.models.schemas import AgentModelConfig

_MODEL_PROVIDERS: Mapping[
    Literal["openai", "deepseek", "ollama"], Callable[[ChatModelOptions], BaseChatModel]
] = {
    "openai": create_openai_model,
    "deepseek": create_deepseek_model,
    "ollama": create_ollama_model,
}


def _input_profile(
    config: AgentModelConfig, supplied: ModelProfile | None
) -> ModelProfile:
    """仅采信官方连接的型号资料，自定义服务使用人工声明"""
    profile = (supplied or {}).copy()
    endpoint = httpx.URL(config.base_url)
    trusted = (
        config.provider_id == "openai"
        and config.provider == "openai"
        and str(endpoint).rstrip("/") == "https://api.openai.com/v1"
    ) or (
        config.provider_id == "deepseek"
        and config.provider == "deepseek"
        and str(endpoint).rstrip("/")
        in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
    )
    # 集成的 profile 按型号查表，不校验服务地址；不能让兼容服务继承同名模型的能力
    if not trusted:
        profile.pop("image_inputs", None)
        profile.pop("image_url_inputs", None)
        profile.pop("pdf_inputs", None)
        profile.pop("audio_inputs", None)
        profile.pop("video_inputs", None)
        profile.pop("image_tool_message", None)
        profile.pop("pdf_tool_message", None)
        profile.pop("attachment", None)
        profile.pop("image_outputs", None)
        profile.pop("audio_outputs", None)
        profile.pop("video_outputs", None)
    profile["image_inputs"] = (
        profile.get("image_inputs") is True
        if config.image_support == "unknown"
        else config.image_support == "supported"
    )
    # 未确认的媒体字段显式为 False，使原生文件工具与附件读取遵循相同声明
    profile["pdf_inputs"] = profile.get("pdf_inputs") is True
    profile["audio_inputs"] = profile.get("audio_inputs") is True
    profile["video_inputs"] = profile.get("video_inputs") is True
    profile["image_tool_message"] = profile.get("image_tool_message") is True
    profile["pdf_tool_message"] = profile.get("pdf_tool_message") is True
    return profile


def create_chat_model(
    config: AgentModelConfig,
    *,
    reasoning_enabled: bool | None = None,
    http_async_client: httpx.AsyncClient | None = None,
    timeout: float = 600,
    max_tokens: int | None = None,
    http_async_transport: httpx.AsyncBaseTransport | None = None,
    max_retries: int = 2,
) -> BaseChatModel:
    """按当前接口类型查表构建模型，异步客户端由调用方持有并关闭

    Args:
        config: 本次调用的模型连接与能力配置
        reasoning_enabled: 本次用途需要的推理开关，留空沿用配置
        http_async_client: 具有模型地址访问约束的调用方客户端
        http_async_transport: 应用拥有的异步连接池，Ollama 客户端只借用
        timeout: 单次网络请求超时秒数
        max_tokens: 本次输出令牌上限
        max_retries: 供应商客户端重试次数，零表示不重试

    Returns:
        可异步调用的聊天模型

    Raises:
        TypeError: 配置未创建受支持的聊天模型
    """
    model = _MODEL_PROVIDERS[config.provider](
        ChatModelOptions(
            config=config,
            reasoning_enabled=config.reasoning_enabled
            if reasoning_enabled is None
            else reasoning_enabled,
            http_async_client=http_async_client,
            timeout=timeout,
            max_tokens=max_tokens
            if max_tokens is not None
            else config.chat_options.max_tokens,
            http_async_transport=http_async_transport,
            max_retries=max_retries,
        )
    )
    model.profile = _input_profile(config, model.profile)
    return model
