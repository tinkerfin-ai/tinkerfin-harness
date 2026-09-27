"""Settle accepted cleanup before restoring cancellation to its caller."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from ._failures import _select_failure


async def _capture_cleanup(operation: Awaitable[None]) -> BaseException | None:
    try:
        await operation
    except BaseException as error:  # noqa: BLE001 - process control belongs to the joining owner
        return error
    return None


async def _close_owned(operation: Awaitable[None]) -> None:
    task = asyncio.create_task(
        _capture_cleanup(operation), name="tinkerfin-gateway-cleanup"
    )
    await _join_cleanup(task)


async def _join_cleanup(task: asyncio.Task[BaseException | None]) -> None:
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = error
    error = task.result()
    if error is not None:
        raise error if cancelled is None else _select_failure(cancelled, error)
    if cancelled is not None:
        raise cancelled


__all__ = ["_capture_cleanup", "_close_owned", "_join_cleanup"]
