"""在宿主资源退出时区分主动取消和必须保留的失败"""

import asyncio


def _cleanup_failure_priority(error: BaseException) -> int:
    """区分纯关闭取消、独立清理失败和进程控制，并允许异常链包含环"""

    pending = [error]
    seen: set[int] = set()
    priority = 0
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        elif not isinstance(current, asyncio.CancelledError):
            priority = max(priority, 1 if isinstance(current, Exception) else 2)
        pending.extend(
            cause
            for cause in (current.__cause__, current.__context__)
            if cause is not None
        )
    return priority
