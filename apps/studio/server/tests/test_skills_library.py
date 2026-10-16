"""技能包校验、安装发布和完整内容保存的可观察契约"""

import io
import stat
import sys
from collections.abc import Iterable
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_LZMA, ZipFile, ZipInfo

import pytest
from langgraph.store.base import Op, Result
from langgraph.store.memory import InMemoryStore
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import TinkerFin
from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.packages import (
    MAX_FILE_BYTES,
    SkillArchiveReader,
    SkillFile,
    parse_package,
)
from tinkerfin_studio.skills.repository import SkillRepository
from tinkerfin_studio.skills.schemas import (
    ConfirmImportRequest,
    InstallSkillRequest,
    SkillCommandRequest,
)

pytestmark = pytest.mark.usefixtures("projects")

_COMPRESSIONS = [ZIP_DEFLATED, ZIP_BZIP2, ZIP_LZMA]
if sys.version_info >= (3, 14):
    from zipfile import ZIP_ZSTANDARD

    _COMPRESSIONS.append(ZIP_ZSTANDARD)


def skill_files(name: str = "reports") -> tuple[SkillFile, ...]:
    return (
        SkillFile(
            "SKILL.md",
            f"---\nname: {name}\ndescription: 核对资料生成报告\n---\n读取 references/data.bin 并执行 scripts/run.py\n".encode(),
        ),
        SkillFile("scripts/run.py", b"from .helpers import run\nrun()\n"),
        SkillFile("references/data.bin", b"\x00\xff\x80\n"),
    )


def archive_bytes(files: tuple[SkillFile, ...]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for file in files:
            archive.writestr(file.path, file.content)
    return output.getvalue()


async def add_users(session: AsyncSession) -> None:
    session.add_all(
        [
            User(
                id=index,
                username=f"user{index}",
                password_hash="unused",
                roles=[],
                disabled=False,
            )
            for index in (1, 2)
        ]
    )
    await session.commit()


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/outside",
        "a/../outside",
        "a\\outside",
        "a//outside",
        "a/./outside",
        "C:outside",
    ],
)
async def test_archive_rejects_ambiguous_and_escaping_paths(path: str) -> None:
    with pytest.raises(BusinessException):
        await SkillArchiveReader().read(
            archive_bytes((*skill_files(), SkillFile(path, b"payload")))
        )


async def test_archive_rejects_symlinks_duplicate_paths_and_large_files() -> None:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        link = ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../outside")
    with pytest.raises(BusinessException):
        await SkillArchiveReader().read(output.getvalue())
    with pytest.raises(BusinessException):
        parse_package((*skill_files(), SkillFile("skill.md", b"duplicate")))
    with pytest.raises(BusinessException) as failure:
        parse_package((*skill_files(), SkillFile("large", b"x" * (MAX_FILE_BYTES + 1))))
    assert failure.value.error_code == SkillErrorCode.TOO_LARGE


async def test_install_idempotency_conflict_isolation_and_resume_snapshot(
    session: AsyncSession,
    skill_library: SkillLibrary,
) -> None:
    await add_users(session)
    preview = await skill_library.preview_zip(1, archive_bytes(skill_files()))
    request = ConfirmImportRequest(
        request_id="install", digests=[preview.candidates[0].digest]
    )
    result = await skill_library.confirm(1, preview.id, request)
    item = (await skill_library.list(1))[0]
    assert item.id == result.installation_ids[0] and item.enabled
    assert await skill_library.confirm(1, preview.id, request) == result
    assert await skill_library.list(2) == []
    with pytest.raises(BusinessException):
        await skill_library.detail(2, item.id)
    different = await skill_library.preview_zip(
        1, archive_bytes((*skill_files(), SkillFile("new", b"changed")))
    )
    with pytest.raises(BusinessException) as conflict:
        await skill_library.confirm(
            1,
            different.id,
            ConfirmImportRequest(
                request_id="conflict", digests=[different.candidates[0].digest]
            ),
        )
    assert conflict.value.error_code == SkillErrorCode.NAME_CONFLICT
    repository = SkillRepository(session, 1)
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="first")
    captured = await repository.capture(
        identity, project_id="project-1", selected_ids=(item.id,)
    )
    await session.commit()
    assert captured.skills[0].selected
    removal = SkillCommandRequest(request_id="remove")
    await skill_library.uninstall(1, item.id, removal)
    await skill_library.uninstall(1, item.id, removal)
    assert (
        await repository.capture(
            identity, project_id="project-1", selected_ids=(item.id,)
        )
        == captured
    )
    with pytest.raises(BusinessException) as changed:
        await repository.capture(identity, project_id="project-1", selected_ids=())
    assert changed.value.error_code == SkillErrorCode.SNAPSHOT_CONFLICT
    resumed = await repository.capture(
        RunIdentity(namespace="ns_1", thread_id="thread", run_id="resume"),
        project_id="project-1",
        source=identity,
    )
    assert resumed == captured
    assert (
        await skill_library.content.load(1, resumed.skills[0].digest)
    ).name == item.name
    with pytest.raises(BusinessException) as unavailable:
        await repository.capture(
            RunIdentity(namespace="ns_1", thread_id="thread", run_id="new"),
            project_id="project-1",
            selected_ids=(item.id,),
        )
    assert unavailable.value.error_code == SkillErrorCode.DISABLED


async def test_failed_content_write_never_publishes_an_installation(
    session: AsyncSession,
    skill_library: SkillLibrary,
) -> None:
    class FailingStore(InMemoryStore):
        async def abatch(self, ops: Iterable[Op]) -> list[Result]:
            raise OSError("storage unavailable")

    await add_users(session)
    skill_library.content = SkillContentStore(TinkerFin(store=FailingStore()))
    with pytest.raises(OSError):
        await skill_library.install(
            1,
            InstallSkillRequest(
                request_id="fail",
                source_id="catalog",
                skill_id="author/reports",
                revision="first",
            ),
        )
    assert await skill_library.list(1) == []
