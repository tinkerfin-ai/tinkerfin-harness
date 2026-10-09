"""等待业务同步任务结算，取消请求不会释放仍被线程占用的容量"""

import asyncio
from collections.abc import Callable
from typing import TypeVar, TypeVarTuple, Unpack

import anyio
from anyio import CapacityLimiter, to_thread
from anyio.lowlevel import checkpoint

_Result = TypeVar("_Result")
_Args = TypeVarTuple("_Args")


async def run_owned_thread(
    function: Callable[[Unpack[_Args]], _Result],
    *args: *_Args,
    limiter: CapacityLimiter,
) -> _Result:
    """在指定容量内运行同步任务，完成后才交付结果或取消

    同步库不能中途停止。调用方必须限制单次工作量；本函数不伪装为可中断
    的线程超时，取消时继续等待当前任务结算，容量令牌由实际工作持有。

    Args:
        function: 具有确定工作量或自身超时的同步操作
        args: 同步操作的位置参数
        limiter: 由业务边界拥有的并发容量

    Returns:
        同步操作的返回值

    Raises:
        asyncio.CancelledError: 工作结束后传播调用方取消
        Exception: 未被取消时传播同步操作的原始错误
    """

    await checkpoint()
    work = asyncio.create_task(
        to_thread.run_sync(function, *args, limiter=limiter),
        name="studio-thread-work",
    )
    cancellation: asyncio.CancelledError | None = None
    with anyio.CancelScope(shield=True):
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except Exception:  # noqa: BLE001 - 已结束任务的错误统一通过 result 传播
                break
    if cancellation is not None:
        try:
            work.result()
        except BaseException as error:
            raise cancellation from error
        raise cancellation
    await checkpoint()
    return work.result()
