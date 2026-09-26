"""限制需要任务轨迹的初始查询并发与等待时间"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anyio import CapacityLimiter


class HistoryQueryTimeout(TimeoutError):
    """历史初始查询在排队或执行期间超过业务时限"""


class HistoryQueryAdmission:
    """应用共享的初始查询容量，不持有长期订阅或框架投影状态"""

    def __init__(self, *, capacity: int = 2, timeout_seconds: float = 30.0) -> None:
        if capacity < 1 or timeout_seconds <= 0:
            raise ValueError("初始查询容量与时限必须大于零")
        self._limiter = CapacityLimiter(capacity)
        self._timeout_seconds = timeout_seconds

    @property
    def borrowed_tokens(self) -> int:
        """返回当前占用初始查询容量的请求数"""
        return self._limiter.borrowed_tokens

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        """在含排队的时限内占用容量，退出或取消时立即归还"""
        deadline = asyncio.timeout(self._timeout_seconds)
        try:
            async with deadline, self._limiter:
                yield
        except TimeoutError:
            if not deadline.expired():
                raise
            raise HistoryQueryTimeout("历史初始查询超时") from None
