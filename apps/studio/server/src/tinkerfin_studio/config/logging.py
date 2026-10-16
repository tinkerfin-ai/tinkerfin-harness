"""由应用生命周期管理的进程日志输出"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager
from logging.handlers import QueueHandler, QueueListener
from queue import Full, Queue

from concurrent_log_handler import ConcurrentRotatingFileHandler

from tinkerfin_studio.config.settings import Settings

_LOG_FORMAT = (
    "%(asctime)s [%(levelname)s] [%(name)s] [pid:%(process)d] "
    "[thread:%(threadName)s] %(filename)s:%(lineno)d - %(funcName)s() - %(message)s"
)


class _LogQueueHandler(QueueHandler):
    """请求只入有界队列，满时累计丢弃数量，由日志线程汇总告警"""

    def __init__(self) -> None:
        self.records: Queue[logging.LogRecord | None] = Queue(maxsize=4096)
        super().__init__(self.records)
        self.dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.records.put_nowait(record)
        except Full:
            self.dropped += 1

    def take_dropped(self) -> int:
        self.acquire()
        try:
            count, self.dropped = self.dropped, 0
            return count
        finally:
            self.release()


class _LogQueueListener(QueueListener):
    """单线程负责全部输出，退出时等待已入队日志写完"""

    def __init__(self, source: _LogQueueHandler, *handlers: logging.Handler) -> None:
        super().__init__(source.queue, *handlers)
        self.source = source

    def enqueue_sentinel(self) -> None:
        # 关闭操作在线程中执行，队列满时等待消费者腾出位置
        self.source.records.put(None)

    def handle(self, record: logging.LogRecord) -> None:
        super().handle(record)
        dropped = self.source.take_dropped()
        if dropped:
            super().handle(
                logging.LogRecord(
                    __name__,
                    logging.WARNING,
                    __file__,
                    0,
                    "日志队列已满，丢弃 %s 条日志",
                    (dropped,),
                    None,
                )
            )


def setup_console_logging(level: str = "INFO") -> None:
    """为命令行父进程配置控制台输出，不打开日志文件"""
    logging.basicConfig(
        level=level,
        format=_LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    # 请求日志可能包含临时图片签名地址，禁止输出 URL 级别日志
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _configure_logging(settings: Settings, stack: ExitStack) -> None:
    outputs: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    stack.callback(outputs[0].close)
    if settings.log_file_enabled:
        settings.log_file_path.parent.mkdir(parents=True, exist_ok=True)
        handler = ConcurrentRotatingFileHandler(
            settings.log_file_path,
            maxBytes=settings.log_file_max_bytes,
            backupCount=settings.log_file_backup_count,
            encoding="utf-8",
        )
        stack.callback(handler.close)
        # 启动即检查文件可写；实际写入与滚动由并发处理器负责
        with handler.do_open():
            pass
        outputs.append(handler)
    for output in outputs:
        output.setFormatter(logging.Formatter(_LOG_FORMAT))
    source = _LogQueueHandler()
    source.setFormatter(logging.Formatter("%(message)s"))
    stack.callback(source.close)
    listener = _LogQueueListener(source, *outputs)
    listener.start()
    stack.callback(listener.stop)
    logging.basicConfig(level=settings.log_level, handlers=[source], force=True)
    # 先切回控制台，再排空队列；进程后续退出日志仍可见
    stack.callback(setup_console_logging, settings.log_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        if name == "uvicorn.access" and not target.handlers and not target.propagate:
            # Uvicorn 通过此状态表达 --no-access-log，应用必须保留
            continue
        target.handlers = []
        target.propagate = True
        target.setLevel(settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def setup_logging(settings: Settings) -> AsyncIterator[None]:
    """在应用运行期间输出日志，退出时排空队列并释放线程和文件

    每个服务进程使用独立上下文，同一进程的应用生命周期不得重叠。
    同步文件依赖由单个监听线程写入，初始化与关闭由专用单线程串行执行。
    文件系统调用无法强制取消；取消仍等待清理，卡顿时退出可能延迟。

    Args:
        settings: 已由统一配置入口解析的服务配置
    """
    stack = ExitStack()
    loop = asyncio.get_running_loop()
    # 单线程保证即使启动被取消，也先完成初始化，再关闭其创建的资源
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-logging")
    opening = loop.run_in_executor(executor, _configure_logging, settings, stack)
    primary: BaseException | None = None
    try:
        await asyncio.shield(opening)
        yield
    except BaseException as error:  # noqa: BLE001 - 清理后传播原始异常与取消
        primary = error
    finally:
        closing = loop.run_in_executor(executor, stack.close)
        executor.shutdown(wait=False)
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError as error:
                primary = error
            except BaseException:  # noqa: BLE001 - 下方读取清理异常并保留主因
                break
        opening.exception()
        cleanup_error = closing.exception()
        if primary is not None:
            raise primary from cleanup_error
        if cleanup_error is not None:
            raise cleanup_error
