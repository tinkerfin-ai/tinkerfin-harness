"""服务端技能来源注册与 ClawHub 的最小协议边界"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.downloads import GitHubSkillImporter, download
from tinkerfin_studio.skills.packages import SkillArchiveReader, SkillPackage
from tinkerfin_studio.skills.schemas import (
    RemoteSkill,
    RemoteSkillPage,
    SkillDetail,
    SkillSourceInfo,
)


class SkillSourceSettings(BaseModel):
    """部署时配置来源实例；新增协议需要对应的来源实现"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["clawhub", "github"] = "clawhub"
    id: str = Field(
        default="clawhub", min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$"
    )
    name: str = Field(default="ClawHub", min_length=1, max_length=128)
    url: str = "https://clawhub.ai"
    token: SecretStr | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def github_repository(self) -> "SkillSourceSettings":
        if self.kind == "github":
            parsed = urlsplit(self.url)
            if parsed.scheme != "https" or parsed.netloc != "github.com":
                raise ValueError("GitHub 来源必须指向公开 GitHub 仓库")
            GitHubSkillImporter._validate_repository(parsed.path.strip("/"))
        return self

    @field_validator("url")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("技能来源需要有效的 HTTP 地址")
        return value.rstrip("/")

    @field_validator("id")
    @classmethod
    def unreserved_id(cls, value: str) -> str:
        if value in {"all", "imports"}:
            raise ValueError("来源标识已保留")
        return value


class SkillSource(ABC):
    """可注册的目录协议：浏览、详情和固定发行内容下载"""

    @property
    @abstractmethod
    def info(self) -> SkillSourceInfo:
        """返回不含凭据的公开来源说明"""

    @abstractmethod
    async def browse(self, *, query: str, cursor: str | None) -> RemoteSkillPage:
        """按来源能力搜索或按最近更新浏览，保留不透明游标"""

    @abstractmethod
    async def lookup(self, skill_id: str) -> RemoteSkill:
        """读取当前公开目录记录"""

    @abstractmethod
    async def detail(self, skill_id: str, revision: str) -> SkillDetail:
        """读取指定发行内容的技能说明"""

    @abstractmethod
    async def package(self, skill_id: str, revision: str) -> SkillPackage:
        """下载和校验固定发行内容，不运行其中代码"""

    def page_url(self, skill_id: str, revision: str) -> str:
        """返回可供用户核对来源的公开页面"""
        return f"{self.info.url}/{quote(skill_id, safe='/')}"


class SkillSources:
    """服务端注册多个来源，以稳定标识选择，不在失败时切换来源"""

    def __init__(self, sources: Sequence[SkillSource]) -> None:
        self._sources = {source.info.id: source for source in sources}
        if len(self._sources) != len(sources):
            raise ValueError("技能来源标识不得重复")

    def list(self) -> list[SkillSourceInfo]:
        return [source.info for source in self._sources.values()]

    def get(self, source_id: str) -> SkillSource:
        source = self._sources.get(source_id)
        if source is None:
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        return source


class _Owner(BaseModel):
    handle: str | None = None
    display_name: str | None = Field(default=None, alias="displayName")


class _Release(BaseModel):
    version: str


class _Item(BaseModel):
    slug: str
    display_name: str | None = Field(default=None, alias="displayName")
    summary: str | None = None
    topics: list[str] = Field(default_factory=list)
    owner_handle: str | None = Field(default=None, alias="ownerHandle")
    owner: _Owner | None = None
    latest_release: _Release | None = Field(default=None, alias="latestVersion")
    version: str | None = None
    updated_at: float | None = Field(
        default=None, alias="updatedAt", ge=0, le=253402300799000
    )


class _Page(BaseModel):
    items: list[_Item]
    cursor: str | None = Field(default=None, alias="nextCursor")


class _Search(BaseModel):
    results: list[_Item]


class _Detail(BaseModel):
    skill: _Item | None
    owner: _Owner | None = None
    latest_release: _Release | None = Field(default=None, alias="latestVersion")


class _Handoff(BaseModel):
    repo: str
    commit: str
    path: str


