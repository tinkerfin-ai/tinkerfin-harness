"""有界远程下载与 GitHub 固定提交导入"""

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.packages import (
    MAX_ARCHIVE_BYTES,
    SkillArchiveReader,
    SkillPackage,
    validate_path,
)


@dataclass(frozen=True, slots=True)
class Download:
    """已关闭响应连接的有界内容"""

    content: bytes
    content_type: str


async def download(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    maximum: int = MAX_ARCHIVE_BYTES,
) -> Download:
    """只借用客户端；失败、取消或超限时关闭本次响应，不跟随未知重定向"""
    try:
        async with client.stream(
            "GET",
            url,
            params=params,
            headers=headers,
            follow_redirects=False,
            timeout=30,
        ) as response:
            if response.status_code == 404:
                raise BusinessException(SkillErrorCode.NOT_FOUND)
            if response.status_code == 429 or (
                response.status_code == 403
                and response.headers.get("x-ratelimit-remaining") == "0"
            ):
                raise BusinessException(SkillErrorCode.RATE_LIMITED)
            if response.status_code == 409:
                raise BusinessException(
                    SkillErrorCode.NOT_FOUND,
                    message="来源中存在同名技能，请选择明确的发布者",
                )
            if response.status_code >= 300:
                raise BusinessException(SkillErrorCode.UNAVAILABLE)
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > maximum:
                    raise BusinessException(SkillErrorCode.TOO_LARGE)
                content.extend(chunk)
            return Download(bytes(content), response.headers.get("content-type", ""))
    except httpx.HTTPError as error:
        raise BusinessException(SkillErrorCode.UNAVAILABLE) from error


class _Repository(BaseModel):
    default_branch: str = Field(min_length=1)


class _Commit(BaseModel):
    sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")


class GitHubSkillImporter:
    """从公开 GitHub 仓库固定提交，来源地址不会成为任意服务端请求"""

    def __init__(self, client: httpx.AsyncClient, reader: SkillArchiveReader) -> None:
        self._client = client
        self._reader = reader

    async def read(self, url: str) -> tuple[SkillPackage, ...]:
        try:
            parsed = urlsplit(url)
        except ValueError as error:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE) from error
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            raise BusinessException(
                SkillErrorCode.INVALID_PACKAGE,
                message="请输入 GitHub 仓库或 tree 子目录的 HTTPS 地址",
            )
        parts = unquote(parsed.path).strip("/").split("/")
        if len(parts) < 2 or len(parts) > 20:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        owner, repository = parts[:2]
        repository = repository.removesuffix(".git")
        self._validate_repository(f"{owner}/{repository}")
        api = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
        directory = ""
        try:
            if len(parts) == 2:
                response = await download(self._client, api, maximum=1024 * 1024)
                reference = _Repository.model_validate_json(
                    response.content
                ).default_branch
                commit_response = await download(
                    self._client,
                    f"{api}/commits/{quote(reference, safe='')}",
                    maximum=2 * 1024 * 1024,
                )
                commit = _Commit.model_validate_json(commit_response.content).sha
            else:
                if len(parts) < 4 or parts[2] != "tree":
                    raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
                # 分支可含斜杠；仅将 GitHub 明确返回的不存在视为下一种路径划分
                commit = ""
                for boundary in range(len(parts), 3, -1):
                    reference = "/".join(parts[3:boundary])
                    try:
                        response = await download(
                            self._client,
                            f"{api}/commits/{quote(reference, safe='')}",
                            maximum=2 * 1024 * 1024,
                        )
                    except BusinessException as error:
                        if error.error_code == SkillErrorCode.NOT_FOUND:
                            continue
                        raise
                    commit = _Commit.model_validate_json(response.content).sha
                    directory = "/".join(parts[boundary:])
                    break
                if not commit:
                    raise BusinessException(SkillErrorCode.NOT_FOUND)
        except (ValidationError, json.JSONDecodeError) as error:
            raise BusinessException(SkillErrorCode.UNAVAILABLE) from error
        return await self.read_commit(f"{owner}/{repository}", commit, directory)

    @staticmethod
    def _validate_repository(repository: str) -> None:
        if not re.fullmatch(
            r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository
        ) or repository.split("/")[-1] in {".", ".."}:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)

    async def read_commit(
        self, repository: str, commit: str, directory: str
    ) -> tuple[SkillPackage, ...]:
        """第三方下载交接只接受固定仓库与提交，不请求交接中的任意 URL"""
        self._validate_repository(repository)
        if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        if directory:
            validate_path(directory)
        response = await download(
            self._client, f"https://codeload.github.com/{repository}/zip/{commit}"
        )
        return await self._reader.read(response.content, subdirectory=directory)
