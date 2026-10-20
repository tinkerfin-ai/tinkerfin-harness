"""可取消、有超时和并发限制的文件生成与图片处理"""

from __future__ import annotations

import asyncio
import json
import sys

import anyio
from pydantic import JsonValue, TypeAdapter

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class DocumentProcessor:
    """拥有每次文件处理子进程，超时或取消时终止并等待回收，最多并行两个"""

    def __init__(self) -> None:
        self._capacity = anyio.CapacityLimiter(2)

    async def run(self, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """处理有界文件生成或图片预览任务，不阻塞 HTTP 或 Agent 事件循环"""
        async with self._capacity:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tinkerfin_studio.attachments.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            cancelled: asyncio.CancelledError | None = None
            try:
                with anyio.fail_after(30):
                    stdout, _ = await process.communicate(
                        json.dumps(payload, ensure_ascii=False).encode()
                    )
                try:
                    result = _JSON_OBJECT.validate_json(stdout)
                except ValueError as error:
                    raise RuntimeError("文件处理进程未返回有效结果") from error
                if process.returncode != 0:
                    if result.get("kind") == "invalid_input":
                        raise ValueError("文件生成或图片预览参数不符合要求")
                    raise RuntimeError("文件处理进程执行失败")
                return result
            except asyncio.CancelledError as error:
                cancelled = error
                raise
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        # 进程可能刚好自然退出，仍须等待系统回收
                        pass

                async def reap() -> BaseException | None:
                    try:
                        await process.wait()
                    except BaseException as error:  # noqa: BLE001 - 回收错误交回本次调用，保持取消和异常链
                        return error
                    return None

                # 此次调用拥有回收任务。兼顾任务取消和取消作用域，回收结束后再传播取消。
                closing = asyncio.create_task(reap())
                with anyio.CancelScope(shield=True):
                    while not closing.done():
                        try:
                            await asyncio.shield(closing)
                        except asyncio.CancelledError as error:
                            cancelled = cancelled or error
                cleanup_error = closing.result()
                if cancelled is not None:
                    if cleanup_error is not None:
                        raise cancelled from cleanup_error
                    raise cancelled
                if cleanup_error is not None:
                    raise cleanup_error
