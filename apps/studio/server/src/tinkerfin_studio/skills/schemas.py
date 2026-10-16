"""技能目录、安装、导入和持久快照的数据边界"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SkillSourceKind = Literal["catalog", "github", "zip"]


class SkillSourceInfo(BaseModel):
    """服务端注册的来源，不包含认证配置"""

    id: str
    name: str
    url: str


class RemoteSkill(BaseModel):
    """第三方目录实际返回的技能信息"""

    id: str = Field(description="来源内包含发布者身份的稳定标识")
    source_id: str
    name: str
    description: str
    revision: str | None = Field(default=None, description="第三方发行内容标识")
    author: str | None = None
    topics: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class RemoteSkillPage(BaseModel):
    items: list[RemoteSkill]
    cursor: str | None = None


class SkillDetail(BaseModel):
    """详情只展示说明和目录，不承载安装管理状态"""

    name: str
    description: str
    markdown: str
    files: list[str] = Field(default_factory=list)
    author: str | None = None
    source_url: str | None = None
    topics: list[str] = Field(default_factory=list)


class InstalledSkill(BaseModel):
    id: str
    project_id: str | None
    overridden: bool = False
    name: str
    description: str
    source_kind: SkillSourceKind
    source_id: str | None = None
    source_name: str
    external_id: str | None = None
    enabled: bool
    author: str | None = None
    topics: list[str]
    file_count: int
    byte_size: int
    created_at: datetime
    updated_at: datetime


class RemoteSkillDetail(BaseModel):
    """详情携带明确来源与可安装的发行标识"""

    skill: RemoteSkill
    detail: SkillDetail


class SkillCommandRequest(BaseModel):
    """客户端重试同一次管理操作时保留的身份"""

    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    project_id: str | None = Field(default=None, min_length=1, max_length=36)


class InstallSkillRequest(SkillCommandRequest):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=64)
    skill_id: str = Field(min_length=1, max_length=256)
    revision: str = Field(
        min_length=1, max_length=128, description="详情或卡片返回的固定来源发行标识"
    )


class SkillEnabledRequest(SkillCommandRequest):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)


class GitHubImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)


class ImportCandidate(BaseModel):
    digest: str
    name: str
    description: str
    file_count: int
    byte_size: int


class ImportPreview(BaseModel):
    id: str
    source: Literal["github", "zip"]
    candidates: list[ImportCandidate]


class ConfirmImportRequest(SkillCommandRequest):
    model_config = ConfigDict(extra="forbid")
    digests: list[str] = Field(min_length=1, max_length=64)


class SkillReplacement(BaseModel):
    """更新时选定的不可变 ZIP 预览内容"""

    model_config = ConfigDict(extra="forbid")
    draft_id: str = Field(
        min_length=1, max_length=36, description="ZIP 预览返回的草稿 ID"
    )
    digest: str = Field(
        pattern=r"^[a-f0-9]{64}$", description="预览中与原技能同名的候选内容摘要"
    )


class UpdateSkillRequest(SkillCommandRequest):
    replacement: SkillReplacement | None = None


class SkillChangeResult(BaseModel):
    """已提交的安装信息，内容相同时 changed 为假"""

    installation: InstalledSkill
    changed: bool


class SkillRemovalResult(BaseModel):
    installation_id: str


class SkillImportResult(BaseModel):
    installation_ids: list[str]


class CatalogSkillTarget(BaseModel):
    """来源目录中的固定发行技能"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["catalog"] = Field(description="从已注册来源安装固定发行")
    source_id: str = Field(
        min_length=1, max_length=64, description="来源列表返回的来源 ID"
    )
    skill_id: str = Field(
        min_length=1, max_length=256, description="搜索或详情返回的来源内技能标识"
    )
    revision: str = Field(
        min_length=1, max_length=128, description="搜索或详情返回的固定发行标识"
    )


class ImportedSkillTarget(BaseModel):
    """用户已预览并明确选定的导入条目"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["import"] = Field(description="安装已预览的 GitHub 或 ZIP 候选技能")
    draft_id: str = Field(
        min_length=1, max_length=36, description="preview_skills 返回的草稿 ID"
    )
    digests: list[str] = Field(
        min_length=1, max_length=64, description="该预览中用户选定的候选内容摘要"
    )


class InstallSkillOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["install"] = Field(description="将用户选定的技能安装到个人库")
    target: Annotated[
        CatalogSkillTarget | ImportedSkillTarget,
        Field(discriminator="kind", description="固定来源发行或已预览的导入内容"),
    ]


class UpdateSkillOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["update"] = Field(description="更新内容并保留安装身份及启用状态")
    installation_id: str = Field(
        min_length=1, max_length=36, description="list_skills 返回的个人安装 ID"
    )
    replacement: SkillReplacement | None = Field(
        default=None, description="仅 ZIP 安装需要同名替换预览，其他来源不传"
    )


class UninstallSkillOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["uninstall"] = Field(
        description="移除个人安装，后续执行前同步工作区"
    )
    installation_id: str = Field(
        min_length=1, max_length=36, description="list_skills 返回的个人安装 ID"
    )


SkillOperation = Annotated[
    InstallSkillOperation | UpdateSkillOperation | UninstallSkillOperation,
    Field(discriminator="action"),
]


class SkillReference(BaseModel):
    """运行开始时的技能选择记录，不决定执行目录内容"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    installation_id: str
    name: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected: bool


class SelectedSkill(BaseModel):
    """用户在某次运行中明确选定的技能身份"""

    id: str
    name: str


class SkillSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    skills: tuple[SkillReference, ...]
