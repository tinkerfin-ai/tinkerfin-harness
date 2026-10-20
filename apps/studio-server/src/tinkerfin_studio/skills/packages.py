"""校验技能目录并保留脚本和资源的原始字节"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from lzma import LZMAError
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

import anyio
import frontmatter
from anyio.to_thread import run_sync
from frontmatter.default_handlers import YAMLHandler
from pydantic import BaseModel, ConfigDict, Field, field_validator
from yaml import YAMLError

from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 1024
MAX_SKILLS = 64

_ARCHIVE_ERRORS: tuple[type[Exception], ...] = (
    BadZipFile,
    OSError,
    EOFError,
    RuntimeError,
    NotImplementedError,
    ZlibError,
    LZMAError,
)
if sys.version_info >= (3, 14):
    from compression.zstd import ZstdError

    _ARCHIVE_ERRORS += (ZstdError,)


class SkillFrontmatter(BaseModel):
    """从 SKILL.md 读取可供目录和 Agent 使用的真实元数据"""

    model_config = ConfigDict(extra="ignore", strict=True)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def executable_name(cls, value: str) -> str:
        """与 Deep Agents 0.7.19 接收的小写字母、数字及单连字符保持一致"""
        if (
            value.startswith("-")
            or value.endswith("-")
            or "--" in value
            or not all(
                character == "-"
                or (character.isalpha() and character.islower())
                or character.isdigit()
                for character in value
            )
        ):
            raise ValueError("技能名只能包含小写字母、数字和单连字符")
        return value


@dataclass(frozen=True, slots=True)
class SkillFile:
    """相对技能根目录的文件和原始内容"""

    path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """已校验、内容不可变的单个技能目录"""

    name: str
    description: str
    digest: str
    files: tuple[SkillFile, ...]

    @property
    def byte_size(self) -> int:
        return sum(len(file.content) for file in self.files)

    @property
    def markdown(self) -> str:
        return next(
            file.content.decode("utf-8")
            for file in self.files
            if file.path == "SKILL.md"
        )


def validate_path(path: str) -> str:
    """拒绝路径穿越、绝对路径和可能产生不同解读的文件名"""
    parts = path.split("/")
    if (
        not path
        or len(path) > 512
        or "\\" in path
        or ":" in path
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or PurePosixPath(path).is_absolute()
    ):
        raise BusinessException(
            SkillErrorCode.INVALID_PACKAGE, message="技能包包含不安全的文件路径"
        )
    return path


def _validate_entry_paths(entries: Iterable[tuple[str, bool]]) -> None:
    paths: set[str] = set()
    files: set[str] = set()
    for path, is_directory in entries:
        key = validate_path(path).casefold()
        if key in paths:
            raise BusinessException(
                SkillErrorCode.INVALID_PACKAGE, message="技能包包含重复路径"
            )
        paths.add(key)
        if not is_directory:
            files.add(key)
    if any(
        str(parent) in files for path in paths for parent in PurePosixPath(path).parents
    ):
        raise BusinessException(
            SkillErrorCode.INVALID_PACKAGE, message="技能包中的文件与目录路径冲突"
        )


def parse_package(files: tuple[SkillFile, ...]) -> SkillPackage:
    """校验单个技能，内容摘要同时固定文件名、目录与原始字节"""
    if (
        not files
        or len(files) > MAX_FILES
        or sum(len(file.content) for file in files) > MAX_TOTAL_BYTES
    ):
        raise BusinessException(SkillErrorCode.TOO_LARGE)
    _validate_entry_paths((file.path, False) for file in files)
    for file in files:
        if len(file.content) > MAX_FILE_BYTES:
            raise BusinessException(SkillErrorCode.TOO_LARGE)
    main = next((file for file in files if file.path == "SKILL.md"), None)
    if main is None or len(main.content) > 1024 * 1024:
        raise BusinessException(
            SkillErrorCode.INVALID_PACKAGE, message="技能需要不超过 1 MiB 的 SKILL.md"
        )
    try:
        markdown = main.content.decode("utf-8-sig")
        if re.match(r"^---\r?\n.*?\n---\s*\n", markdown, re.DOTALL) is None:
            raise ValueError("技能元数据需要完整分隔符，结束分隔符后须换行")
        parsed, _ = frontmatter.parse(markdown, handler=YAMLHandler())
        metadata = SkillFrontmatter.model_validate(parsed)
        if not metadata.description.strip():
            raise ValueError("技能描述不能为空")
    except (UnicodeError, ValueError, YAMLError) as error:
        raise BusinessException(
            SkillErrorCode.INVALID_PACKAGE,
            message="SKILL.md 需要有效的 name 和 description 元数据",
        ) from error
    ordered = tuple(sorted(files, key=lambda file: file.path))
    digest = hashlib.sha256()
    for file in ordered:
        path = file.path.encode("utf-8")
        digest.update(len(path).to_bytes(4, "big"))
        digest.update(path)
        digest.update(len(file.content).to_bytes(8, "big"))
        digest.update(file.content)
    # 目录摘要与 Deep Agents SkillsMiddleware 的展示上限一致，原始文件保留全文
    return SkillPackage(
        metadata.name, metadata.description.strip()[:1024], digest.hexdigest(), ordered
    )


def parse_archive(content: bytes, subdirectory: str = "") -> tuple[SkillPackage, ...]:
    """校验压缩包并读取完整技能，调用方须使用有容量限制的线程

    Args:
        content: 已有界接收的压缩包原始字节
        subdirectory: GitHub 仓库压缩包内要读取的相对子目录

    Returns:
        原始字节完整保留且已校验的技能包集合

    Raises:
        BusinessException: 包、路径、技能元数据或容量不符合要求
    """
    if len(content) > MAX_ARCHIVE_BYTES:
        raise BusinessException(SkillErrorCode.TOO_LARGE)
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > MAX_FILES
                or sum(entry.file_size for entry in entries) > MAX_TOTAL_BYTES
            ):
                raise BusinessException(SkillErrorCode.TOO_LARGE)
            paths = [
                (
                    entry.filename.rstrip("/") if entry.is_dir() else entry.filename,
                    entry.is_dir(),
                )
                for entry in entries
            ]
            _validate_entry_paths(paths)
            files: list[SkillFile] = []
            for entry, (path, is_directory) in zip(entries, paths, strict=True):
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (
                    stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                ):
                    raise BusinessException(
                        SkillErrorCode.INVALID_PACKAGE,
                        message="技能包不能包含链接或特殊文件",
                    )
                if is_directory:
                    continue
                if entry.file_size > MAX_FILE_BYTES or entry.flag_bits & 1:
                    raise BusinessException(SkillErrorCode.TOO_LARGE)
                with archive.open(entry) as source:
                    data = source.read(MAX_FILE_BYTES + 1)
                if len(data) != entry.file_size or len(data) > MAX_FILE_BYTES:
                    raise BusinessException(SkillErrorCode.TOO_LARGE)
                files.append(SkillFile(path, data))
    except _ARCHIVE_ERRORS as error:
        raise BusinessException(SkillErrorCode.INVALID_PACKAGE) from error
    if subdirectory:
        validate_path(subdirectory)
        wrappers = {file.path.partition("/")[0] for file in files}
        if len(wrappers) != 1:
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        prefix = next(iter(wrappers)) + "/" + subdirectory + "/"
        files = [
            SkillFile(file.path[len(prefix) :], file.content)
            for file in files
            if file.path.startswith(prefix)
        ]
    candidates = [
        file.path.removesuffix("SKILL.md")
        for file in files
        if PurePosixPath(file.path).name == "SKILL.md"
    ]
    # 最外层入口确定技能归属，内部同名文档作为资源保留，不另建候选
    roots = [
        root
        for root in candidates
        if not any(root != parent and root.startswith(parent) for parent in candidates)
    ]
    if not roots or len(roots) > MAX_SKILLS:
        raise BusinessException(
            SkillErrorCode.INVALID_PACKAGE, message="压缩包需要包含 1 至 64 个技能"
        )
    packages = tuple(
        parse_package(
            tuple(
                SkillFile(file.path[len(root) :], file.content)
                for file in files
                if file.path.startswith(root)
            )
        )
        for root in sorted(roots)
    )
    if len({package.name for package in packages}) != len(packages):
        raise BusinessException(SkillErrorCode.NAME_CONFLICT)
    return packages


class SkillArchiveReader:
    """以最多两个工作线程解析压缩包，取消请求等待解析释放内存后退出"""

    def __init__(self) -> None:
        self._limiter = anyio.CapacityLimiter(2)

    async def read(
        self, content: bytes, *, subdirectory: str = ""
    ) -> tuple[SkillPackage, ...]:
        """解析有字节和文件数量上限的包，不在服务端执行其中的脚本"""
        return await run_sync(
            parse_archive, content, subdirectory, limiter=self._limiter
        )
