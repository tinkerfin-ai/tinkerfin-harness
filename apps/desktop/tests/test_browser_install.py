"""浏览器选择、完整安装、共享准备与取消清理的可观察契约"""

import asyncio
import hashlib
import importlib.resources
import io
import json
import plistlib
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import httpx
import pytest

from tinkerfin_companion import browser_install

RELEASE = "154.0.8037.57"
URL = (
    "https://storage.googleapis.com/chrome-for-testing-public/"
    f"{RELEASE}/mac-arm64/chrome-mac-arm64.zip"
)
APPLICATION = "chrome-mac-arm64/Google Chrome for Testing.app"
PROGRAM = APPLICATION + "/Contents/MacOS/Google Chrome for Testing"


def archive_bytes(*, extra: tuple[str, bytes, int] | None = None) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            APPLICATION + "/Contents/Info.plist",
            plistlib.dumps(
                {
                    "CFBundleExecutable": "Google Chrome for Testing",
                    "CFBundleShortVersionString": RELEASE,
                }
            ),
        )
        executable = ZipInfo(PROGRAM)
        executable.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(executable, b"synthetic executable")
        if extra is not None:
            name, value, mode = extra
            entry = ZipInfo(name)
            entry.external_attr = mode << 16
            archive.writestr(entry, value)
    return output.getvalue()


@pytest.fixture
def release_files(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    original = importlib.resources.files

    def resources(anchor):
        return package if anchor == "tinkerfin_companion" else original(anchor)

    monkeypatch.setattr(importlib.resources, "files", resources)
    monkeypatch.setattr(browser_install, "SYSTEM_APPLICATIONS", tmp_path / "system")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))

    def prepare(content):
        (package / "browser_release.json").write_text(
            json.dumps(
                {
                    "version": RELEASE,
                    "url": URL,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            )
        )

    return prepare


class Download(httpx.AsyncByteStream):
    def __init__(self, content: bytes, *, blocked: bool = False) -> None:
        self.content = content
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.closed = asyncio.Event()
        self.error: Exception | None = None
        if not blocked:
            self.proceed.set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await self.proceed.wait()
        if self.error is not None:
            raise self.error
        yield self.content

    async def aclose(self) -> None:
        self.closed.set()


def transport(monkeypatch, download: Download):
    requests: list[httpx.Request] = []
    original = httpx.AsyncClient

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == URL
        return httpx.Response(200, stream=download)

    def client(**kwargs):
        return original(
            transport=httpx.MockTransport(handle), trust_env=False, **kwargs
        )

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requests


@pytest.mark.parametrize("location", ["system", "user/Applications"])
async def test_installed_browser_is_preferred_without_download(
    tmp_path, monkeypatch, release_files, location
):
    release_files(archive_bytes())
    bundle = tmp_path / location / "Google Chrome.app"
    executable = bundle / "Contents/MacOS/Google Chrome"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic installed browser")
    executable.chmod(0o755)
    (bundle / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleExecutable": "Google Chrome",
                "CFBundleShortVersionString": "155.0.8059.82",
            }
        )
    )

    def no_client(**kwargs):
        raise AssertionError("system browser must not require a download")

    async def no_prepare():
        raise AssertionError("system browser must not show preparation")

    monkeypatch.setattr(httpx, "AsyncClient", no_client)
    cache = tmp_path / "cache"
    provider = browser_install.BrowserProvider(cache, on_prepare=no_prepare)
    assert await provider.resolve() == executable.resolve()
    assert not cache.exists()


async def test_concurrent_resolution_publishes_one_complete_browser_and_reuses_cache(
    tmp_path, monkeypatch, release_files
):
    content = archive_bytes(
        extra=(
            APPLICATION + "/Contents/browser-link",
            b"MacOS/Google Chrome for Testing",
            stat.S_IFLNK | 0o777,
        )
    )
    release_files(content)
    download = Download(content, blocked=True)
    requests = transport(monkeypatch, download)
    prepared: list[str] = []

    async def prepare():
        prepared.append("preparing")

    cache = tmp_path / "cache"
    provider = browser_install.BrowserProvider(cache, on_prepare=prepare)
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(provider.resolve()) for _ in range(2)]
        await download.started.wait()
        assert not list(cache.glob("chrome-*"))
        download.proceed.set()
    first, second = (task.result() for task in tasks)
    assert first == second
    assert first.read_bytes() == b"synthetic executable"
    assert first.stat().st_mode & 0o111
    installed = next(cache.glob("chrome-*"))
    assert (installed / APPLICATION / "Contents/browser-link").resolve() == first
    assert await provider.resolve() == first
    assert len(requests) == 1
    assert prepared == ["preparing"]
    assert download.closed.is_set()
    assert not list(cache.glob(".download-*"))


async def test_cancelling_all_waiters_closes_download_and_removes_partial_installation(
    tmp_path, monkeypatch, release_files
):
    content = archive_bytes()
    release_files(content)
    download = Download(content, blocked=True)
    transport(monkeypatch, download)
    cache = tmp_path / "cache"
    provider = browser_install.BrowserProvider(cache)
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(provider.resolve()) for _ in range(2)]
        await download.started.wait()
        for task in tasks:
            task.cancel()
    assert all(task.cancelled() for task in tasks)
    assert download.closed.is_set()
    assert list(cache.iterdir()) == []


async def test_interrupted_transfer_cleans_up_and_allows_a_new_resolution(
    tmp_path, monkeypatch, release_files
):
    content = archive_bytes()
    release_files(content)
    download = Download(content)
    download.error = httpx.ReadTimeout("synthetic transfer interruption")
    requests = transport(monkeypatch, download)
    cache = tmp_path / "cache"
    provider = browser_install.BrowserProvider(cache)
    with pytest.raises(httpx.ReadTimeout):
        await provider.resolve()
    assert download.closed.is_set()
    assert list(cache.iterdir()) == []
    download.error = None
    assert (await provider.resolve()).read_bytes() == b"synthetic executable"
    assert len(requests) == 2


@pytest.mark.parametrize("invalid", ["digest", "size", "path", "link"])
async def test_invalid_download_never_publishes_or_leaves_partial_files(
    tmp_path, monkeypatch, release_files, invalid
):
    content = archive_bytes()
    if invalid == "path":
        content = archive_bytes(extra=("../outside", b"payload", stat.S_IFREG | 0o644))
    elif invalid == "link":
        content = archive_bytes(
            extra=("outside", b"../../outside", stat.S_IFLNK | 0o777)
        )
    release_files(content)
    if invalid == "digest":
        content = b"x" + content[1:]
    elif invalid == "size":
        content += b"x"
    download = Download(content)
    transport(monkeypatch, download)
    cache = tmp_path / "cache"
    with pytest.raises(ValueError):
        await browser_install.BrowserProvider(cache).resolve()
    assert download.closed.is_set()
    assert list(cache.iterdir()) == []
    assert not (tmp_path / "outside").exists()
