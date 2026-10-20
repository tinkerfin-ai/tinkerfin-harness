"""按提供方构建当前聊天模型集成"""

from dataclasses import dataclass
from typing import Literal, TypedDict

import httpx
from langchain.chat_models.base import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama

from tinkerfin_studio.models.schemas import AgentModelConfig


@dataclass(frozen=True, slots=True)
class ChatModelOptions:
    """一次模型构建所需的配置；客户端归调用方所有，适配器仅借用"""

    config: AgentModelConfig
    reasoning_enabled: bool
    http_async_client: httpx.AsyncClient | None
    timeout: float
    max_tokens: int | None
    http_async_transport: httpx.AsyncBaseTransport | None
    max_retries: int


def create_openai_model(options: ChatModelOptions) -> BaseChatModel:
    """构建 OpenAI 兼容接口，服务商差异通过模型名称和地址配置"""
    model = init_chat_model(
        options.config.model_name,
        model_provider="openai",
        reasoning_effort=options.config.chat_options.reasoning_effort,
        # 显式凭据来源和空认证头避免免密服务读取 SDK 的进程凭据
        api_key=options.config.api_key.get_secret_value,
        default_headers={"Authorization": ""}
        if not options.config.api_key.get_secret_value()
        else None,
        base_url=options.config.base_url,
        streaming=True,
        http_async_client=options.http_async_client,
        timeout=options.timeout,
        max_tokens=options.max_tokens,
        temperature=options.config.chat_options.temperature,
        top_p=options.config.chat_options.top_p,
        stop=options.config.chat_options.stop,
        use_responses_api=False,
        max_retries=options.max_retries,
    )
    if not isinstance(model, BaseChatModel):
        raise TypeError("模型配置未创建 BaseChatModel")
    return model


class _DeepSeekReasoning(TypedDict, total=False):
    reasoning_effort: Literal["low", "medium", "high"]


def create_deepseek_model(options: ChatModelOptions) -> BaseChatModel:
    """把 Studio 推理开关转换为 DeepSeek 支持的请求参数"""
    reasoning_options: _DeepSeekReasoning = (
        {"reasoning_effort": options.config.chat_options.reasoning_effort or "high"}
        if options.reasoning_enabled
        else {}
    )
    model = init_chat_model(
        options.config.model_name,
        model_provider="deepseek",
        api_key=options.config.api_key.get_secret_value(),
        base_url=options.config.base_url,
        streaming=True,
        http_async_client=options.http_async_client,
        timeout=options.timeout,
        max_tokens=options.max_tokens,
        temperature=options.config.chat_options.temperature,
        top_p=options.config.chat_options.top_p,
        stop=options.config.chat_options.stop,
        use_responses_api=False,
        max_retries=options.max_retries,
        extra_body={
            "thinking": {"type": "enabled" if options.reasoning_enabled else "disabled"}
        },
        **reasoning_options,
    )
    if not isinstance(model, BaseChatModel):
        raise TypeError("模型配置未创建 BaseChatModel")
    return model


def create_ollama_model(options: ChatModelOptions) -> BaseChatModel:
    """使用 Ollama 原生接口，借用应用拥有的传输和连接池

    SDK 的客户端不拥有独立网络连接池，应用关闭传输时统一释放连接。
    每次请求显式使用当前用户认证，避免 SDK 读取进程环境中的其他密钥。
    只使用异步模型调用，不在构建阶段探测或下载模型。
    """
    if options.http_async_transport is None:
        raise ValueError("Ollama 需要应用拥有的模型传输")
    key = options.config.api_key.get_secret_value()

    async def authenticate(request: httpx.Request) -> None:
        request.headers.pop("Authorization", None)
        if key:
            request.headers["Authorization"] = "Bearer " + key

    settings = options.config.chat_options
    return ChatOllama(
        model=options.config.model_name,
        base_url=options.config.base_url,
        reasoning=(settings.reasoning_effort or True)
        if options.reasoning_enabled
        else False,
        temperature=settings.temperature,
        top_p=settings.top_p,
        num_predict=options.max_tokens,
        num_ctx=settings.context_window,
        keep_alive=settings.keep_alive,
        stop=settings.stop,
        validate_model_on_init=False,
        async_client_kwargs={
            "transport": options.http_async_transport,
            "headers": {"Accept-Encoding": "identity"},
            "trust_env": False,
            "follow_redirects": False,
            "timeout": options.timeout,
            "event_hooks": {"request": [authenticate]},
        },
    )
