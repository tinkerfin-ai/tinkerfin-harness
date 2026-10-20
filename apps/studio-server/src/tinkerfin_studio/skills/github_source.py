"""从官方 GitHub 仓库读取技能目录，安装固定提交中的完整文件"""

import asyncio
import re
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime
from typing import TypeVar
from urllib.parse import quote, urlsplit

import anyio
import httpx
from anyio.to_thread import run_sync
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.downloads import download
from tinkerfin_studio.skills.packages import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_SKILLS,
    MAX_TOTAL_BYTES,
    SkillFile,
    SkillPackage,
    parse_package,
    validate_path,
)
from tinkerfin_studio.skills.schemas import (
    RemoteSkill,
    RemoteSkillPage,
    SkillDetail,
    SkillSourceInfo,
)
from tinkerfin_studio.skills.sources import SkillSource, SkillSourceSettings


class _Committer(BaseModel):
    date: datetime


class _CommitInfo(BaseModel):
    committer: _Committer


class _Commit(BaseModel):
    sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    commit: _CommitInfo


class _Entry(BaseModel):
    path: str
    type: str
    mode: str
    size: int = Field(default=0, ge=0)


class _Tree(BaseModel):
    tree: list[_Entry]
    truncated: bool


T = TypeVar("T")
R = TypeVar("R")


