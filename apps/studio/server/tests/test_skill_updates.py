"""技能更新的固定内容、失败保护、并发前置条件与操作重放"""

import asyncio

import httpx
import pytest
from langgraph.store.memory import InMemoryStore
from skill_fakes import MemorySkillSource
from sqlalchemy.ext.asyncio import AsyncSession
from test_skills_library import add_users, archive_bytes, skill_files

from tinkerfin import TinkerFin
from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.downloads import GitHubSkillImporter
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.packages import (
    SkillArchiveReader,
    SkillFile,
    parse_package,
)
from tinkerfin_studio.skills.repository import SkillRepository
from tinkerfin_studio.skills.schemas import (
    ConfirmImportRequest,
    InstallSkillRequest,
    SkillCommandRequest,
    SkillEnabledRequest,
    SkillReplacement,
    UpdateSkillRequest,
)
from tinkerfin_studio.skills.sources import SkillSources

pytestmark = pytest.mark.usefixtures("projects")


def install_request(request_id: str = "install") -> InstallSkillRequest:
    return InstallSkillRequest(
        request_id=request_id,
        source_id="catalog",
        skill_id="author/reports",
        revision="first",
    )


async def test_catalog_update_retains_identity_state_and_both_run_contents(
    session: AsyncSession, skill_library: SkillLibrary, skill_catalog: MemorySkillSource
):
    await add_users(session)
    item = (await skill_library.install(1, install_request())).installation
    repository = SkillRepository(session, 1)
    original = await repository.capture(
        RunIdentity(namespace="ns_1", thread_id="thread", run_id="old"),
        project_id="project-1",
    )
    await session.commit()
    await skill_library.set_enabled(
        1, item.id, SkillEnabledRequest(request_id="disable", enabled=False)
    )
    skill_catalog.packages["second"] = parse_package(skill_files())
    skill_catalog.revision = "second"
    request = UpdateSkillRequest(request_id="update")
    changed = await skill_library.update(1, item.id, request)
    assert changed.changed and changed.installation.id == item.id
    assert (
        changed.installation.created_at == item.created_at
        and not changed.installation.enabled
    )
    assert (await skill_library.detail(1, item.id)).markdown == skill_catalog.packages[
        "second"
    ].markdown
    assert (
        await skill_library.content.load(1, original.skills[0].digest)
    ) == skill_catalog.packages["first"]
    downloads = skill_catalog.downloads
    skill_catalog.revision = "first"
    assert await skill_library.update(1, item.id, request) == changed
    assert skill_catalog.downloads == downloads
    await skill_library.set_enabled(
        1, item.id, SkillEnabledRequest(request_id="enable", enabled=True)
    )
    fresh = await repository.capture(
        RunIdentity(namespace="ns_1", thread_id="thread", run_id="fresh"),
        project_id="project-1",
    )
    assert fresh.skills[0].digest == skill_catalog.packages["second"].digest
    assert fresh.skills[0].digest != original.skills[0].digest


async def test_operation_replay_cannot_change_arguments_or_reinstall_deleted_skill(
    session: AsyncSession, skill_library: SkillLibrary, skill_catalog: MemorySkillSource
):
    await add_users(session)
    request = install_request()
    result = await skill_library.install(1, request)
    with pytest.raises(BusinessException) as conflict:
        await skill_library.install(1, request.model_copy(update={"revision": "other"}))
    assert conflict.value.error_code == SkillErrorCode.OPERATION_CONFLICT
    await skill_library.uninstall(
        1, result.installation.id, SkillCommandRequest(request_id="delete")
    )
    assert await skill_library.install(1, request) == result
    assert await skill_library.list(1) == []
    assert skill_catalog.downloads == 1
    with pytest.raises(BusinessException):
        await skill_library.update(
            2, result.installation.id, UpdateSkillRequest(request_id="foreign")
        )


async def test_unchanged_update_and_zip_replacement_validate_target_and_owner(
    session: AsyncSession, skill_library: SkillLibrary
):
    await add_users(session)
    item = (await skill_library.install(1, install_request())).installation
    result = await skill_library.update(
        1, item.id, UpdateSkillRequest(request_id="same")
    )
    assert not result.changed and result.installation.updated_at == item.updated_at
    await skill_library.uninstall(1, item.id, SkillCommandRequest(request_id="remove"))
    preview = await skill_library.preview_zip(1, archive_bytes(skill_files()))
    imported = await skill_library.confirm(
        1,
        preview.id,
        ConfirmImportRequest(request_id="zip", digests=[preview.candidates[0].digest]),
    )
    installed_id = imported.installation_ids[0]
    with pytest.raises(BusinessException) as missing:
        await skill_library.update(
            1, installed_id, UpdateSkillRequest(request_id="needs-file")
        )
    assert missing.value.error_code == SkillErrorCode.REPLACEMENT_REQUIRED
    wrong = await skill_library.preview_zip(1, archive_bytes(skill_files("other")))
    with pytest.raises(BusinessException) as mismatch:
        await skill_library.update(
            1,
            installed_id,
            UpdateSkillRequest(
                request_id="wrong",
                replacement=SkillReplacement(
                    draft_id=wrong.id, digest=wrong.candidates[0].digest
                ),
            ),
        )
    assert mismatch.value.error_code == SkillErrorCode.UPDATE_TARGET_MISSING
    foreign = await skill_library.preview_zip(2, archive_bytes(skill_files()))
    with pytest.raises(BusinessException):
        await skill_library.update(
            1,
            installed_id,
            UpdateSkillRequest(
                request_id="foreign-preview",
                replacement=SkillReplacement(
                    draft_id=foreign.id, digest=foreign.candidates[0].digest
                ),
            ),
        )
    fresh = await skill_library.preview_zip(
        1, archive_bytes((*skill_files(), SkillFile("new.txt", b"new")))
    )
    updated = await skill_library.update(
        1,
        installed_id,
        UpdateSkillRequest(
            request_id="replace",
            replacement=SkillReplacement(
                draft_id=fresh.id, digest=fresh.candidates[0].digest
            ),
        ),
    )
    assert updated.changed and updated.installation.source_kind == "zip"
    assert "new.txt" in (await skill_library.detail(1, installed_id)).files


