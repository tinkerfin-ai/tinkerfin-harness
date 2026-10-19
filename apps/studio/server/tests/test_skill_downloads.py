"""GitHub 引用和技能子目录的解析，以及第三方错误的明确归属"""

import io
from urllib.parse import unquote
from zipfile import ZipFile

import httpx
import pytest

from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.downloads import GitHubSkillImporter
from tinkerfin_studio.skills.packages import SkillArchiveReader

COMMIT = "a" * 40
API = "https://api.github.com/repos/example/skills"
ARCHIVE = f"https://codeload.github.com/example/skills/zip/{COMMIT}"
MARKDOWN = b"---\nname: demo\ndescription: A test skill\n---\nUse the fixture.\n"


def archive_bytes() -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(f"skills-{COMMIT}/skills/demo/SKILL.md", MARKDOWN)
    return output.getvalue()


@pytest.mark.parametrize(
    ("reference", "missing_status"),
    [(COMMIT, 422), ("feature/browser-tools", 422), ("feature/browser-tools", 404)],
)
async def test_github_tree_resolves_reference_before_loading_fixed_skill_directory(
    reference, missing_status
):
    requests: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if url == ARCHIVE:
            return httpx.Response(200, content=archive_bytes())
        assert url.startswith(API + "/commits/")
        candidate = unquote(url.removeprefix(API + "/commits/"))
        if candidate == reference:
            return httpx.Response(200, json={"sha": COMMIT})
        return httpx.Response(
            missing_status,
            json={"message": f"No commit found for SHA: {candidate}"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        packages = await GitHubSkillImporter(client, SkillArchiveReader()).read(
            f"https://github.com/example/skills/tree/{reference}/skills/demo"
        )
    assert len(packages) == 1
    assert packages[0].name == "demo"
    assert packages[0].files[0].content == MARKDOWN
    assert requests.count(ARCHIVE) == 1
    assert len(requests) == len(set(requests))


@pytest.mark.parametrize(
    "body",
    [
        b'{"message":"Validation Failed"}',
        b'{"message":"No commit found for SHA: another-reference"}',
        b'{"message":123}',
        b"not-json",
    ],
)
async def test_other_unprocessable_commit_responses_stop_without_path_probing(body):
    requests: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(422, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(BusinessException) as failure:
            await GitHubSkillImporter(client, SkillArchiveReader()).read(
                f"https://github.com/example/skills/tree/{COMMIT}/skills/demo"
            )
    assert failure.value.error_code == SkillErrorCode.UNAVAILABLE
    assert len(requests) == 1


@pytest.mark.parametrize("status", [403, 429])
async def test_github_rate_limit_does_not_probe_another_reference(status):
    requests: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(status, headers={"x-ratelimit-remaining": "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(BusinessException) as failure:
            await GitHubSkillImporter(client, SkillArchiveReader()).read(
                f"https://github.com/example/skills/tree/{COMMIT}/skills/demo"
            )
    assert failure.value.error_code == SkillErrorCode.RATE_LIMITED
    assert len(requests) == 1


async def test_missing_repository_default_branch_is_reported_as_not_found():
    async def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == API:
            return httpx.Response(200, json={"default_branch": "main"})
        assert str(request.url) == API + "/commits/main"
        return httpx.Response(422, json={"message": "No commit found for SHA: main"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(BusinessException) as failure:
            await GitHubSkillImporter(client, SkillArchiveReader()).read(
                "https://github.com/example/skills"
            )
    assert failure.value.error_code == SkillErrorCode.NOT_FOUND
