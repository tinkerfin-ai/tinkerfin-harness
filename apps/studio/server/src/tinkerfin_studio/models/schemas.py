"""模型管理、目录与运行时边界模型"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

ModelId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    ),
]


ModelProvider = Literal["openai", "deepseek", "ollama"]
ModelAPI = Literal["openai_chat_completions", "ollama"]


class ChatOptions(BaseModel):
    """聊天生成参数；空值表示不向服务覆盖该参数"""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, gt=0, description="单次最大输出令牌数")
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: list[str] | None = Field(
        default=None, max_length=4, description="停止生成的字符串，最多四项"
    )
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    context_window: int | None = Field(
        default=None, gt=0, description="Ollama 单次请求的上下文令牌容量"
    )
    keep_alive: int | None = Field(
        default=None,
        ge=0,
        le=86400,
        description="Ollama 模型在请求结束后保持加载的秒数",
    )


class ModelConnectionSave(BaseModel):
    """保存本人的提供方连接；密钥为 null 时保留同地址已有密钥"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    connection_id: ModelId
    display_name: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(
        min_length=1, max_length=64, description="提供方目录标识，自定义连接使用 custom"
    )
    api_type: ModelAPI = Field(description="服务采用的模型接口")
    base_url: str = Field(
        min_length=1,
        max_length=1024,
        description="模型 API 基础地址，Ollama 使用服务根地址",
    )
    auth_type: Literal["api_key", "none"] = "api_key"
    api_key: SecretStr | None = Field(
        default=None, description="新密钥；null 保留已有密钥，无需认证时清除密钥"
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return str(TypeAdapter(AnyHttpUrl).validate_python(value))


class ModelConnectionSettings(BaseModel):
    """提供方设置，不包含密钥原文"""

    connection_id: str
    display_name: str
    provider_id: str
    api_type: ModelAPI
    base_url: str
    auth_type: Literal["api_key", "none"]
    has_key: bool


class ProviderPreset(BaseModel):
    """新建连接时提供的服务默认值"""

    provider_id: str
    display_name: str
    api_type: ModelAPI
    base_url: str
    auth_type: Literal["api_key", "none"] = "api_key"
    models: list[str] = Field(
        default_factory=list, description="推荐模型 ID，不代表当前账户的可用模型"
    )


class DiscoveredModel(BaseModel):
    """模型服务实际返回的候选项；未返回的能力保持未知"""

    model_name: str = Field(min_length=1, max_length=128)
    display_name: str
    image_support: Literal["supported", "unsupported", "unknown"] = "unknown"


class ModelDiscoveryResult(BaseModel):
    """一次模型发现的结果，不保存或启用模型"""

    outcome: Literal["success", "inconclusive", "failed"]
    items: list[DiscoveredModel] = Field(default_factory=list)
    code: str


class AgentModelWrite(BaseModel):
    """受控模型配置写入使用的边界数据"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generation_options: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="生图接口附加参数，不得覆盖 model、prompt 或 n",
    )
    purpose: Literal["chat", "image"] = "chat"
    model_id: ModelId = Field(description="前后端使用的稳定模型 ID")
    display_name: str = Field(min_length=1, max_length=128, description="前端展示名称")
    connection_id: ModelId = Field(description="所属提供方连接 ID")
    chat_options: ChatOptions = Field(default_factory=ChatOptions)
    model_name: str = Field(
        min_length=1, max_length=128, description="供应商实际模型名称"
    )
    image_support: Literal["supported", "unsupported", "unknown"] = Field(
        default="unknown", description="已确认的图片输入能力，未知时禁止发送新图片"
    )
    reasoning_enabled: bool = Field(
        default=False, description="是否启用 provider reasoning 参数"
    )
    enabled: bool = Field(default=True, description="是否允许创建新 run")
    is_default: bool = Field(default=False, description="是否设为唯一默认模型")
    sort_order: int = Field(default=0, description="模型目录升序排序值")

    @field_validator("generation_options")
    @classmethod
    def validate_generation_options(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """保存和测试共享附加参数的大小、数值及受控字段约束"""
        if {key.casefold() for key in value}.intersection(
            {"model", "prompt", "n", "api_key", "authorization"}
        ):
            raise ValueError("附加参数不能覆盖模型、提示词、数量或认证字段")
        for key in ("size", "output_format"):
            option = value.get(key)
            if key in value and (not isinstance(option, str) or not option.strip()):
                raise ValueError("尺寸和输出格式必须是非空字符串")
        containers: list[tuple[dict[str, JsonValue] | list[JsonValue], int]] = [
            (value, 1)
        ]
        while containers:
            current, depth = containers.pop()
            if depth > 64:
                raise ValueError("附加参数嵌套不能超过 64 层")
            children = current.values() if isinstance(current, dict) else current
            for child in children:
                if isinstance(child, (dict, list)):
                    containers.append((child, depth + 1))
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise ValueError("附加参数必须是有效 JSON，不允许非有限数值") from error
        if len(encoded) > 64 * 1024:
            raise ValueError("附加参数不能超过 64 KiB")
        return value

    @model_validator(mode="after")
    def validate_default_is_enabled(self) -> AgentModelWrite:
        if self.is_default and not self.enabled:
            raise ValueError("默认模型必须处于启用状态")
        return self


class AgentModelCatalogItem(BaseModel):
    """返回前端的安全模型目录项"""

    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(alias="modelId", description="稳定模型 ID")
    display_name: str = Field(alias="displayName", description="前端展示名称")
    image_support: Literal["supported", "unsupported", "unknown"] = Field(
        default="unknown", alias="imageSupport", description="图片输入能力"
    )
    reasoning_enabled: bool = Field(
        alias="reasoningEnabled", description="是否启用 reasoning"
    )
    is_default: bool = Field(alias="isDefault", description="是否为默认模型")


class AgentModelCatalog(BaseModel):
    """前端模型目录响应"""

    model_config = ConfigDict(populate_by_name=True)

    items: list[AgentModelCatalogItem] = Field(description="启用模型列表")
    default_model_id: str | None = Field(
        default=None, alias="defaultModelId", description="默认模型 ID"
    )


class AgentModelConfig(BaseModel):
    """当前请求使用的完整模型连接配置"""

    model_config = ConfigDict(frozen=True)

    model_id: str
    display_name: str
    provider: ModelProvider
    model_name: str
    base_url: str
    api_key: SecretStr
    reasoning_enabled: bool
    chat_options: ChatOptions = Field(default_factory=ChatOptions)
    generation_options: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="生图接口附加参数，不得覆盖 model、prompt 或 n",
    )
    purpose: Literal["chat", "image"] = "chat"
    image_support: Literal["supported", "unsupported", "unknown"] = "unknown"


class AgentModelSettings(AgentModelWrite):
    """当前用户保存的模型设置，连接与密钥单独管理"""


class AgentModelSave(AgentModelWrite):
    """设置页提交的模型配置，引用本人已保存的连接"""


class ModelSettingsOverview(BaseModel):
    """模型设置页的连接、模型和内置提供方目录"""

    models: list[AgentModelSettings]
    connections: list[ModelConnectionSettings]
    providers: list[ProviderPreset]


ModelTestKind = Literal["basic", "text", "vision", "image"]
ModelTestCode = Literal[
    "model_listed",
    "models_unavailable",
    "model_not_listed",
    "text_received",
    "vision_response_received",
    "image_received",
    "empty_response",
    "invalid_response",
    "response_too_large",
    "timeout",
    "authentication_failed",
    "rate_limited",
    "service_error",
    "network_error",
    "endpoint_not_allowed",
    "invalid_configuration",
]


class ModelTestRequest(BaseModel):
    """测试当前表单草稿，不保存配置或改变模型能力标记"""

    model_config = ConfigDict(extra="forbid")
    kind: ModelTestKind
    configuration: AgentModelSave

    @model_validator(mode="after")
    def validate_test_purpose(self) -> ModelTestRequest:
        if self.kind == "image" and self.configuration.purpose != "image":
            raise ValueError("生图测试需要生图服务配置")
        if self.kind in {"text", "vision"} and self.configuration.purpose != "chat":
            raise ValueError("文字和看图测试需要聊天模型配置")
        return self


class ModelTestImage(BaseModel):
    """测试输入或生图结果的有界内联预览，不创建附件"""

    mime_type: Literal["image/png", "image/jpeg"]
    data_base64: str = Field(
        max_length=350_000, description="不超过 256 KiB 的图片字节的 Base64 编码"
    )


class ModelTestResult(BaseModel):
    """一次独立测试的结果；未确认不代表模型不可用"""

    kind: ModelTestKind
    outcome: Literal["success", "failed", "inconclusive"]
    elapsed_ms: int = Field(ge=0, description="本次测试耗时，单位毫秒")
    code: ModelTestCode
    text: str | None = Field(
        default=None, max_length=2000, description="文字或看图测试的有界模型回复"
    )
    image: ModelTestImage | None = None
