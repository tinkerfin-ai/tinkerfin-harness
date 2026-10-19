"""资源清理保留主体取消，私有失败不进入事件循环诊断日志"""

import asyncio

import pytest

from tinkerfin_companion.lifecycle import settle


async def test_cancelled_cleanup_waits_for_failure_without_logging_private_context(
    caplog,
):
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("private-cleanup-context")

    async def cleanup():
        entered.set()
        await release.wait()
        raise failure

    owner = asyncio.create_task(settle(cleanup()))
    await entered.wait()
    owner.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await owner
    assert owner.cancelled()
    assert cancelled.value.__cause__ is failure
    assert "private-cleanup-context" not in caplog.text


@pytest.mark.parametrize("primary", [asyncio.CancelledError, GeneratorExit, SystemExit])
async def test_cleanup_failure_preserves_existing_control_exception(primary):
    original = primary()
    failure = RuntimeError("cleanup failed")
    completed = asyncio.get_running_loop().create_future()
    completed.set_exception(failure)
    with pytest.raises(primary) as propagated:
        await settle(completed, primary_error=original)
    assert propagated.value is original
    assert propagated.value.__cause__ is failure
