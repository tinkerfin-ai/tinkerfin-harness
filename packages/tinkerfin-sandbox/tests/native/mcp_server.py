"""A real MCP peer whose effects and blocking point are controlled by its caller."""

import asyncio
import json
import os
from pathlib import Path

from mcp.server import MCPServer

server = MCPServer("local-test")


@server.tool()
async def echo(value: str) -> str:
    """Return the input and check that host secrets were not inherited."""

    def record() -> None:
        with Path("effects").open("a") as stream:
            stream.write("echo")

    await asyncio.to_thread(record)
    return json.dumps(
        {"value": value, "host_secret": os.environ.get("TINKERFIN_TEST_SECRET")}
    )


@server.tool()
async def blocked() -> str:
    """Notify the test through an authorized FIFO, then await cancellation."""

    def notify() -> None:
        with Path("started").open("wb", buffering=0) as stream:
            stream.write(b"ready")

    await asyncio.to_thread(notify)
    await asyncio.Event().wait()
    return "not-reached"


@server.tool()
def large_output() -> str:
    """Produce a bounded fixture result larger than a small workspace budget."""
    return "x" * 16384


server.run()