async def _read_batch(
    items: Sequence[T], read: Callable[[T], Coroutine[object, object, R]]
) -> list[R]:
    """每批最多四次下载；失败和取消时等待本批所有请求释放连接"""
    result: list[R] = []
    for start in range(0, len(items), 4):
        tasks = [asyncio.create_task(read(item)) for item in items[start : start + 4]]
        try:
            result.extend(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return result


class GitHubSkillSource(SkillSource):
    """读取仓库 skills/ 下的技能；每个来源只保留两个固定提交的目录元数据"""

    def __init__(
        self, settings: SkillSourceSettings, client: httpx.AsyncClient
    ) -> None:
        self._settings = settings
        self._client = client
        self._repository = urlsplit(settings.url).path.strip("/")
        self._trees: OrderedDict[str, tuple[_Entry, ...]] = OrderedDict()
        self._catalogs: OrderedDict[str, tuple[RemoteSkill, ...]] = OrderedDict()
        self._parsing = anyio.CapacityLimiter(2)

    @property
    def info(self) -> SkillSourceInfo:
        return SkillSourceInfo(
            id=self._settings.id, name=self._settings.name, url=self._settings.url
        )

    def page_url(self, skill_id: str, revision: str) -> str:
        return f"{self.info.url}/tree/{revision}/{quote(skill_id, safe='/')}"

    async def _api(
        self, path: str, *, params: dict[str, str] | None = None, raw: bool = False
    ) -> bytes:
        token = self._settings.token
        headers = {
            "Accept": "application/vnd.github.raw+json"
            if raw
            else "application/vnd.github+json"
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        response = await download(
            self._client,
            f"https://api.github.com/repos/{self._repository}/{path}",
            params=params,
            headers=headers,
            maximum=8 * 1024 * 1024,
        )
        return response.content

    async def _latest(self) -> _Commit:
        try:
            commits = TypeAdapter(list[_Commit]).validate_json(
                await self._api("commits", params={"per_page": "1"})
            )
            if not commits:
                raise BusinessException(SkillErrorCode.NOT_FOUND)
            return commits[0]
        except ValidationError as error:
            raise BusinessException(SkillErrorCode.UNAVAILABLE) from error

    async def _tree(self, revision: str) -> tuple[_Entry, ...]:
        if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        cached = self._trees.get(revision)
        if cached is not None:
            return cached
        try:
            result = _Tree.model_validate_json(
                await self._api(f"git/trees/{revision}", params={"recursive": "1"})
            )
        except ValidationError as error:
            raise BusinessException(SkillErrorCode.UNAVAILABLE) from error
        if result.truncated:
            raise BusinessException(SkillErrorCode.TOO_LARGE)
        entries = tuple(
            entry for entry in result.tree if entry.path.startswith("skills/")
        )
        self._trees[revision] = entries
        while len(self._trees) > 2:
            self._trees.popitem(last=False)
        return entries

    async def _file(self, entry: _Entry, revision: str) -> SkillFile:
        validate_path(entry.path)
        if entry.type != "blob" or entry.mode not in {"100644", "100755"}:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        if entry.size > MAX_FILE_BYTES:
            raise BusinessException(SkillErrorCode.TOO_LARGE)
        content = await self._api(
            f"contents/{quote(entry.path, safe='/')}",
            params={"ref": revision},
            raw=True,
        )
        if len(content) != entry.size:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        return SkillFile(entry.path, content)

    async def _metadata(self, entry: _Entry, revision: str) -> SkillPackage:
        if entry.size > 1024 * 1024:
            raise BusinessException(SkillErrorCode.TOO_LARGE)
        file = await self._file(entry, revision)
        return await run_sync(
            parse_package, (SkillFile("SKILL.md", file.content),), limiter=self._parsing
        )

    async def browse(self, *, query: str, cursor: str | None) -> RemoteSkillPage:
        if cursor:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        commit = await self._latest()
        cards = self._catalogs.get(commit.sha)
        if cards is None:
            entries = await self._tree(commit.sha)
            roots = [
                entry
                for entry in entries
                if entry.path.count("/") == 2 and entry.path.endswith("/SKILL.md")
            ]
            if len(roots) > MAX_SKILLS:
                raise BusinessException(SkillErrorCode.TOO_LARGE)
            packages = await _read_batch(
                roots, lambda entry: self._metadata(entry, commit.sha)
            )
            cards = tuple(
                RemoteSkill(
                    id=entry.path.removesuffix("/SKILL.md"),
                    source_id=self.info.id,
                    name=package.name,
                    description=package.description,
                    revision=commit.sha,
                    author=self.info.name,
                    updated_at=commit.commit.committer.date,
                )
                for entry, package in zip(roots, packages, strict=True)
            )
            self._catalogs[commit.sha] = cards
            while len(self._catalogs) > 2:
                self._catalogs.popitem(last=False)
        term = query.casefold()
        return RemoteSkillPage(
            items=[
                card
                for card in cards
                if not term or term in f"{card.name} {card.description}".casefold()
            ]
        )

    async def lookup(self, skill_id: str) -> RemoteSkill:
        page = await self.browse(query="", cursor=None)
        match = next((card for card in page.items if card.id == skill_id), None)
        if match is None:
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        return match

    async def _members(self, skill_id: str, revision: str) -> tuple[_Entry, ...]:
        validate_path(skill_id)
        if not skill_id.startswith("skills/") or skill_id.count("/") != 1:
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        files = tuple(
            entry
            for entry in await self._tree(revision)
            if entry.path.startswith(skill_id + "/") and entry.type != "tree"
        )
        if not any(entry.path == skill_id + "/SKILL.md" for entry in files):
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        if (
            len(files) > MAX_FILES
            or sum(entry.size for entry in files) > MAX_TOTAL_BYTES
        ):
            raise BusinessException(SkillErrorCode.TOO_LARGE)
        return files

    async def detail(self, skill_id: str, revision: str) -> SkillDetail:
        files = await self._members(skill_id, revision)
        main = next(entry for entry in files if entry.path == skill_id + "/SKILL.md")
        package = await self._metadata(main, revision)
        return SkillDetail(
            name=package.name,
            description=package.description,
            markdown=package.markdown,
            files=[entry.path.removeprefix(skill_id + "/") for entry in files],
            author=self.info.name,
            source_url=self.page_url(skill_id, revision),
        )

    async def package(self, skill_id: str, revision: str) -> SkillPackage:
        entries = await self._members(skill_id, revision)
        files = await _read_batch(entries, lambda entry: self._file(entry, revision))
        relative = tuple(
            SkillFile(file.path.removeprefix(skill_id + "/"), file.content)
            for file in files
        )
        return await run_sync(parse_package, relative, limiter=self._parsing)
