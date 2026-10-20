"""校验云端技能原始字节并为当前执行准备只读输入"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import unicodedata
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .configuration import SkillReference
from .lifecycle import settle

if TYPE_CHECKING:
    from tinkerfin_remote import WorkspaceRequest

    from .cloud import StudioClient

Value = TypeVar("Value")
MAX_SKILL_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024


class SkillFile(BaseModel):
    """与云端技能下载一致的相对路径和 base64 原始字节"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=((8 * 1024 * 1024 + 2) // 3) * 4)


class SkillDownload(BaseModel):
    """单个已授权技能的完整内容，必须核验会话保存的摘要"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=64)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[SkillFile, ...] = Field(min_length=1, max_length=1024)


class FileContent(BaseModel):
    """云端返回的附件原件，内容类型只描述实际字节"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    mime_type: str
    content: str


def _decode(
    package: SkillDownload, reference: SkillReference
) -> tuple[tuple[str, bytes], ...]:
    name = package.name
    if (
        package.name != reference.name
        or package.digest != reference.digest
        or name.startswith("-")
        or name.endswith("-")
        or "--" in name
        or not all(
            char == "-" or (char.isalpha() and char.islower()) or char.isdigit()
            for char in name
        )
    ):
        raise ValueError("技能身份与会话授权不一致")
    paths: set[str] = set()
    files: list[tuple[str, bytes]] = []
    total = 0
    for file in sorted(package.files, key=lambda file: file.path):
        path = file.path
        key = unicodedata.normalize("NFC", path).casefold()
        if (
            key in paths
            or "\\" in path
            or ":" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or PurePosixPath(path).is_absolute()
        ):
            raise ValueError("技能路径包含穿越、重名或不支持的文件名")
        paths.add(key)
        content = base64.b64decode(file.content, validate=True)
        total += len(content)
        if len(content) > 8 * 1024 * 1024 or total > MAX_SKILL_BYTES:
            raise ValueError("技能内容超过本机容量限制")
        files.append((path, content))
    if any(
        str(parent) in paths for path in paths for parent in PurePosixPath(path).parents
    ):
        raise ValueError("技能文件与目录冲突")
    if not any(
        path == "SKILL.md" and len(content) <= 1024 * 1024 for path, content in files
    ):
        raise ValueError("技能缺少有效的 SKILL.md")
    digest = hashlib.sha256()
    for path, content in files:
        encoded = path.encode()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    if digest.hexdigest() != reference.digest:
        raise ValueError("技能文件摘要不匹配")
    return tuple(files)


class SkillInputs:
    """借用按执行作用域和内容摘要定位的只读技能目录

    文件系统操作最多使用两个线程；取消等待已开始的操作和清理结束。
    同宿主的并发借用共享完整目录，最后一次退出只清理本次创建的目录。
    预先存在的目录必须逐文件核验，作为借用资源保留。
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._slots = asyncio.Semaphore(2)
        self._lock = asyncio.Lock()
        self._leases: dict[str, int] = {}
        self._owned: set[str] = set()

    async def _disk(self, operation: Callable[[], Value]) -> Value:
        async with self._slots:
            return await settle(asyncio.to_thread(operation))

    @staticmethod
    def _publish(
        directory: Path, files: tuple[tuple[str, bytes], ...], allocated: list[Path]
    ) -> None:
        if directory.is_symlink():
            raise ValueError("技能目录不能是符号链接")
        if directory.exists():
            expected = {path: content for path, content in files}
            observed: set[str] = set()
            directories = {
                str(parent)
                for path in expected
                for parent in PurePosixPath(path).parents
                if str(parent) != "."
            }
            for entry in directory.rglob("*"):
                relative = entry.relative_to(directory).as_posix()
                if entry.is_symlink():
                    raise ValueError("技能目录包含符号链接")
                if entry.is_dir():
                    if relative not in directories:
                        raise ValueError("技能目录包含未授权内容")
                elif entry.is_file() and relative in expected:
                    content = expected[relative]
                    with entry.open("rb") as stream:
                        actual = stream.read(len(content) + 1)
                    if actual != content:
                        raise ValueError("技能文件与已授权摘要不一致")
                    observed.add(relative)
                else:
                    raise ValueError("技能目录包含未授权内容")
            if observed != set(expected):
                raise ValueError("技能目录不完整")
            return
        directory.mkdir(mode=0o700)
        allocated.append(directory)
        for path, content in files:
            destination = directory / path
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with os.fdopen(
                os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                ),
                "wb",
            ) as stream:
                stream.write(content)

    async def _release(self, key: str) -> None:
        async with self._lock:
            remaining = self._leases[key] - 1
            if remaining:
                self._leases[key] = remaining
                return
            del self._leases[key]
            if key in self._owned:
                self._owned.remove(key)
                await self._disk(lambda: shutil.rmtree(self.directory / key))

    @asynccontextmanager
    async def prepare(
        self,
        cloud: StudioClient,
        request: WorkspaceRequest,
        references: tuple[SkillReference, ...],
    ) -> AsyncIterator[Path | None]:
        """核验全部技能后才提供目录；失败保留借用文件并清理本次分配"""
        if not references:
            yield None
            return
        names = [
            unicodedata.normalize("NFC", reference.name).casefold()
            for reference in references
        ]
        if len(names) != len(set(names)):
            raise ValueError("技能名称在本机文件系统中重复")
        identity = [
            request.identity.namespace,
            request.authorization,
            request.workspace_scope_id,
            sorted((reference.name, reference.digest) for reference in references),
        ]
        key = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False).encode()
        ).hexdigest()
        files: list[tuple[str, bytes]] = []
        total = 0
        for reference in references:
            package = await cloud.skill(request, reference.installation_id)
            decoded = await self._disk(
                lambda package=package, reference=reference: _decode(package, reference)
            )
            total += sum(len(content) for _path, content in decoded)
            if total > MAX_INPUT_BYTES:
                raise ValueError("本轮技能总量超过 64 MiB")
            files.extend(
                (reference.name + "/" + path, content) for path, content in decoded
            )
        allocated: list[Path] = []
        directory = self.directory / key
        borrowed = False
        try:
            async with self._lock:
                try:
                    if key not in self._leases:
                        await self._disk(
                            lambda: self._publish(directory, tuple(files), allocated)
                        )
                        self._leases[key] = 0
                        if allocated:
                            self._owned.add(key)
                    self._leases[key] += 1
                    borrowed = True
                except BaseException:
                    for owned in allocated:
                        await settle(
                            self._disk(lambda owned=owned: shutil.rmtree(owned))
                        )
                    raise
            yield directory
        finally:
            if borrowed:
                await settle(self._release(key))
