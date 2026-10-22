"""原生客户端拥有的 Python 进程入口，凭证只从独占标准输入读取"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from .host import CompanionHost, ConnectionState
from .installation import Installation
from .lifecycle import settle
from .maintenance import MaintenanceControl
from .native import (
    DesktopReply,
    Initialize,
    MaintenanceCancel,
    MaintenanceCommit,
    MaintenancePrepare,
    NativeChannel,
    ReadinessChanged,
    StdioChannel,
    Stop,
)


async def serve(channel: StdioChannel, installation: Installation, state: Path) -> None:
    """父进程退出、显式停止或认证失效时结算全部自有任务"""
    async with asyncio.timeout(15):
        initial = await channel.receive()
    if not isinstance(initial, Initialize):
        raise TypeError("本机启动消息缺少设备初始化")
    native = NativeChannel(channel.send, readiness=initial.readiness)
    maintenance: MaintenanceControl | None = None

    async def report(status: ConnectionState) -> None:
        await channel.send({"kind": "state", "state": status})

    async def control() -> None:
        while message := await channel.receive():
            if isinstance(message, Stop):
                return
            if isinstance(message, DesktopReply):
                native.receive(message)
            elif isinstance(message, ReadinessChanged):
                if native.update_readiness(message.readiness):
                    await host.refresh_capabilities()
            elif isinstance(
                message, (MaintenancePrepare, MaintenanceCancel, MaintenanceCommit)
            ):
                assert maintenance is not None
                if await maintenance.receive(message):
                    return
            else:
                raise TypeError("同一进程不能重复初始化设备身份")

    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    signals = (signal.SIGTERM, signal.SIGINT)
    previous = {kind: signal.getsignal(kind) for kind in signals}
    for kind in signals:
        loop.add_signal_handler(kind, stopped.set)
    try:
        async with CompanionHost(
            initial.configuration,
            state_directory=state,
            sandbox_executable=installation.sandbox,
            node_executable=installation.directory / "node/bin/node",
            browser=installation.browser,
            runtime_directory=installation.directory,
            native=native,
            report=report,
        ) as host:
            maintenance = MaintenanceControl(host, channel.send)
            tasks = (
                asyncio.create_task(host.run()),
                asyncio.create_task(control()),
                asyncio.create_task(stopped.wait()),
            )
            try:
                completed, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in completed:
                    task.result()
            finally:
                native.close()
                for task in tasks:
                    task.cancel()
                await settle(asyncio.gather(*tasks, return_exceptions=True))
    finally:
        # 先由宿主关闭工作端和资源，再释放维护，提交后的接纳门禁没有空隙
        try:
            if maintenance is not None:
                await maintenance.close()
        finally:
            native.close()
            for kind in signals:
                loop.remove_signal_handler(kind)
                signal.signal(kind, previous[kind])


async def run(resources: Path, state: Path) -> int:
    """将 stdio 接为异步管道，运行失败只输出可恢复的安全说明"""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
    read_transport, _reader_protocol = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    write_transport, write_protocol = await loop.connect_write_pipe(
        lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader()), sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    channel = StdioChannel(reader, writer)
    try:
        installation = await settle(asyncio.to_thread(Installation.read, resources))
        await serve(channel, installation, state)
        return 0
    except Exception:  # noqa: BLE001 - 进程边界不向原生界面泄漏配置、凭证或第三方异常
        try:
            await channel.send(
                {
                    "kind": "failure",
                    "message": "本机服务未能继续运行，请检查应用安装、文件权限和设备配对后重新连接",
                }
            )
        except (OSError, TimeoutError):
            pass
        return 1
    finally:
        read_transport.close()
        writer.transport.abort()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    """启动选项只接收资源和状态目录，禁止通过参数或环境传递凭证"""
    parser = argparse.ArgumentParser(description="TinkerFin 本机服务")
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        installation = Installation.read(args.resources)
        print(
            json.dumps(
                {
                    "ready": True,
                    "platform": "macos",
                    "architecture": "arm64",
                    "resources": str(installation.directory),
                }
            )
        )
        return 0
    if args.state_directory is None:
        parser.error("--state-directory is required")
    return asyncio.run(run(args.resources, args.state_directory))


if __name__ == "__main__":
    raise SystemExit(main())
