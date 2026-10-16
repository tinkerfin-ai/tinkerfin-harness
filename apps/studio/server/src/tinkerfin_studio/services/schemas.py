"""个人服务的请求边界与可安全返回的设置"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

from tinkerfin_studio.models.transport import validate_model_url

Capability = Literal["web_search", "image_generation"]
ImageFormat = Literal["png", "jpeg", "webp"]


def _bounded_json(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """限制用户提交的结构化参数，避免形成无界请求"""
    stack: list[tuple[dict[str, JsonValue] | list[JsonValue], int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > 32:
            raise ValueError("请求参数嵌套不能超过 32 层")
        if isinstance(current, dict) and any(
            key.casefold().replace("_", "").replace("-", "")
            in {"apikey", "authorization", "accesstoken"}
            for key in current
        ):
            raise ValueError("凭证须通过认证设置提供，不能写入请求参数")
        children = current.values() if isinstance(current, dict) else current
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 64 * 1024:
        raise ValueError("请求参数不能超过 64 KiB")
    return value


def _pointer(value: str) -> str:
    if value and not value.startswith("/"):
        raise ValueError("结果路径须为空或以 / 开始")
    if len(value) > 256:
        raise ValueError("结果路径不能超过 256 个字符")
    if re.search(r"~(?![01])", value):
        raise ValueError("结果路径包含无效的 JSON Pointer 转义")
    return value


def _endpoint(value: str) -> str:
    url = validate_model_url(value)
    if url.query or url.fragment:
        raise ValueError("服务地址不包含查询参数或片段；请在请求参数中填写")
    return str(url)


def _template_variables(value: JsonValue, allowed: set[str]) -> None:
    """保存时确认请求模板只使用本能力提供的结构化变量"""
    if isinstance(value, str) and "${" in value:
        if not (
            value.startswith("${") and value.endswith("}") and value[2:-1] in allowed
        ):
            raise ValueError("请求参数包含不支持的变量或拼接写法")
    elif isinstance(value, dict):
        for child in value.values():
            _template_variables(child, allowed)
    elif isinstance(value, list):
        for child in value:
            _template_variables(child, allowed)


class HttpRequestConfig(BaseModel):
    """自定义同步 HTTP 接口的认证、请求参数与结果提取声明"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["GET", "POST"] = "POST"
    auth: Literal["bearer", "header", "none"] = "bearer"
    header: str = Field(default="Authorization", min_length=1, max_length=128)
    prefix: str = Field(default="Bearer ", max_length=128)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    items_pointer: str = ""
    value_pointer: str = ""
    title_pointer: str = ""
    url_pointer: str = ""
    score_pointer: str = ""
    response_type: Literal["url", "base64", "binary"] = "url"

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _bounded_json(value)

    @model_validator(mode="after")
    def validate_query(self) -> HttpRequestConfig:
        if self.method == "GET" and any(
            isinstance(value, (dict, list)) for value in self.parameters.values()
        ):
            raise ValueError("GET 查询参数只支持标量")
        return self

    @field_validator(
        "items_pointer",
        "value_pointer",
        "title_pointer",
        "url_pointer",
        "score_pointer",
    )
    @classmethod
    def validate_pointer(cls, value: str) -> str:
        return _pointer(value)

    @field_validator("header")
    @classmethod
    def validate_header(cls, value: str) -> str:
        if not value.isascii() or not all(
            char.isalnum() or char == "-" for char in value
        ):
            raise ValueError("认证头名称只允许 ASCII 字母、数字和连字符")
        if value.casefold() in {
            "host",
            "content-length",
            "transfer-encoding",
            "cookie",
            "accept-encoding",
        }:
            raise ValueError("不能覆盖请求传输头")
        return value

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("凭证前缀不能包含换行")
        return value


