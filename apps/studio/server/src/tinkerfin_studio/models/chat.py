"""会话和配置测试共用的聊天模型构建"""

from collections.abc import Callable, Mapping
from typing import Literal

import httpx
from langchain_core.language_models import BaseChatModel

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
    # 将用户声明的图片能力提供给文件读取工具，避免向纯文本模型传入图片
    model.profile = {
        **(model.profile or {}),
        "image_inputs": config.image_support == "supported",
    }
    return model
