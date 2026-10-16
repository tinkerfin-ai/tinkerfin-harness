"""Execute one SQLite-backed test job under explicit parent-process signals."""

import asyncio
import sys
from datetime import datetime

import pytest
from sql_test_support import control_database_clock
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_automation import Automation, ExecutionRequest, SqlAlchemyAutomationStore
from tinkerfin_automation.clock import ManualClock


async def main() -> None:
    database = create_async_engine(sys.argv[1])
    store = SqlAlchemyAutomationStore(database)
    clock = ManualClock(datetime.fromisoformat(sys.argv[2]))
    patch = pytest.MonkeyPatch()
    control_database_clock(database, clock, patch)
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    output_protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
    output_transport, _ = await asyncio.get_running_loop().connect_write_pipe(
        lambda: output_protocol, sys.stdout
    )
    writer = asyncio.StreamWriter(
        output_transport, output_protocol, None, asyncio.get_running_loop()
    )
    started, proceed = asyncio.Event(), asyncio.Event()
    app = Automation(
        namespace="process-test",
        store=store,
        clock=clock,
    )

    @app.target("report")
    async def report(request: ExecutionRequest) -> dict[str, str]:
        started.set()
        await proceed.wait()
        return {"process": "worker"}

    try:
        async with app.worker() as worker:
            await started.wait()
            writer.write(b"running\n")
            await writer.drain()
            if await reader.readline() != b"finish\n":
                proceed.set()
                raise ValueError("parent did not authorize completion")
            proceed.set()
            await worker.wait_until_idle()
            writer.write(b"completed\n")
            await writer.drain()
    finally:
        transport.close()
        writer.close()
        try:
            await writer.wait_closed()
        finally:
            try:
                await store.close()
            finally:
                try:
                    await database.dispose()
                finally:
                    patch.undo()


if __name__ == "__main__":
    asyncio.run(main())
