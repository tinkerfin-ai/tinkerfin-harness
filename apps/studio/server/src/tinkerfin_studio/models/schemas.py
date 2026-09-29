"""模型管理、目录与运行时边界模型"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from tinkerfin_studio.models.capabilities import ImageInputCapability
from tinkerfin_studio.models.capability_data import ImageSupport

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
ModelDiscoveryFailureCode = Literal[
    "response_too_large",
    "timeout",
    "authentication_failed",
    "rate_limited",
    "service_error",
    "network_error",
    "invalid_response",
]
ModelDiscoveryCode = Literal[
    "models_received", "models_unavailable", ModelDiscoveryFailureCode
]


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
    """模型服务实际返回的候选名称，不代表已验证的输入能力"""

    model_name: str = Field(min_length=1, max_length=128)
    display_name: str
    image_input_capability: ImageInputCapability = Field(
        default_factory=lambda: ImageInputCapability("unknown", "unknown", "unknown"),
        description="从可信目录补充的图片输入能力，不代表账户调用验证",
    )


class InputCapabilityRequest(BaseModel):
    """预览本人已保存连接的模型能力，不修改配置或发起推理"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    connection_id: ModelId
    model_name: str = Field(min_length=1, max_length=128)
    image_support: ImageSupport = "unknown"


class InputCapabilityResult(BaseModel):
    """模型编辑时的只读图片输入能力"""

    image_input_capability: ImageInputCapability


class ModelDiscoveryResult(BaseModel):
    """一次模型发现的结果，不保存或启用模型"""

    outcome: Literal["success", "inconclusive", "failed"]
    items: list[DiscoveredModel] = Field(default_factory=list)
    code: ModelDiscoveryCode


class AgentModelWrite(BaseModel):
    """受控模型配置写入使用的边界数据"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: ModelId = Field(description="前后端使用的稳定模型 ID")
    display_name: str = Field(
        min_length=1, max_length=40, description="前端展示名称，最多 40 个字符"
    )
    connection_id: ModelId = Field(description="所属提供方连接 ID")
    chat_options: ChatOptions = Field(default_factory=ChatOptions)
    model_name: str = Field(
        min_length=1, max_length=128, description="供应商实际模型名称"
    )
    image_support: Literal["supported", "unsupported", "unknown"] = Field(
        default="unknown",
        description="原生图片输入的人工声明；unknown 使用可信模型资料，不限制附件提交",
    )
    reasoning_enabled: bool = Field(
        default=False, description="是否启用 provider reasoning 参数"
    )
    enabled: bool = Field(default=True, description="是否允许创建新 run")
    is_default: bool = Field(default=False, description="是否设为唯一默认模型")
    sort_order: int = Field(default=0, description="模型目录升序排序值")

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
    connection_id: str = Field(
        alias="connectionId", description="所属提供方的稳定连接 ID"
    )
    connection_display_name: str = Field(
        alias="connectionDisplayName", description="模型配置页中的提供方名称"
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
    provider_id: str = Field(
        default="custom", description="已授权连接的提供方标识，用于核验模型资料来源"
    )
    model_name: str
    base_url: str
    api_key: SecretStr
    reasoning_enabled: bool
    chat_options: ChatOptions = Field(default_factory=ChatOptions)
    image_support: Literal["supported", "unsupported", "unknown"] = "unknown"


class AgentModelSettings(AgentModelWrite):
    """当前用户保存的模型设置，连接与密钥单独管理"""

    image_input_capability: ImageInputCapability = Field(
        description="对话模型的只读图片输入判定"
    )


class AgentModelSave(AgentModelWrite):
    """设置页提交的模型配置，引用本人已保存的连接"""


class ModelSettingsOverview(BaseModel):
    """模型设置页的连接、模型和内置提供方目录"""

    models: list[AgentModelSettings]
    connections: list[ModelConnectionSettings]
    providers: list[ProviderPreset]
