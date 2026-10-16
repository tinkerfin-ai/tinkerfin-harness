"""维护模型资料快照：python -m tinkerfin_studio.models.update_capabilities

只在维护时运行，不进入用户请求或消息执行链路。下载原生异步且有总时限；
校验及原子写入在命令主线程执行，失败不覆盖现有快照。
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import tempfile
from pathlib import Path

import anyio
import httpx

from tinkerfin_studio.models.capability_data import (
    MAX_SOURCE_BYTES,
    SOURCE_URL,
    canonical_json,
    parse_source,
    read_catalog,
)


async def download_source(client: httpx.AsyncClient) -> bytes:
    """下载有界模型资料，取消或失败时关闭响应流

    Args:
        client: 由维护命令拥有并关闭的异步客户端

    Returns:
        不超过 20 MiB 的原始 JSON

    Raises:
        ValueError: 下载内容超过限制
        httpx.HTTPError: 上游请求失败
        TimeoutError: 下载超过 30 秒
    """
    data = bytearray()
    with anyio.fail_after(30):
        async with client.stream("GET", SOURCE_URL) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_SOURCE_BYTES:
                    raise ValueError("模型资料超过 20 MiB")
                data.extend(chunk)
    return bytes(data)


async def _download() -> bytes:
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=False, trust_env=False
    ) as client:
        return await download_source(client)


def update_catalog(data: bytes, destination: Path) -> str:
    """校验后原子替换目录，并返回模型能力变化的可审查差异

    Args:
        data: 上游原始内容
        destination: 当前快照路径

    Returns:
        新增、删除和能力改变的统一差异

    Raises:
        ValueError: 新资料或现有目录损坏
        OSError: 文件无法读取或原子写入失败
    """
    candidate = parse_source(data)
    previous = read_catalog(destination.read_bytes()) if destination.exists() else None
    before = (
        canonical_json(previous.model_dump(mode="json")["providers"])
        if previous
        else b"{}\n"
    )
    after = canonical_json(candidate.model_dump(mode="json")["providers"])
    difference = "".join(
        difflib.unified_diff(
            before.decode().splitlines(keepends=True),
            after.decode().splitlines(keepends=True),
            fromfile="current",
            tofile="candidate",
        )
    )
    encoded = canonical_json(candidate.model_dump(mode="json"))
    read_catalog(encoded)
    descriptor, name = tempfile.mkstemp(prefix=".capability-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
    return difference


def main() -> None:
    """读取本地源文件或下载资料，输出差异并更新当前快照"""
    parser = argparse.ArgumentParser(description="更新 Studio 离线模型输入能力目录")
    parser.add_argument(
        "--source", type=Path, help="本地 models.dev JSON 文件；留空时下载"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("capability_catalog.json"),
    )
    args = parser.parse_args()
    if args.source is not None:
        with args.source.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    else:
        data = asyncio.run(_download())
    print(update_catalog(data, args.output) or "模型能力无变化")


if __name__ == "__main__":
    main()
