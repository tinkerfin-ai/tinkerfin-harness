"""通过工作信号验证同步操作的容量归属和取消交付，不使用真实时钟"""

import asyncio
import threading

import anyio
import pytest

from tinkerfin_studio.infrastructure.threads import run_owned_thread


@pytest.mark.parametrize("fail", [False, True])
async def test_cancel_waits_for_owned_work_and_keeps_capacity(fail: bool) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finish = threading.Event()
    limiter = anyio.CapacityLimiter(1)

    def work() -> bytes:
        loop.call_soon_threadsafe(started.set)
        finish.wait()
        if fail:
            raise ValueError("image failed")
        return b"image"

    operation = asyncio.create_task(run_owned_thread(work, limiter=limiter))
    try:
        await started.wait()
        for _ in range(2):
            operation.cancel()
            # FIFO 回调显式排在本次取消投递之后，避免用计时等待猜测线程状态
            delivered = loop.create_future()
            loop.call_soon(delivered.set_result, None)
            await delivered
            assert not operation.done()
            assert limiter.borrowed_tokens == 1
        finish.set()
        with pytest.raises(asyncio.CancelledError) as captured:
            await operation
        assert isinstance(captured.value.__cause__, ValueError) is fail
        assert limiter.borrowed_tokens == 0
    finally:
        finish.set()
        await asyncio.gather(operation, return_exceptions=True)


async def test_owned_thread_delivers_return_values_and_errors() -> None:
    limiter = anyio.CapacityLimiter(1)
    assert await run_owned_thread(bytes, b"image", limiter=limiter) == b"image"

    def fail() -> None:
        raise OSError("backup failed")

    with pytest.raises(OSError, match="backup failed"):
        await run_owned_thread(fail, limiter=limiter)
    assert limiter.borrowed_tokens == 0
