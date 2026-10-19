"""原生宿主关闭时等待已接收的资源清理并保留取消"""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

Value = TypeVar("Value")


async def settle(
    operation: Awaitable[Value], *, primary_error: BaseException | None = None
) -> Value:
    """等待已受理清理，清理失败也不能覆盖主体取消或进程控制异常"""
    task = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            # Python 3.14 的 shield 在等待者取消后会输出子任务的原始异常
            # wait 同样不取消被等待任务，并由此处独占处理结果与私有异常
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    try:
        result = task.result()
    except (Exception, asyncio.CancelledError) as error:
        if primary_error is not None and not isinstance(primary_error, Exception):
            raise primary_error from error
        if cancellation is not None:
            raise cancellation from error
        raise
    if cancellation is not None:
        raise cancellation
    return result
