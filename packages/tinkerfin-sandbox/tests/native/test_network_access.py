"""Network policy uses owned local services and the system certificate bundle."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from tinkerfin_sandbox import LocalAccess, LocalWorkspace


@asynccontextmanager
async def website() -> AsyncIterator[int]:
    tasks: set[asyncio.Task[None]] = set()

    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nowned"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(respond(reader, writer))
        tasks.add(task)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("proxied", [False, True])
async def test_normal_network_reaches_direct_and_proxied_owned_services(
    tmp_path: Path, sandbox: Path, proxied: bool
) -> None:
    async with website() as port:
        async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as local:
            async with local.open() as session:
                option = "--noproxy ''" if proxied else ""
                result = await session.aexecute(
                    f"curl -fsS {option} http://127.0.0.1:{port}/"
                )
                assert result.exit_code == 0 and result.output == "owned"


async def test_restricted_empty_allowlist_denies_owned_service(
    tmp_path: Path, sandbox: Path
) -> None:
    async with website() as port:
        async with LocalWorkspace(
            tmp_path,
            sandbox_executable=sandbox,
            access=LocalAccess(network="restricted"),
        ) as local:
            async with local.open() as session:
                result = await session.aexecute(f"curl -fsS http://127.0.0.1:{port}/")
                assert result.exit_code != 0 and "owned" not in result.output


async def test_default_tls_context_can_load_system_trust_under_workspace_isolation(
    tmp_path: Path, sandbox: Path
) -> None:
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as local:
        async with local.open() as session:
            result = await session.aexecute(
                'python -I -c \'import ssl; assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 0; print("verified")\''
            )
            assert result.exit_code == 0 and result.output == "verified\n"


def test_normal_network_rejects_ambiguous_domain_restrictions() -> None:
    with pytest.raises(ValueError, match="requires restricted"):
        LocalAccess(allowed_domains=("example.com",))
