"""技能包校验、安装发布和运行固定内容的可观察契约"""

import io
import stat
import struct
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


def damaged_archive(compression: int = ZIP_DEFLATED) -> bytes:
    """保留完整 ZIP 目录表，只损坏首个文件的压缩数据"""
    output = io.BytesIO()
    with ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("SKILL.md", skill_files()[0].content)
        compressed_size = archive.getinfo("SKILL.md").compress_size
    data = bytearray(output.getvalue())
    name_length, extra_length = struct.unpack_from("<HH", data, 26)
    start = 30 + name_length + extra_length
    if compression == ZIP_LZMA:
        # 保留 ZIP 中的 LZMA 属性头，只损坏实际压缩流
        start += 9
        compressed_size -= 9
    data[start : start + compressed_size] = b"\xff" * compressed_size
    return bytes(data)


def test_frontmatter_handles_crlf_multiline_and_literal_delimiters_without_rewriting_bytes() -> (
    None
):
    markdown = "---\r\nname: café-reports\r\ndescription: |\r\n  Reports with --- separators\r\nmetadata:\r\n  author: sample\r\n---\r\n# Instructions\r\n".encode()
    package = parse_package((SkillFile("SKILL.md", markdown),))
    assert package.name == "café-reports"
    assert "--- separators" in package.description
    assert package.files[0].content == markdown


async def add_users(session: AsyncSession) -> None:
    session.add_all(
        [
            User(
                id=index,
                username=f"user{index}",
                display_name="用户",
                password_hash="unused",
                roles=[],
                disabled=False,
            )
            for index in (1, 2)
        ]
    )
    await session.commit()


async def test_zip_and_persistent_store_preserve_full_directories_and_binary_bytes() -> (
    None
):
    packages = await SkillArchiveReader().read(
        archive_bytes(
            tuple(
                SkillFile("repository/" + file.path, file.content)
                for file in skill_files()
            )
        )
    )
    package = packages[0]
    assert package.files == tuple(sorted(skill_files(), key=lambda file: file.path))
    content = SkillContentStore(TinkerFin(store=InMemoryStore()))
    await content.save(1, package)
    assert await content.load(1, package.digest) == package
    with pytest.raises(BusinessException) as failure:
        await content.load(2, package.digest)
    assert failure.value.error_code == SkillErrorCode.CONTENT_UNAVAILABLE


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


@pytest.mark.parametrize("compression", _COMPRESSIONS)
async def test_archive_reports_invalid_compressed_content_as_a_package_error(
    compression: int,
) -> None:
    with pytest.raises(BusinessException) as failure:
        await SkillArchiveReader().read(damaged_archive(compression))
    assert failure.value.error_code == SkillErrorCode.INVALID_PACKAGE


@pytest.mark.parametrize(
    "paths",
    [
        ("references", "references/data.bin"),
        ("references/data.bin", "references"),
        ("References", "references/data.bin"),
        ("references/data.bin", "REFERENCES"),
    ],
)
def test_package_rejects_files_used_as_parent_directories(
    paths: tuple[str, str],
) -> None:
    files = (skill_files()[0], *(SkillFile(path, b"content") for path in paths))
    with pytest.raises(BusinessException) as failure:
        parse_package(files)
    assert failure.value.error_code == SkillErrorCode.INVALID_PACKAGE


@pytest.mark.parametrize(
    "paths",
    [
        ("references", "references/data.bin"),
        ("references/data.bin", "references"),
        ("References", "references/data.bin"),
        ("references", "references/"),
        ("references/", "REFERENCES"),
        ("references", "references/nested/"),
        ("references/nested/", "References"),
    ],
)
async def test_archive_rejects_conflicting_file_and_directory_paths(
    paths: tuple[str, str],
) -> None:
    files = (skill_files()[0], *(SkillFile(path, b"") for path in paths))
    with pytest.raises(BusinessException) as failure:
        await SkillArchiveReader().read(archive_bytes(files))
    assert failure.value.error_code == SkillErrorCode.INVALID_PACKAGE


async def test_archive_preserves_files_under_explicit_directories() -> None:
    expected = (skill_files()[0], SkillFile("references/nested/data.bin", b"\x00\xff"))
    packages = await SkillArchiveReader().read(
        archive_bytes(
            (
                SkillFile("references/", b""),
                *expected,
                SkillFile("references/nested/", b""),
            )
        )
    )
    assert packages[0].files == expected


@pytest.mark.parametrize(
    "markdown",
    [
        b"# Missing frontmatter",
        b"---\nname: ../escape\ndescription: test\n---\n",
        b"---\nname: okay\ndescription: []\n---\n",
        b"---\nname: reports\ndescription: Reports\n---",
        b"---\r\nname: reports\r\ndescription: Reports\r\n---\r",
    ],
)
def test_skill_requires_valid_name_and_description(markdown: bytes) -> None:
    with pytest.raises(BusinessException):
        parse_package((SkillFile("SKILL.md", markdown),))


def test_long_description_limits_catalog_excerpt_and_preserves_original_file() -> None:
    description = "Full description " * 100
    markdown = (
        f"---\nname: reports\ndescription: {description}\n---\nInstructions".encode()
    )
    package = parse_package((SkillFile("SKILL.md", markdown),))
    assert package.description == description.strip()[:1024]
    assert package.files[0].content == markdown


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
    captured = await repository.capture(identity, selected_ids=(item.id,))
    await session.commit()
    assert captured.skills[0].selected
    removal = SkillCommandRequest(request_id="remove")
    await skill_library.uninstall(1, item.id, removal)
    await skill_library.uninstall(1, item.id, removal)
    assert await repository.capture(identity, selected_ids=(item.id,)) == captured
    with pytest.raises(BusinessException) as changed:
        await repository.capture(identity, selected_ids=())
    assert changed.value.error_code == SkillErrorCode.SNAPSHOT_CONFLICT
    resumed = await repository.capture(
        RunIdentity(namespace="ns_1", thread_id="thread", run_id="resume"),
        source=identity,
    )
    assert resumed == captured
    assert (
        await skill_library.content.load(1, resumed.skills[0].digest)
    ).name == item.name
    with pytest.raises(BusinessException) as unavailable:
        await repository.capture(
            RunIdentity(namespace="ns_1", thread_id="thread", run_id="new"),
            selected_ids=(item.id,),
        )
    assert unavailable.value.error_code == SkillErrorCode.DISABLED


async def test_import_confirmation_uses_preview_and_is_idempotent(
    session: AsyncSession,
    skill_library: SkillLibrary,
) -> None:
    await add_users(session)
    preview = await skill_library.preview_zip(1, archive_bytes(skill_files()))
    request = ConfirmImportRequest(
        request_id="confirm", digests=[preview.candidates[0].digest]
    )
    result = await skill_library.confirm(1, preview.id, request)
    assert await skill_library.confirm(1, preview.id, request) == result
    assert (
        await skill_library.detail(1, result.installation_ids[0])
    ).markdown == parse_package(skill_files()).markdown
    with pytest.raises(BusinessException):
        await skill_library.confirm(2, preview.id, request)


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