class ClawHubSkillSource(SkillSource):
    """ClawHub 官方 HTTP 协议，第三方字段和路径只在此处使用"""

    def __init__(
        self,
        settings: SkillSourceSettings,
        client: httpx.AsyncClient,
        reader: SkillArchiveReader,
        github: GitHubSkillImporter,
    ) -> None:
        self._settings = settings
        self._client = client
        self._reader = reader
        self._github = github

    @property
    def info(self) -> SkillSourceInfo:
        return SkillSourceInfo(
            id=self._settings.id, name=self._settings.name, url=self._settings.url
        )

    def _headers(self) -> dict[str, str]:
        token = self._settings.token
        return (
            {}
            if token is None
            else {"Authorization": f"Bearer {token.get_secret_value()}"}
        )

    @staticmethod
    def _identity(skill_id: str) -> tuple[str, dict[str, str]]:
        parts = skill_id.split("/")
        if not 1 <= len(parts) <= 2 or any(
            not part or part in {".", ".."} for part in parts
        ):
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        return quote(parts[-1], safe=""), {} if len(parts) == 1 else {
            "ownerHandle": parts[0]
        }

    def _card(self, item: _Item) -> RemoteSkill:
        owner = item.owner
        handle = item.owner_handle or (owner.handle if owner is not None else None)
        return RemoteSkill(
            id=f"{handle}/{item.slug}" if handle else item.slug,
            source_id=self.info.id,
            name=item.display_name or item.slug,
            description=item.summary or "",
            revision=item.latest_release.version
            if item.latest_release is not None
            else item.version,
            author=(owner.display_name or owner.handle)
            if owner is not None
            else handle,
            topics=item.topics,
            updated_at=None
            if item.updated_at is None
            else datetime.fromtimestamp(item.updated_at / 1000, UTC),
        )

    async def browse(self, *, query: str, cursor: str | None) -> RemoteSkillPage:
        if query and cursor:
            raise BusinessException(
                SkillErrorCode.INVALID_PACKAGE, message="来源搜索不支持游标"
            )
        params = {"limit": "24"}
        if query:
            params["q"] = query
        else:
            params["sort"] = "updated"
            if cursor:
                params["cursor"] = cursor
        response = await download(
            self._client,
            self.info.url + ("/api/v1/search" if query else "/api/v1/skills"),
            params=params,
            headers=self._headers(),
            maximum=2 * 1024 * 1024,
        )
        try:
            if query:
                return RemoteSkillPage(
                    items=[
                        self._card(item)
                        for item in _Search.model_validate_json(
                            response.content
                        ).results
                    ]
                )
            page = _Page.model_validate_json(response.content)
            return RemoteSkillPage(
                items=[self._card(item) for item in page.items], cursor=page.cursor
            )
        except (ValidationError, ValueError, OverflowError) as error:
            raise BusinessException(SkillErrorCode.UNAVAILABLE) from error

    async def lookup(self, skill_id: str) -> RemoteSkill:
        slug, params = self._identity(skill_id)
        response = await download(
            self._client,
            f"{self.info.url}/api/v1/skills/{slug}",
            params=params,
            headers=self._headers(),
            maximum=2 * 1024 * 1024,
        )
        try:
            payload = _Detail.model_validate_json(response.content)
            if payload.skill is None:
                raise BusinessException(SkillErrorCode.NOT_FOUND)
            card = self._card(
                payload.skill.model_copy(
                    update={
                        "owner": payload.owner,
                        "latest_release": payload.latest_release,
                    }
                )
            )
            if "/" in skill_id and card.id != skill_id:
                raise ValueError("来源发布者身份不一致")
            return card
        except (ValidationError, ValueError, OverflowError) as error:
            raise BusinessException(SkillErrorCode.UNAVAILABLE) from error

    async def detail(self, skill_id: str, revision: str) -> SkillDetail:
        card = await self.lookup(skill_id)
        slug, params = self._identity(card.id)
        response = await download(
            self._client,
            f"{self.info.url}/api/v1/skills/{slug}/file",
            params={**params, "path": "SKILL.md", "version": revision},
            headers=self._headers(),
            maximum=1024 * 1024,
        )
        try:
            markdown = response.content.decode("utf-8")
        except UnicodeError as error:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE) from error
        return SkillDetail(
            name=card.name,
            description=card.description,
            markdown=markdown,
            author=card.author,
            source_url=f"{self.info.url}/{quote(card.id, safe='/')}",
            topics=card.topics,
        )

    async def package(self, skill_id: str, revision: str) -> SkillPackage:
        slug, params = self._identity(skill_id)
        response = await download(
            self._client,
            f"{self.info.url}/api/v1/download",
            params={**params, "slug": skill_id.rsplit("/", 1)[-1], "version": revision},
            headers=self._headers(),
        )
        if "application/json" in response.content_type:
            try:
                handoff = _Handoff.model_validate_json(response.content)
            except ValidationError as error:
                raise BusinessException(SkillErrorCode.INVALID_PACKAGE) from error
            packages = await self._github.read_commit(
                handoff.repo, handoff.commit, handoff.path
            )
        else:
            packages = await self._reader.read(response.content)
        if len(packages) != 1:
            raise BusinessException(
                SkillErrorCode.INVALID_PACKAGE, message="来源下载必须对应单个技能"
            )
        return packages[0]
