"""请求断连时取消主动网络操作并等待资源清理"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import anyio
from fastapi import Request

from tinkerfin_studio.infrastructure._failures import _cleanup_failure_priority

_ResultT = TypeVar("_ResultT")


async def connected_operation(
    request: Request, operation: Callable[[], Awaitable[_ResultT]]
) -> _ResultT:
    """浏览器断连时取消并等待本次主动请求关闭连接"""

    async def watch_disconnect() -> None:
        while not await request.is_disconnected():
            await asyncio.sleep(0.1)

    async def execute() -> _ResultT:
        return await operation()

    operation_task = asyncio.create_task(execute(), name="studio-connected-operation")
    disconnected = asyncio.create_task(
        watch_disconnect(), name="studio-disconnect-watch"
    )
    primary: BaseException | None = None
    try:
        done, _ = await asyncio.wait(
            (operation_task, disconnected), return_when=asyncio.FIRST_COMPLETED
        )
        if operation_task not in done:
            raise asyncio.CancelledError()
        return await operation_task
    except BaseException as error:
        primary = error
        raise
    finally:
        tasks = (operation_task, disconnected)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        cancellation = primary if isinstance(primary, asyncio.CancelledError) else None
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    await asyncio.wait(tasks)
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
                    if any(not task.done() for task in tasks):
                        continue
                break
        failures: list[BaseException] = [] if primary is None else [primary]
        if cancellation is not None and cancellation is not primary:
            failures.append(cancellation)
        for task in tasks:
            try:
                task.result()
            except BaseException as error:  # noqa: BLE001 - 收齐后一次性交付原始失败
                if error is not primary and _cleanup_failure_priority(error):
                    failures.append(error)
        if failures:
            chosen = next(
                (error for error in failures if _cleanup_failure_priority(error) == 2),
                cancellation or primary or failures[0],
            )
            remaining = [error for error in failures if error is not chosen]
            if remaining:
                secondary = (
                    remaining[0]
                    if len(remaining) == 1
                    else BaseExceptionGroup("网络请求与客户端清理同时失败", remaining)
                )
                try:
                    raise secondary
                except BaseException:  # noqa: BLE001 - 保留取消及各异常原始cause
                    raise chosen
            raise chosen
