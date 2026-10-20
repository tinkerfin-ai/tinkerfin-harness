"""按内容摘要共享官方运行时缓存，不缓存应用源码或生成的安装包"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeArchive:
    """固定平台的官方分发输入，摘要同时作为缓存身份和完整性校验"""

    name: str
    version: str
    url: str
    sha256: str


PYTHON = RuntimeArchive(
    "python",
    "3.11.14",
    "https://github.com/astral-sh/python-build-standalone/releases/download/20251031/"
    "cpython-3.11.14%2B20251031-aarch64-apple-darwin-pgo%2Blto-full.tar.zst",
    "da32f9b54c581c203d96f55d802aca5adce2d1e72da532b7b1c83d40a4cd89aa",
)
NODE = RuntimeArchive(
    "node",
    "22.23.3",
    "https://nodejs.org/dist/v22.23.3/node-v22.23.3-darwin-arm64.tar.gz",
    "23b25245dcfb9af7262f8ff142e9e2e0af025368117329e7a7458a51e5922f53",
)
DEFAULT_CACHE = Path.home() / "Library/Caches/TinkerFin/build-inputs"


def digest(path: Path) -> str:
    """按流计算文件摘要，避免把运行时压缩包全部载入内存"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def obtain(archive: RuntimeArchive, cache: Path) -> Path:
    """取得已核验输入；同一摘要串行下载，失败或中断后不发布半成品

    构建是同步 CLI，网络读取每次最多等待 30 秒，单个输入限 256 MiB。
    每次使用都重新校验摘要；损坏缓存明确报错，由开发者移除后重新下载。
    """
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / archive.sha256
    with (cache / f"{archive.sha256}.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            if target.is_symlink() or digest(target) != archive.sha256:
                raise ValueError(f"运行时缓存损坏，请移除后重试：{target}")
            return target
        with tempfile.NamedTemporaryFile(dir=cache, prefix=".download-") as temporary:
            request = urllib.request.Request(
                archive.url, headers={"User-Agent": "TinkerFin-build"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 256 * 1024 * 1024:
                        raise ValueError("运行时下载超过容量")
                    temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
            if digest(Path(temporary.name)) != archive.sha256:
                raise ValueError(f"{archive.name} 下载摘要不符，未写入缓存")
            # 硬链接原子发布，临时文件由上下文独立清理
            os.link(temporary.name, target)
        return target
