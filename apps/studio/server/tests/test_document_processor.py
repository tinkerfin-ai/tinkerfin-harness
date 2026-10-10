"""文件生成进程在完成、超时和取消时始终归属于当前调用"""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tinkerfin_studio.attachments import documents
from tinkerfin_studio.attachments.documents import DocumentProcessor


async def test_cancel_preserves_failure_while_reaping_child(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    failure = OSError("reaping failed")

    async def communicate(_input):
        entered.set()
        await release.wait()
        return b"{}", b""

    async def wait():
        raise failure

    process = SimpleNamespace(
        returncode=None, communicate=communicate, wait=wait, kill=lambda: None
    )
    monkeypatch.setattr(
        documents.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    task = asyncio.create_task(DocumentProcessor().run({"operation": "generate"}))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.__cause__ is failure
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_truncated_image_is_a_recoverable_input_error(large_image):
    with pytest.raises(ValueError, match="参数不符合要求"):
        await DocumentProcessor().run(
            {
                "operation": "preview_image",
                "data": base64.b64encode(large_image[:2_500_000]).decode("ascii"),
                "max_bytes": 1_000_000,
            }
        )