async def test_cancelled_download_and_concurrent_uninstall_keep_authoritative_state(
    session: AsyncSession, skill_library: SkillLibrary, skill_catalog: MemorySkillSource
):
    await add_users(session)
    item = (await skill_library.install(1, install_request())).installation
    skill_catalog.release = asyncio.Event()
    skill_catalog.started.clear()
    work = asyncio.create_task(
        skill_library.update(1, item.id, UpdateSkillRequest(request_id="cancel"))
    )
    try:
        await skill_catalog.started.wait()
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    assert (await skill_library.list(1))[0] == item
    skill_catalog.started.clear()
    work = asyncio.create_task(
        skill_library.update(
            1, item.id, UpdateSkillRequest(request_id="deleted-during-download")
        )
    )
    try:
        await skill_catalog.started.wait()
        await skill_library.uninstall(
            1, item.id, SkillCommandRequest(request_id="remove")
        )
        skill_catalog.release.set()
        with pytest.raises(BusinessException) as missing:
            await work
        assert missing.value.error_code == SkillErrorCode.NOT_FOUND
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    assert await skill_library.list(1) == []


async def test_github_update_uses_original_address_and_same_declared_name(
    session: AsyncSession, database, attachments, notifications
):
    await add_users(session)
    files = [
        archive_bytes(
            tuple(
                SkillFile("repo/" + file.path, file.content) for file in skill_files()
            )
        )
    ]
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=files[0])
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(200, json={"default_branch": "main"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reader = SkillArchiveReader()
        library = SkillLibrary(
            database,
            SkillContentStore(TinkerFin(store=InMemoryStore())),
            sources=SkillSources([]),
            github=GitHubSkillImporter(client, reader),
            archives=reader,
            attachments=attachments,
            notifications=notifications,
        )
        preview = await library.preview_github(1, "https://github.com/owner/skills")
        result = await library.confirm(
            1,
            preview.id,
            ConfirmImportRequest(
                request_id="install", digests=[preview.candidates[0].digest]
            ),
        )
        files[0] = archive_bytes(
            tuple(
                SkillFile("repo/" + file.path, file.content)
                for file in (*skill_files(), SkillFile("new", b"changed"))
            )
        )
        updated = await library.update(
            1, result.installation_ids[0], UpdateSkillRequest(request_id="update")
        )
        assert updated.changed and updated.installation.source_kind == "github"
        assert calls.count("https://api.github.com/repos/owner/skills") == 2
        assert all(
            url.startswith(
                (
                    "https://api.github.com/repos/owner/skills",
                    "https://codeload.github.com/owner/skills/zip/",
                )
            )
            for url in calls
        )


async def test_update_rejects_stale_download_and_preserves_later_enable_choice(
    session: AsyncSession,
    skill_library: SkillLibrary,
    skill_catalog: MemorySkillSource,
):
    await add_users(session)
    item = (await skill_library.install(1, install_request())).installation
    skill_catalog.packages["second"] = parse_package(skill_files())
    skill_catalog.packages["third"] = parse_package(
        (*skill_files(), SkillFile("latest", b"newest"))
    )
    skill_catalog.revision = "second"
    release = asyncio.Event()
    skill_catalog.release = release
    skill_catalog.started.clear()
    work = asyncio.create_task(
        skill_library.update(1, item.id, UpdateSkillRequest(request_id="slow"))
    )
    try:
        await skill_catalog.started.wait()
        await skill_library.set_enabled(
            1, item.id, SkillEnabledRequest(request_id="disable", enabled=False)
        )
        skill_catalog.release = None
        skill_catalog.revision = "third"
        newest = await skill_library.update(
            1, item.id, UpdateSkillRequest(request_id="newest")
        )
        assert not newest.installation.enabled
        release.set()
        with pytest.raises(BusinessException) as conflict:
            await work
        assert conflict.value.error_code == SkillErrorCode.UPDATE_CONFLICT
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    assert "latest" in (await skill_library.detail(1, item.id)).files


async def test_failed_update_save_preserves_old_installation_and_can_be_retried(
    session: AsyncSession,
    skill_library: SkillLibrary,
    skill_catalog: MemorySkillSource,
    monkeypatch: pytest.MonkeyPatch,
):
    await add_users(session)
    item = (await skill_library.install(1, install_request())).installation
    skill_catalog.packages["second"] = parse_package(skill_files())
    skill_catalog.revision = "second"
    request = UpdateSkillRequest(request_id="update")

    async def fail(*args):
        raise OSError("store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(skill_library.content, "save", fail)
        with pytest.raises(OSError):
            await skill_library.update(1, item.id, request)
    assert (await skill_library.list(1))[0] == item
    assert (await skill_library.detail(1, item.id)).markdown == skill_catalog.packages[
        "first"
    ].markdown
    assert (await skill_library.update(1, item.id, request)).changed
