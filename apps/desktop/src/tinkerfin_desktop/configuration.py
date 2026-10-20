"""原生宿主传入的授权配置，不将凭证序列化到磁盘"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Self

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from tinkerfin_sandbox import LocalAccess

DeviceId = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]
WorkspaceId = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
]


def server_url(value: str) -> str:
    """只向 HTTPS 或明确填写的本机开发地址发送设备凭证"""
    url = httpx.URL(value)
    if url.username or url.password or url.query or url.fragment or not url.host:
        raise ValueError("Studio 地址不能包含账户、查询参数或片段")
    if url.scheme != "https" and not (
        url.scheme == "http" and url.host in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ValueError("Studio 地址必须使用 HTTPS；HTTP 仅允许本机开发服务")
    return value.strip().rstrip("/")


class McpServerSelection(BaseModel):
    """用户在本机授权的 MCP 程序，模型只能选择标识和工具参数"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    server_id: WorkspaceId
    label: str = Field(min_length=1, max_length=128)
    command: tuple[Annotated[str, Field(min_length=1, max_length=4096)], ...] = Field(
        min_length=1,
        max_length=64,
        description="已授权的可执行程序及固定参数，不经过 shell 展开",
    )

    @field_validator("command")
    @classmethod
    def valid_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\0" in item for item in value):
            raise ValueError("MCP 命令不能包含空字节")
        return value


class WorkspacePermissions(BaseModel):
    """本机工具权限；项目目录和权限不接受云端调用修改"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    network: Literal["normal", "restricted"] = Field(
        description="正常联网或显式受限联网；仅受限模式使用域名列表"
    )
    allowed_domains: tuple[str, ...] = Field(
        default=(),
        max_length=128,
        description="受限模式允许的外网域名；受限模式空列表禁止外联",
    )
    browser: bool = False
    mcp_servers: tuple[McpServerSelection, ...] = Field(default=(), max_length=16)
    desktop: bool = Field(
        default=False, description="原生宿主单独授予的桌面操作权限，不从目录权限推断"
    )

    @model_validator(mode="after")
    def valid_network(self) -> Self:
        try:
            LocalAccess(network=self.network, allowed_domains=self.allowed_domains)
        except ValueError as error:
            raise ValueError(
                "正常联网无需域名列表；受限联网仅接受外网域名或子域通配符"
            ) from error
        return self

    @field_validator("mcp_servers")
    @classmethod
    def distinct_servers(
        cls, value: tuple[McpServerSelection, ...]
    ) -> tuple[McpServerSelection, ...]:
        if len({server.server_id for server in value}) != len(value):
            raise ValueError("MCP 服务标识重复")
        return value


class CompanionConfiguration(BaseModel):
    """当前设备的连接身份与本机授权，只从原生宿主独占管道接收"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    server: str
    device_id: DeviceId
    namespace: str = Field(pattern=r"^ns_[1-9][0-9]*$")
    credential: SecretStr = Field(
        exclude=True,
        description="Keychain 中的 256 位随机设备凭证，禁止写入配置文件或命令行",
    )
    workspace_root: Path
    permissions: WorkspacePermissions = Field(
        default_factory=lambda: WorkspacePermissions(network="normal")
    )

    _server = field_validator("server")(server_url)

    @field_validator("credential")
    @classmethod
    def credential_is_random_secret(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r"[0-9a-f]{64}", value.get_secret_value()) is None:
            raise ValueError("设备凭证格式无效")
        return value

    @field_validator("workspace_root")
    @classmethod
    def absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute() or value == Path("/") or ".." in value.parts:
            raise ValueError("项目根目录必须是明确的绝对目录")
        return value

    @property
    def authorization(self) -> str:
        """仅供受信任的云端通信客户端使用"""
        return f"Bearer {self.device_id}.{self.credential.get_secret_value()}"


class SkillReference(BaseModel):
    """会话授权的技能内容摘要，下载必须保持此身份"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    installation_id: str
    name: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected: bool


class ExecutionConfiguration(BaseModel):
    """设备执行授权对应的项目和只读技能，不包含模型或工具审批策略"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    authorization: DeviceId
    workspace_id: WorkspaceId
    skills: tuple[SkillReference, ...]


class PairedDevice(BaseModel):
    """云端确认的设备身份，首次配对与重连均须验证"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    device_id: DeviceId
    namespace: str = Field(pattern=r"^ns_[1-9][0-9]*$")
    name: str
