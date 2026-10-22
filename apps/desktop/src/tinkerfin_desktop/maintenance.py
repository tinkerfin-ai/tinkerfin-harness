"""本机更新准备的所有权与回执，退出进程前保持维护接纳门禁"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import JsonValue

from .host import CompanionHost
from .lifecycle import settle
from .native import MaintenanceCancel, MaintenanceCommit, MaintenancePrepare

Receipt = Literal["waiting", "ready", "cancelled", "failed", "committed"]


class MaintenanceControl:
    """拥有一项本机更新等待；宿主关闭后才能结束已提交的维护上下文"""

    def __init__(
        self,
        host: CompanionHost,
        send: Callable[[dict[str, JsonValue]], Awaitable[None]],
    ) -> None:
        self._host = host
        self._send = send
        self._task: asyncio.Task[None] | None = None
        self._request: str | None = None
        self._state: Receipt = "cancelled"
        self._hold = asyncio.Event()

    async def _report(self, request: str, state: Receipt) -> None:
        await self._send({"kind": "maintenance", "request_id": request, "state": state})

    async def _prepare(self, request: str) -> None:
        try:
            async with self._host.maintenance():
                self._state = "ready"
                await self._report(request, "ready")
                await self._hold.wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 原生进程边界只发布稳定状态，不传递底层异常
            self._state = "failed"
            await self._report(request, "failed")

    async def receive(
        self, message: MaintenancePrepare | MaintenanceCancel | MaintenanceCommit
    ) -> bool:
        """返回是否已确认退出工作进程；迟到消息不能结束另一项更新等待"""
        request = message.request_id
        if isinstance(message, MaintenancePrepare):
            if request == self._request:
                if self._state != "waiting":
                    await self._report(request, self._state)
            elif self._task is not None and not self._task.done():
                await self._report(request, "failed")
            else:
                if self._task is not None:
                    self._task.result()
                self._request = request
                self._state = "waiting"
                self._task = asyncio.create_task(self._prepare(request))
            return False
        if request != self._request:
            await self._report(request, "failed")
            return False
        if isinstance(message, MaintenanceCancel):
            if self._state == "committed":
                await self._report(request, "committed")
                return False
            await self.close()
            self._state = "cancelled"
            await self._report(request, "cancelled")
            return False
        if self._state != "ready":
            await self._report(request, "failed")
            return False
        # 这里只要求主循环退出，不能提前离开维护上下文而重新接纳工作
        self._state = "committed"
        await self._report(request, "committed")
        return True

    async def close(self) -> None:
        """取消时恢复接纳；安装提交时必须由调用者先关闭宿主再调用此方法"""
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            results = await settle(asyncio.gather(task, return_exceptions=True))
            for result in results:
                if isinstance(result, Exception):
                    raise result