class SearchConfig(BaseModel):
    """网页搜索参数；供应商预设与自定义请求共用同一结果类型"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: Literal["web_search"] = "web_search"
    provider_id: Literal["tavily", "custom"] = "tavily"
    endpoint: str = Field(
        default="https://api.tavily.com", min_length=1, max_length=1024
    )
    depth: Literal["basic", "advanced"] = "basic"
    max_results: int = Field(default=5, ge=1, le=10)
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    request: HttpRequestConfig | None = None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _endpoint(value)

    @field_validator("extra")
    @classmethod
    def validate_extra(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if {key.casefold() for key in value} & {
            "query",
            "max_results",
            "search_depth",
            "api_key",
        }:
            raise ValueError("附加参数不能覆盖搜索词、数量、检索方式或认证")
        return _bounded_json(value)

    @model_validator(mode="after")
    def validate_provider(self) -> SearchConfig:
        if (self.provider_id == "custom") != (self.request is not None):
            raise ValueError("自定义搜索须填写请求与结果规则，预设不接受自定义规则")
        if (
            self.provider_id == "custom"
            and self.request is not None
            and not all(
                (
                    self.request.items_pointer,
                    self.request.title_pointer,
                    self.request.url_pointer,
                )
            )
        ):
            raise ValueError("自定义搜索须指定结果列表、标题和来源路径")
        if self.provider_id == "custom" and self.request is not None:
            _template_variables(
                self.request.parameters, {"query", "max_results", "topic"}
            )
            if self.extra:
                raise ValueError("自定义搜索请在请求参数中填写附加字段")
        return self


class ImageConfig(BaseModel):
    """专用图片生成参数；导出格式由应用处理，不发送给供应商"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: Literal["image_generation"] = "image_generation"
    provider_id: Literal["openai", "fal", "custom"] = "openai"
    endpoint: str = Field(
        default="https://api.openai.com/v1", min_length=1, max_length=1024
    )
    model: str = Field(default="", max_length=128)
    size: str = Field(default="", max_length=64)
    output_formats: list[ImageFormat] = Field(default_factory=list, max_length=3)
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    request: HttpRequestConfig | None = None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _endpoint(value)

    @field_validator("output_formats")
    @classmethod
    def validate_formats(cls, value: list[ImageFormat]) -> list[ImageFormat]:
        if len(set(value)) != len(value):
            raise ValueError("输出格式不能重复")
        return value

    @field_validator("extra")
    @classmethod
    def validate_extra(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if {key.casefold() for key in value} & {
            "model",
            "prompt",
            "n",
            "num_images",
            "api_key",
            "authorization",
            "output_formats",
        }:
            raise ValueError("附加参数不能覆盖模型、描述、数量、认证或导出格式")
        return _bounded_json(value)

    @model_validator(mode="after")
    def validate_provider(self) -> ImageConfig:
        if (self.provider_id == "custom") != (self.request is not None):
            raise ValueError("自定义生图须填写请求与结果规则，预设不接受自定义规则")
        if self.provider_id == "openai" and not self.model.strip():
            raise ValueError("OpenAI 兼容生图须填写模型 ID")
        if (
            self.provider_id == "custom"
            and self.request is not None
            and self.request.response_type != "binary"
            and not self.request.value_pointer
        ):
            raise ValueError("自定义生图须指定图片结果路径")
        if self.provider_id == "custom" and self.request is not None:
            _template_variables(self.request.parameters, {"prompt"})
            if self.extra:
                raise ValueError("自定义生图请在请求参数中填写附加字段")
        return self


ServiceConfiguration = Annotated[
    SearchConfig | ImageConfig, Field(discriminator="capability")
]


class ServiceSave(BaseModel):
    """保存个人服务；密钥为空值时仅在认证目标不变时保留已存密钥"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    configuration: ServiceConfiguration
    enabled: bool = True
    api_key: SecretStr | None = Field(
        default=None, description="新密钥；null 表示尝试保留同认证目标已有密钥"
    )


class ServiceSettings(BaseModel):
    """服务设置响应，只公开是否已有凭证"""

    id: str
    configuration: ServiceConfiguration
    enabled: bool
    has_key: bool
    test_status: Literal["success", "failed"] | None = None
    test_code: str | None = None
    tested_at: str | None = None


class ServiceTestResult(BaseModel):
    """主动测试的安全结果，不含供应商正文与凭证"""

    outcome: Literal["success", "failed"]
    code: str


class ServiceBinding(BaseModel):
    """运行引用个人服务的稳定身份与执行配置摘要，不保存凭证"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ServiceBindings(BaseModel):
    """每次运行明确固定搜索和生图服务，空值表示当时未配置"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    web_search: ServiceBinding | None
    image_generation: ServiceBinding | None

    @classmethod
    def empty(cls) -> ServiceBindings:
        return cls(web_search=None, image_generation=None)
