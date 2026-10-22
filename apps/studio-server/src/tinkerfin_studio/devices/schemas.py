"""设备授权与本机权限展示的 HTTP 边界"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
)

from tinkerfin_studio.skills.schemas import SkillReference

DeviceId = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]
WorkspaceId = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
]


def _secret(value: SecretStr) -> SecretStr:
    import re

    if re.fullmatch(r"[0-9a-f]{64}", value.get_secret_value()) is None:
        raise ValueError("授权能力必须是 256 位随机值的十六进制表示")
    return value


AuthorizationSecret = Annotated[SecretStr, AfterValidator(_secret)]
AuthorizationState = Literal[
    "waiting", "authorized", "expired", "cancelled", "superseded"
]


class AuthorizationStart(BaseModel):
    """本机先保存 Keychain 凭证，再请求浏览器授权入口"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$")
    credential: AuthorizationSecret = Field(
        description="本机预存的 256 位凭证，不提供给浏览器"
    )


class AuthorizationCreated(BaseModel):
    """发给本机的浏览器入口，浏览器能力不具备设备查询权限"""

    authorization_id: DeviceId
    browser_token: str = Field(
        description="仅放在浏览器 URL fragment 的 256 位审批能力"
    )
    expires_at: datetime


class AuthorizationProof(BaseModel):
    """客户端凭 Keychain 凭证查询或取消自己的申请"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    credential: AuthorizationSecret


class BrowserAuthorizationProof(BaseModel):
    """已登录浏览器持有的审批能力"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    browser_token: AuthorizationSecret


class AuthorizationApproval(BrowserAuthorizationProof):
    """把用户实际看到的账号与当前登录身份绑定"""

    expected_user_id: int = Field(
        gt=0, description="批准页面显示的账号 ID，必须等于当前认证身份"
    )


class PairedDevice(BaseModel):
    """设备身份与运行范围，凭证仍由本机自己保管"""

    device_id: DeviceId
    namespace: str
    name: str


class AuthorizationStatus(BaseModel):
    """仅向持有客户端凭证的一方返回有效设备身份"""

    authorization_id: DeviceId
    state: AuthorizationState
    device: PairedDevice | None = None


class AuthorizationPreview(BaseModel):
    """浏览器批准前可见的设备名称和真实申请状态"""

    authorization_id: DeviceId
    name: str
    expires_at: datetime
    state: AuthorizationState


class DeviceName(BaseModel):
    """当前设备修改自己的显示名称"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$")


class DeviceCapabilities(BaseModel):
    """当前实际可用的本机能力，不包含目录路径或工具凭证"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    browser: bool = False
    mcp: bool = False
    desktop: bool = False
    desktop_capture: bool = False
    open_url: bool = False


class DevicePresence(BaseModel):
    """本机握手时同步实际可用能力"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)


class ConnectedDevice(PairedDevice):
    """已认证连接的身份，后续能力报告只能更新同一连接"""

    connection_id: DeviceId


class DeviceCapabilityUpdate(BaseModel):
    """当前连接的完整能力快照，不变更连接身份或任务授权"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    connection_id: DeviceId
    capabilities: DeviceCapabilities


class DeviceView(BaseModel):
    """当前用户可见的设备、在线状态与授权工作区"""

    device_id: DeviceId
    name: str
    online: bool
    revoked: bool
    paired_at: datetime
    last_seen_at: datetime | None
    capabilities: DeviceCapabilities


class DeviceProject(BaseModel):
    """设备可自动准备的项目身份，显示名称不参与目录定位"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str
    name: str


class DeviceProjectPage(BaseModel):
    """按项目身份分页，不随显示名称变化改变顺序"""

    account_name: str
    items: tuple[DeviceProject, ...]
    next_cursor: str | None


class LocalExecutionConfiguration(BaseModel):
    """云端运行获准使用的本机工作区与技能快照"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    authorization: DeviceId = Field(description="本次执行绑定的工作区授权标识")
    workspace_id: WorkspaceId
    skills: tuple[SkillReference, ...]


class LocalSkillFile(BaseModel):
    """技能文件的相对路径与原始字节，内容使用 base64 编码"""

    path: str = Field(description="相对技能根目录的文件路径，不包含链接或路径穿越")
    content: str = Field(description="原文件字节的 base64 编码")


class LocalSkillPackage(BaseModel):
    """执行授权已固定的技能内容，不根据本机请求扩展技能范围"""

    name: str
    digest: str
    files: tuple[LocalSkillFile, ...]


class LocalFileContent(BaseModel):
    """有权读取的附件内容，mime_type 描述实际返回字节"""

    mime_type: str
    content: str = Field(description="原始文件内容的 base64 字节")
