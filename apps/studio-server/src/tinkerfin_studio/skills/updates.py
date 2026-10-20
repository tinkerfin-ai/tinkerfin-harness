"""按安装来源准备更新内容，不持有业务事务或修改安装关系"""

from dataclasses import dataclass
from typing import Protocol

from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.downloads import GitHubSkillImporter
from tinkerfin_studio.skills.packages import SkillPackage
from tinkerfin_studio.skills.repository import SkillOrigin
from tinkerfin_studio.skills.sources import SkillSources


@dataclass(frozen=True, slots=True)
class SkillUpdateTarget:
    """下载前固定的安装身份与内容，提交时用摘要检查并发变更"""

    id: str
    name: str
    digest: str
    origin: SkillOrigin


@dataclass(frozen=True, slots=True)
class SkillUpdateContent:
    """已经解析校验的候选包及其真实来源"""

    package: SkillPackage
    origin: SkillOrigin


class SkillUpdateStrategy(Protocol):
    """三类来源共用的候选内容准备边界，权限与发布由技能库负责"""

    async def prepare(
        self, target: SkillUpdateTarget, replacement: SkillPackage | None
    ) -> SkillUpdateContent:
        """准备一次固定内容，不执行技能代码或操作数据库"""
        ...


class CatalogSkillUpdate:
    """沿原来源和发布者标识读取当前发行，下载时固定发行内容"""

    def __init__(self, sources: SkillSources) -> None:
        self._sources = sources

    async def prepare(
        self, target: SkillUpdateTarget, replacement: SkillPackage | None
    ) -> SkillUpdateContent:
        if replacement is not None:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        origin = target.origin
        if origin.source_id is None or origin.external_id is None:
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        source = self._sources.get(origin.source_id)
        card = await source.lookup(origin.external_id)
        if card.revision is None:
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        package = await source.package(card.id, card.revision)
        return SkillUpdateContent(
            package,
            SkillOrigin(
                name=source.info.name,
                kind="catalog",
                source_id=source.info.id,
                external_id=card.id,
                revision=card.revision,
                url=source.page_url(card.id, card.revision),
                author=card.author,
                topics=tuple(card.topics),
            ),
        )


class GitHubSkillUpdate:
    """沿原导入地址固定提交，以声明名选择同一技能"""

    def __init__(self, github: GitHubSkillImporter) -> None:
        self._github = github

    async def prepare(
        self, target: SkillUpdateTarget, replacement: SkillPackage | None
    ) -> SkillUpdateContent:
        if replacement is not None or target.origin.url is None:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        packages = await self._github.read(target.origin.url)
        matches = [package for package in packages if package.name == target.name]
        if len(matches) != 1:
            raise BusinessException(SkillErrorCode.UPDATE_TARGET_MISSING)
        return SkillUpdateContent(matches[0], target.origin)


class ZipSkillUpdate:
    """只接受用户已预览并指定的同名 ZIP 内容"""

    async def prepare(
        self, target: SkillUpdateTarget, replacement: SkillPackage | None
    ) -> SkillUpdateContent:
        if replacement is None:
            raise BusinessException(SkillErrorCode.REPLACEMENT_REQUIRED)
        if replacement.name != target.name:
            raise BusinessException(SkillErrorCode.UPDATE_TARGET_MISSING)
        return SkillUpdateContent(replacement, target.origin)
