"""优先借用已安装浏览器；缺少时准备经过摘要校验的独立 Chrome"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
import os
import plistlib
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import BinaryIO, TypeVar
from zipfile import ZipFile

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .lifecycle import settle

SYSTEM_APPLICATIONS = Path("/Applications")
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 20000
DOWNLOAD_DEADLINE_SECONDS = 3600
_Value = TypeVar("_Value")


class BrowserRelease(BaseModel):
    """开发者固定的 Chrome for Testing 下载清单"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: str = Field(pattern=r"^\d+\.\d+\.\d+\.\d+$")
    url: str = Field(
        pattern=r"^https://storage\.googleapis\.com/chrome-for-testing-public/\d+\.\d+\.\d+\.\d+/mac-arm64/chrome-mac-arm64\.zip$"
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=512 * 1024 * 1024)


def _release() -> BrowserRelease:
    release = BrowserRelease.model_validate_json(
        importlib.resources.files("tinkerfin_companion")
        .joinpath("browser_release.json")
        .read_bytes()
    )
    if release.url != (
        "https://storage.googleapis.com/chrome-for-testing-public/"
        f"{release.version}/mac-arm64/chrome-mac-arm64.zip"
    ):
        raise ValueError("浏览器下载地址与固定版本不一致")
    return release


def _application(
    bundle: Path, executable: str, *, version: str, exact: bool = False
) -> Path | None:
    try:
        manifest = bundle / "Contents/Info.plist"
        if manifest.stat().st_size > 1024 * 1024:
            return None
        metadata = plistlib.loads(manifest.read_bytes())
        if not isinstance(metadata, dict):
            return None
        installed = metadata.get("CFBundleShortVersionString")
        if not isinstance(installed, str):
            return None
        parts = installed.split(".")
        if len(parts) != 4 or any(not part.isdecimal() for part in parts):
            return None
        if exact and installed != version:
            return None
        if not exact and int(parts[0]) < int(version.split(".")[0]):
            return None
        program = bundle / "Contents/MacOS" / executable
        if metadata.get("CFBundleExecutable") != executable:
            return None
        if program.is_file() and os.access(program, os.X_OK):
            return program.resolve(strict=True)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return None


def _system_browser(release: BrowserRelease) -> Path | None:
    for directory in (SYSTEM_APPLICATIONS, Path.home() / "Applications"):
        for name, executable in (
            ("Google Chrome.app", "Google Chrome"),
            ("Chromium.app", "Chromium"),
        ):
            program = _application(
                directory / name, executable, version=release.version
            )
            if program is not None:
                return program
    return None


def _cached_browser(directory: Path, release: BrowserRelease) -> Path | None:
    if directory.is_symlink():
        raise ValueError("浏览器缓存不能指向其他目录")
    if not directory.exists():
        return None
    receipt = directory / "release.json"
    if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 4096:
        raise ValueError("浏览器缓存不完整")
    if BrowserRelease.model_validate_json(receipt.read_bytes()) != release:
        raise ValueError("浏览器缓存与固定发行不一致")
    program = _application(
        directory / "chrome-mac-arm64/Google Chrome for Testing.app",
        "Google Chrome for Testing",
        version=release.version,
        exact=True,
    )
    if program is None or not program.is_relative_to(directory.resolve(strict=True)):
        raise ValueError("浏览器缓存缺少可执行程序")
    return program


def _extract(archive: Path, directory: Path, release: BrowserRelease) -> None:
    """先验证目录，再提取普通文件，最后建立仅指向包内目标的链接"""
    directory.mkdir()
    with ZipFile(archive) as content:
        entries = content.infolist()
        if len(entries) > MAX_ARCHIVE_FILES or (
            sum(entry.file_size for entry in entries) > MAX_EXTRACTED_BYTES
        ):
            raise ValueError("浏览器压缩包超过解压容量")
        names: set[str] = set()
        files: set[str] = set()
        links: list[tuple[Path, str]] = []
        for entry in entries:
            name = entry.filename.removesuffix("/")
            parts = name.split("/")
            key = unicodedata.normalize("NFC", name).casefold()
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if (
                not name
                or "\\" in name
                or ":" in name
                or any(part in {"", ".", ".."} for part in parts)
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or key in names
                or kind not in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}
            ):
                raise ValueError("浏览器压缩包包含不安全的路径或文件类型")
            names.add(key)
            if not entry.is_dir():
                files.add(key)
            if stat.S_ISLNK(mode):
                if entry.file_size > 4096:
                    raise ValueError("浏览器压缩包链接超过容量")
                target = content.read(entry).decode("utf-8")
                if not target or "\\" in target or PurePosixPath(target).is_absolute():
                    raise ValueError("浏览器压缩包包含外部链接")
                path = directory / name
                destination = Path(os.path.normpath(path.parent / target))
                if not destination.is_relative_to(directory):
                    raise ValueError("浏览器压缩包包含外部链接")
                links.append((path, target))
        if any(
            str(parent) in files
            for name in names
            for parent in PurePosixPath(name).parents
        ):
            raise ValueError("浏览器压缩包的文件与目录冲突")
        for entry in entries:
            path = directory / entry.filename
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with content.open(entry) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target, length=256 * 1024)
            path.chmod((mode & 0o777) or 0o644)
        for path, target in links:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        for path, _ in links:
            if not path.resolve(strict=True).is_relative_to(directory):
                raise ValueError("浏览器压缩包链接越过安装目录")
    (directory / "release.json").write_text(release.model_dump_json() + "\n")
    if _cached_browser(directory, release) is None:
        raise ValueError("浏览器压缩包缺少完整应用")


