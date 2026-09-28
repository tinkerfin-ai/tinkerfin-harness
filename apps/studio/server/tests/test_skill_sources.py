"""第三方技能来源的游标、固定下载和失败边界"""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from test_skills_library import archive_bytes, skill_files

from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.downloads import GitHubSkillImporter, download
from tinkerfin_studio.skills.packages import SkillArchiveReader, SkillFile
from tinkerfin_studio.skills.sources import (
    ClawHubSkillSource,
    SkillSources,
    SkillSourceSettings,
)


def source(
    client: httpx.AsyncClient, *, source_id: str = "clawhub"
) -> ClawHubSkillSource:
    reader = SkillArchiveReader()
    return ClawHubSkillSource(
        SkillSourceSettings(id=source_id, token=SecretStr("private-token")),
        client,
        reader,
        GitHubSkillImporter(client, reader),
    )


async def test_catalog_preserves_empty_page_cursor_and_source_identity() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer private-token"
        if request.url.params.get("cursor") == "next":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "ownerHandle": "author",
                            "slug": "reports",
                            "displayName": "Reports",
                            "summary": "Generate reports",
                            "topics": ["Writing"],
                            "latestVersion": {"version": "1.2.0"},
                        }
                    ],
                    "nextCursor": None,
                },
            )
        assert request.url.params["sort"] == "updated"
        return httpx.Response(200, json={"items": [], "nextCursor": "next"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        registry = SkillSources([source(client), source(client, source_id="team")])
        page = await registry.get("team").browse(query="", cursor=None)
        assert page.items == [] and page.cursor == "next"
        page = await registry.get("team").browse(query="", cursor=page.cursor)
        assert page.items[0].id == "author/reports"
        assert page.items[0].source_id == "team"
        assert page.items[0].topics == ["Writing"]
        assert "private-token" not in str(registry.list())
        with pytest.raises(ValueError):
            SkillSources([source(client), source(client)])


async def test_search_has_no_cursor_and_download_pins_owner_and_release() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("search"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "slug": "reports",
                            "ownerHandle": "author",
                            "version": "fixed",
                            "summary": "Search result",
                        }
                    ]
                },
            )
        assert request.url.params["ownerHandle"] == "author"
        assert request.url.params["version"] == "fixed"
        return httpx.Response(
            200,
            content=archive_bytes(skill_files()),
            headers={"content-type": "application/zip"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = source(client)
        page = await remote.browse(query="reports", cursor=None)
        assert page.cursor is None
        assert (await remote.package(page.items[0].id, "fixed")).name == "reports"
        with pytest.raises(BusinessException):
            await remote.browse(query="reports", cursor="invalid")
    assert len(requests) == 2


@pytest.mark.parametrize(
    "status,code",
    [
        (429, SkillErrorCode.RATE_LIMITED),
        (503, SkillErrorCode.UNAVAILABLE),
        (404, SkillErrorCode.NOT_FOUND),
        (302, SkillErrorCode.UNAVAILABLE),
    ],
)
async def test_source_failure_is_not_an_empty_catalog(
    status: int, code: SkillErrorCode
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    ) as client:
        with pytest.raises(BusinessException) as error:
            await source(client).browse(query="", cursor=None)
        assert error.value.error_code == code


async def test_github_handoff_uses_fixed_commit_and_never_forwards_source_secret() -> (
    None
):
    commit = "a" * 40
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.host == "clawhub.ai":
            return httpx.Response(
                200,
                json={
                    "repo": "author/repository",
                    "commit": commit,
                    "path": "skills/reports",
                    "archiveUrl": "http://127.0.0.1/private",
                },
            )
        assert request.url.host == "codeload.github.com"
        assert "authorization" not in request.headers
        assert request.url.path.endswith(commit)
        return httpx.Response(
            200,
            content=archive_bytes(
                tuple(
                    SkillFile("repository/skills/reports/" + file.path, file.content)
                    for file in skill_files()
                )
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert (
            await source(client).package("author/reports", "fixed")
        ).name == "reports"
    assert len(urls) == 2 and all("127.0.0.1" not in url for url in urls)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://github.com@localhost/a/b",
        "https://example.com/a/b",
        "https://github.com/a/b?redirect=localhost",
        "https://[github.com/a/b",
        "https://github.com\uff0fa/b",
    ],
)
async def test_github_import_rejects_arbitrary_network_destinations(url: str) -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        raise AssertionError("无效地址不得触发网络访问")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(BusinessException):
            await GitHubSkillImporter(client, SkillArchiveReader()).read(url)


async def test_cancelled_download_closes_only_its_response() -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b"never"

        async def aclose(self) -> None:
            closed.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
    ) as client:
        task = asyncio.create_task(
            download(client, "https://clawhub.ai/api/v1/download")
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and not client.is_closed
