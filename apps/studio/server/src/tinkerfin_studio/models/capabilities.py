"""设置页和模型运行共同使用的原生图片输入判定"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Literal

import httpx

from tinkerfin_studio.models.capability_data import ImageSupport, read_catalog

if TYPE_CHECKING:
    from tinkerfin_studio.models.schemas import ModelAPI

# 随应用导入一次；缺失或损坏的分发资料阻止启动，不在请求中同步读盘
_CATALOG = read_catalog(
    files("tinkerfin_studio.models").joinpath("capability_catalog.json").read_bytes()
)

# 受信任的精确服务端点
_PROVIDERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "openai": ("openai", ("https://api.openai.com/v1",)),
    "deepseek": (
        "deepseek",
        ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
    ),
    "gemini": ("google", ("https://generativelanguage.googleapis.com/v1beta/openai",)),
    "dashscope": ("alibaba-cn", ("https://dashscope.aliyuncs.com/compatible-mode/v1",)),
    "moonshot": ("moonshotai-cn", ("https://api.moonshot.cn/v1",)),
    "zhipu": ("zhipuai", ("https://open.bigmodel.cn/api/paas/v4",)),
    "siliconflow": ("siliconflow-cn", ("https://api.siliconflow.cn/v1",)),
    "volcengine": ("volcengine", ("https://ark.cn-beijing.volces.com/api/v3",)),
    "xai": ("xai", ("https://api.x.ai/v1",)),
    "openrouter": ("openrouter", ("https://openrouter.ai/api/v1",)),
    "groq": ("groq", ("https://api.groq.com/openai/v1",)),
    "mistral": ("mistral", ("https://api.mistral.ai/v1",)),
    "together": ("togetherai", ("https://api.together.xyz/v1",)),
    "fireworks": ("fireworks-ai", ("https://api.fireworks.ai/inference/v1",)),
}


@dataclass(frozen=True, slots=True)
class ImageInputCapability:
    """自动资料和人工声明合并后的结果，source 表示 effective 的依据"""

    automatic: ImageSupport
    effective: ImageSupport
    source: Literal["catalog", "manual", "unknown"]


def resolve_image_input(
    *,
    provider_id: str,
    api_type: ModelAPI,
    base_url: str,
    model_name: str,
    image_support: ImageSupport = "unknown",
) -> ImageInputCapability:
    """核对本人连接与精确型号，不联网、不保存、不验证账户调用能力

    Args:
        provider_id: 已授权连接的提供方标识
        api_type: 实际连接使用的接口
        base_url: 实际请求端点，网关不能借用官方同名模型资料
        model_name: 供应商精确模型 ID
        image_support: unknown 表示自动，其余为用户人工指定

    Returns:
        人工声明优先的图片输入三态结果；缺少可信资料保持未知
    """
    automatic: ImageSupport = "unknown"
    mapping = _PROVIDERS.get(provider_id)
    if mapping is not None and api_type == "openai_chat_completions":
        catalog_provider, endpoints = mapping
        endpoint = httpx.URL(base_url)
        if not endpoint.userinfo and not endpoint.query and not endpoint.fragment:
            normalized = str(endpoint).removesuffix("/")
            if normalized in endpoints:
                automatic = _CATALOG.providers.get(catalog_provider, {}).get(
                    model_name, "unknown"
                )
    manual = image_support != "unknown"
    return ImageInputCapability(
        automatic=automatic,
        effective=image_support if manual else automatic,
        source="manual"
        if manual
        else "unknown"
        if automatic == "unknown"
        else "catalog",
    )