class BrowserProvider:
    """共享浏览器准备任务；最后一个等待者取消后等待下载和临时文件清理

    文件操作只占用一个工作线程。取消不能中止已进入的文件操作，必须等其
    结束后清理。缓存由宿主保护为工具只读；此处不访问日常浏览器资料目录。
    """

    def __init__(
        self, cache: Path, *, on_prepare: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        self._cache = cache
        self._on_prepare = on_prepare
        self._lock = asyncio.Lock()
        self._worker = asyncio.Semaphore(1)
        self._task: asyncio.Task[Path] | None = None
        self._waiters = 0
        self._closing: asyncio.Event | None = None

    async def _disk(
        self,
        operation: Callable[[], _Value],
        *,
        primary_error: BaseException | None = None,
    ) -> _Value:
        async with self._worker:
            return await settle(
                asyncio.to_thread(operation), primary_error=primary_error
            )

    async def resolve(self) -> Path:
        """返回可用浏览器；系统安装优先，仅首次实际下载通知准备状态"""
        release = await self._disk(_release)
        program = await self._disk(lambda: _system_browser(release))
        if program is not None:
            return program
        while True:
            async with self._lock:
                closing = self._closing
                if closing is None:
                    if self._task is None:
                        self._task = asyncio.create_task(self._prepare(release))
                    task = self._task
                    self._waiters += 1
                    break
            await closing.wait()
        error: BaseException | None = None
        try:
            await asyncio.wait((task,))
            return task.result()
        except BaseException as caught:
            error = caught
            raise
        finally:
            async with self._lock:
                self._waiters -= 1
                final = self._waiters == 0
                if final:
                    closing = self._closing = asyncio.Event()
                    self._task = None
                    if not task.done():
                        task.cancel()
            if final:
                try:
                    await settle(
                        asyncio.gather(task, return_exceptions=True),
                        primary_error=error,
                    )
                finally:
                    async with self._lock:
                        self._closing = None
                        assert closing is not None
                        closing.set()

    async def _prepare(self, release: BrowserRelease) -> Path:
        def directory() -> Path:
            if self._cache.is_symlink():
                raise ValueError("浏览器缓存不能指向其他目录")
            self._cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            return self._cache.resolve(strict=True)

        root = await self._disk(directory)
        target = root / f"chrome-{release.version}-{release.sha256[:12]}"
        cached = await self._disk(lambda: _cached_browser(target, release))
        if cached is not None:
            return cached
        if self._on_prepare is not None:
            await self._on_prepare()
        temporary: Path | None = None

        def create_temporary() -> None:
            nonlocal temporary
            temporary = Path(tempfile.mkdtemp(prefix=".download-", dir=root))

        error: BaseException | None = None
        try:
            await self._disk(create_temporary)
            assert temporary is not None
            archive = temporary / "browser.zip"
            await self._download(archive, release)
            extracted = temporary / "content"
            await self._disk(lambda: _extract(archive, extracted, release))

            def publish() -> Path:
                existing = _cached_browser(target, release)
                if existing is not None:
                    return existing
                try:
                    extracted.rename(target)
                except OSError:
                    existing = _cached_browser(target, release)
                    if existing is None:
                        raise
                    return existing
                program = _cached_browser(target, release)
                assert program is not None
                return program

            return await self._disk(publish)
        except BaseException as caught:
            error = caught
            raise
        finally:
            if temporary is not None:
                owned = temporary
                await self._disk(lambda: shutil.rmtree(owned), primary_error=error)

    async def _download(self, archive: Path, release: BrowserRelease) -> None:
        digest = hashlib.sha256()
        received = 0
        stream: BinaryIO | None = None

        def open_stream() -> None:
            nonlocal stream
            stream = archive.open("xb")

        error: BaseException | None = None
        try:
            await self._disk(open_stream)
            assert stream is not None
            output = stream
            async with (
                asyncio.timeout(DOWNLOAD_DEADLINE_SECONDS),
                httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=httpx.Timeout(30, connect=15),
                    headers={"Accept-Encoding": "identity"},
                ) as client,
                client.stream("GET", release.url) as response,
            ):
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError("浏览器下载未返回完整文件")
                declared_size = response.headers.get("Content-Length")
                if (
                    declared_size is not None
                    and int(declared_size) != release.size_bytes
                ):
                    raise ValueError("浏览器下载大小与固定文件不符")
                async for chunk in response.aiter_bytes(256 * 1024):
                    received += len(chunk)
                    if received > release.size_bytes:
                        raise ValueError("浏览器下载超过固定文件大小")
                    digest.update(chunk)
                    await self._disk(lambda: output.write(chunk))
        except BaseException as caught:
            error = caught
            raise
        finally:
            if stream is not None:
                await self._disk(stream.close, primary_error=error)
        if received != release.size_bytes or digest.hexdigest() != release.sha256:
            raise ValueError("浏览器下载摘要或大小不符")
