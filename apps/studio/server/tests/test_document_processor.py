"""文件生成进程在完成、超时和取消时始终归属于当前调用"""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tinkerfin_studio.attachments import documents
from tinkerfin_studio.attachments.documents import DocumentProcessor


@pytest.mark.parametrize("stop", ["success", "failure", "timeout", "cancel"])
@pytest.mark.parametrize("cancel_during_cleanup", [False, True])
async def test_processing_waits_for_owned_child_before_finishing(
    monkeypatch, stop, cancel_during_cleanup
):
    entered = asyncio.Event()
    operation = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    reaped = asyncio.Event()
    killed = []
    process = SimpleNamespace(returncode=None)

    async def communicate(_input):
        entered.set()
        await operation.wait()
        if stop == "timeout":
            raise TimeoutError("controlled deadline")
        if stop == "failure":
            process.returncode = 1
            return b'{"kind":"internal_error"}', b""
        process.returncode = 0
        return b'{"data":"ok"}', b""

    async def wait():
        cleaning.set()
        await release.wait()
        reaped.set()
        return 0

    process.communicate = communicate
    process.wait = wait
    process.kill = lambda: killed.append(True)
    monkeypatch.setattr(
        documents.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    task = asyncio.create_task(DocumentProcessor().run({"operation": "generate"}))
    try:
        await entered.wait()
        if stop == "cancel":
            task.cancel()
        else:
            operation.set()
        await cleaning.wait()
        if cancel_during_cleanup:
            task.cancel()
            delivered = asyncio.Event()
            asyncio.get_running_loop().call_soon(delivered.set)
            await delivered.wait()
        assert not task.done()
        assert not reaped.is_set()
        release.set()
        if stop == "cancel" or cancel_during_cleanup:
            with pytest.raises(asyncio.CancelledError):
                await task
        elif stop == "timeout":
            with pytest.raises(TimeoutError, match="controlled deadline"):
                await task
        elif stop == "failure":
            with pytest.raises(RuntimeError, match="文件处理进程执行失败"):
                await task
        else:
            assert await task == {"data": "ok"}
        assert reaped.is_set()
        assert bool(killed) is (stop in {"timeout", "cancel"})
    finally:
        operation.set()
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
