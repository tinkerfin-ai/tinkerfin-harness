"""官方仓库来源按固定提交读取目录和二进制文件"""

import httpx
import pytest
from pydantic import SecretStr

from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.github_source import GitHubSkillSource
from tinkerfin_studio.skills.sources import SkillSourceSettings

REVISION = "a" * 40
FILES = {
    "skills/reports/SKILL.md": b"\xef\xbb\xbf---\nname: reports\ndescription: Verified reports\n---\nRead data.bin",
    "skills/reports/data.bin": b"\x00\xff\x80",
    "skills/reports/references/slides/SKILL.md": b"Nested reference",
    "template/SKILL.md": b"excluded",
}


async def test_github_catalog_search_details_and_install_keep_fixed_content() -> None:
    requested: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        if "/contents/" in request.url.path:
            assert request.url.host == "api.github.com"
            assert request.headers["authorization"] == "Bearer source-secret"
            assert request.headers["accept"] == "application/vnd.github.raw+json"
            assert request.url.params["ref"] == REVISION
            return httpx.Response(
                200, content=FILES[request.url.path.split("/contents/")[1]]
            )
        if request.url.host == "api.github.com":
            assert request.headers["authorization"] == "Bearer source-secret"
            if request.url.path.endswith("/commits"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "sha": REVISION,
                            "commit": {"committer": {"date": "2026-09-28T00:00:00Z"}},
                        }
                    ],
                )
            assert request.url.path.endswith(REVISION)
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {
                            "path": path,
                            "type": "blob",
                            "mode": "100644",
                            "size": len(content),
                        }
                        for path, content in FILES.items()
                    ],
                },
            )
        raise AssertionError("来源内容请求不得离开 GitHub API")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = GitHubSkillSource(
            SkillSourceSettings(
                kind="github",
                id="anthropic",
                name="Anthropic",
                url="https://github.com/anthropics/skills",
                token=SecretStr("source-secret"),
            ),
            client,
        )
        page = await source.browse(query="", cursor=None)
        assert [item.name for item in page.items] == ["reports"]
        assert (
            page.items[0].id == "skills/reports" and page.items[0].revision == REVISION
        )
        assert (await source.browse(query="absent", cursor=None)).items == []
        detail = await source.detail("skills/reports", REVISION)
        assert (
            detail.source_url
            == f"https://github.com/anthropics/skills/tree/{REVISION}/skills/reports"
        )
        assert detail.files == ["SKILL.md", "data.bin", "references/slides/SKILL.md"]
        package = await source.package("skills/reports", REVISION)
        assert {file.path: file.content for file in package.files} == {
            path.removeprefix("skills/reports/"): content
            for path, content in FILES.items()
            if path.startswith("skills/reports/")
        }
        assert (
            next(file.content for file in package.files if file.path == "data.bin")
            == FILES["skills/reports/data.bin"]
        )
        assert (
            len([request for request in requested if "/git/trees/" in request.url.path])
            == 1
        )
        with pytest.raises(BusinessException) as failure:
            await source.package("template", REVISION)
        assert failure.value.error_code == SkillErrorCode.NOT_FOUND


@pytest.mark.parametrize("truncated,mode", [(True, "100644"), (False, "120000")])
async def test_github_rejects_incomplete_trees_and_symlink_content(
    truncated: bool, mode: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        return httpx.Response(
            200,
            json={
                "truncated": truncated,
                "tree": [
                    {
                        "path": "skills/reports/SKILL.md",
                        "type": "blob",
                        "mode": mode,
                        "size": 20,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = GitHubSkillSource(
            SkillSourceSettings(
                kind="github",
                id="vercel",
                name="Vercel",
                url="https://github.com/vercel-labs/agent-skills",
            ),
            client,
        )
        with pytest.raises(BusinessException) as failure:
            await source.package("skills/reports", REVISION)
        assert failure.value.error_code == (
            SkillErrorCode.TOO_LARGE if truncated else SkillErrorCode.INVALID_PACKAGE
        )
