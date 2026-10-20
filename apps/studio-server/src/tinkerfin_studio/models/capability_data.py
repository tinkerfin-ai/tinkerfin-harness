"""离线模型图片输入资料的校验与确定性序列化"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

ImageSupport = Literal["supported", "unsupported", "unknown"]
SOURCE_URL = "https://models.dev/api.json"
MAX_SOURCE_BYTES = 20 * 1024 * 1024


class CapabilityCatalog(BaseModel):
    """随应用分发的当前资料；校验和用于检查来源及规范数据完整性"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    source: Literal["https://models.dev/api.json"]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    providers: dict[str, dict[str, ImageSupport]]


class _Modalities(BaseModel):
    model_config = ConfigDict(strict=True)
    input: list[Literal["text", "image", "audio", "video", "pdf"]] | None = None


class _SourceModel(BaseModel):
    model_config = ConfigDict(strict=True)
    modalities: _Modalities | None = None


class _SourceProvider(BaseModel):
    model_config = ConfigDict(strict=True)
    models: dict[str, _SourceModel]


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"模型资料包含重复键：{key}")
        result[key] = value
    return result


def canonical_json(value: JsonValue) -> bytes:
    """返回可重复审查及计算摘要的 UTF-8 数据"""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def parse_source(data: bytes) -> CapabilityCatalog:
    """校验上游资料，只保留图片输入结论，缺失资料仍为未知

    Args:
        data: 有界的上游 JSON 原始内容

    Returns:
        带来源和内容摘要的单一当前目录

    Raises:
        ValueError: 内容过大、重复键、空目录或所消费字段不合法
    """
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise ValueError("模型资料不能为空或超过 20 MiB")
    source = TypeAdapter(dict[str, _SourceProvider]).validate_python(
        json.loads(data, object_pairs_hook=_unique_object), strict=True
    )
    providers: dict[str, dict[str, ImageSupport]] = {}
    for provider_id, provider in source.items():
        if not provider_id:
            raise ValueError("提供方标识不能为空")
        models: dict[str, ImageSupport] = {}
        for model_id, model in provider.models.items():
            if not model_id:
                raise ValueError("模型标识不能为空")
            inputs = model.modalities.input if model.modalities is not None else None
            models[model_id] = (
                "unknown"
                if not inputs
                else "supported"
                if "image" in inputs
                else "unsupported"
            )
        providers[provider_id] = models
    if not providers or not any(providers.values()):
        raise ValueError("模型资料不包含任何模型")
    content = TypeAdapter(dict[str, dict[str, ImageSupport]]).dump_python(
        providers, mode="json"
    )
    return CapabilityCatalog(
        source=SOURCE_URL,
        source_sha256=hashlib.sha256(data).hexdigest(),
        content_sha256=hashlib.sha256(canonical_json(content)).hexdigest(),
        providers=providers,
    )


def read_catalog(data: bytes) -> CapabilityCatalog:
    """校验应用资料，损坏时显式失败，不冒充所有模型均未知"""
    catalog = CapabilityCatalog.model_validate(
        json.loads(data, object_pairs_hook=_unique_object)
    )
    content = catalog.model_dump(mode="json")["providers"]
    if hashlib.sha256(canonical_json(content)).hexdigest() != catalog.content_sha256:
        raise ValueError("模型能力目录校验和不匹配")
    if not catalog.providers or not any(catalog.providers.values()):
        raise ValueError("模型能力目录为空")
    return catalog
