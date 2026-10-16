"""固定发行内容与可控下载信号，用于技能管理的来源契约验证"""

import asyncio

from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.packages import SkillPackage
from tinkerfin_studio.skills.schemas import (
    RemoteSkill,
    RemoteSkillPage,
    SkillDetail,
    SkillSourceInfo,
)
from tinkerfin_studio.skills.sources import SkillSource


class MemorySkillSource(SkillSource):
    def __init__(self, packages: dict[str, SkillPackage]) -> None:
        self.packages = packages
        self.revision = next(iter(packages))
        self.downloads = 0
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    @property
    def info(self) -> SkillSourceInfo:
        return SkillSourceInfo(
            id="catalog", name="Catalog", url="https://skills.example"
        )

    async def browse(self, *, query: str, cursor: str | None) -> RemoteSkillPage:
        item = await self.lookup("author/reports")
        return RemoteSkillPage(items=[item] if not query or query in item.name else [])

    async def lookup(self, skill_id: str) -> RemoteSkill:
        if skill_id != "author/reports":
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        package = self.packages[self.revision]
        return RemoteSkill(
            id=skill_id,
            source_id="catalog",
            name=package.name,
            description=package.description,
            revision=self.revision,
        )

    async def detail(self, skill_id: str, revision: str) -> SkillDetail:
        package = await self.package(skill_id, revision)
        return SkillDetail(
            name=package.name,
            description=package.description,
            markdown=package.markdown,
            files=[file.path for file in package.files],
        )

    async def package(self, skill_id: str, revision: str) -> SkillPackage:
        self.downloads += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return self.packages[revision]
