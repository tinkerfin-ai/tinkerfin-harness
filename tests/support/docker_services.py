"""Own disposable database containers for explicitly selected integration tests."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, TypeVar
from uuid import uuid4

import pytest

_ResultT = TypeVar("_ResultT")


async def _settle(operation: Awaitable[_ResultT]) -> _ResultT:
    task = asyncio.ensure_future(operation)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
    try:
        result = task.result()
    except Exception as error:
        if cancelled is not None:
            raise cancelled from error
        raise
    if cancelled is not None:
        raise cancelled
    return result


async def _docker(*arguments: str, missing: frozenset[str] = frozenset()) -> str:
    async def run() -> str:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(90):
                output, diagnostic = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode:
            explanation = diagnostic.decode(errors="replace").strip()
            if explanation in missing:
                return ""
            raise RuntimeError(explanation[-4096:])
        return output.decode()

    return await _settle(run())


@asynccontextmanager
async def owned_database(
    directory: Path, provider: Literal["mysql", "redis"]
) -> AsyncGenerator[str]:
    """Borrow a cached image and release only this invocation's labelled container."""
    if shutil.which("docker") is None:
        pytest.skip("docker_integration requires the Docker CLI and daemon")
    image = "mysql:8.4" if provider == "mysql" else "redis/redis-stack-server:7.4.0-v8"
    image_id = (
        await _docker(
            "image",
            "inspect",
            image,
            "--format",
            "{{.Id}}",
            missing=frozenset({f"Error response from daemon: No such image: {image}"}),
        )
    ).strip()
    if not image_id:
        pytest.skip(f"docker_integration requires the already cached image {image}")
    run_id = uuid4().hex
    name = f"tinkerfin-contract-{provider}-{run_id}"
    port = 3306 if provider == "mysql" else 6379
    manifest = directory / "resources.json"
    log_path = directory / "startup.log"
    record: dict[str, object] = {
        "run_id": run_id,
        "name": name,
        "borrowed_image": image_id,
        "container": None,
        "state": "creating",
    }

    async def save() -> None:
        await _settle(
            asyncio.to_thread(manifest.write_text, json.dumps(record, indent=2))
        )

    async def cleanup() -> None:
        record["state"] = "cleanup_pending"
        await save()

        async def inspect_container(identifier: str) -> str:
            return await _docker(
                "inspect",
                identifier,
                missing=frozenset(
                    {
                        f"Error: No such object: {identifier}",
                        f"Error response from daemon: No such container: {identifier}",
                    }
                ),
            )

        raw = await inspect_container(name)
        if raw:
            details = json.loads(raw)[0]
            if details["Config"]["Labels"].get("com.tinkerfin.test-run") != run_id:
                raise RuntimeError("The integration container has another owner")
            identifier = details["Id"]
            record["container"] = identifier
            volumes = [
                mount["Name"]
                for mount in details["Mounts"]
                if mount["Type"] == "volume"
            ]
            record["owned_volumes"] = volumes
            await save()
            await _docker("rm", "--force", "--volumes", identifier)
            if await inspect_container(identifier):
                raise RuntimeError("The owned integration container was not removed")
            for volume in volumes:
                if await _docker(
                    "volume",
                    "inspect",
                    volume,
                    missing=frozenset(
                        {
                            f"Error response from daemon: get {volume}: no such volume",
                        }
                    ),
                ):
                    raise RuntimeError("An owned anonymous volume was not removed")
        record["state"] = "removed_and_verified"
        await save()

    await save()
    interrupted: BaseException | None = None
    try:
        arguments = [
            "create",
            "--name",
            name,
            "--label",
            f"com.tinkerfin.test-run={run_id}",
            "--publish",
            f"127.0.0.1::{port}",
            "--tmpfs",
            "/var/lib/mysql" if provider == "mysql" else "/data",
        ]
        if provider == "mysql":
            arguments.extend(
                (
                    "--env",
                    "MYSQL_ALLOW_EMPTY_PASSWORD=yes",
                    "--env",
                    "MYSQL_ROOT_HOST=%",
                    "--env",
                    "MYSQL_DATABASE=contract",
                )
            )
        identifier = (await _docker(*arguments, image_id)).strip()
        record.update(container=identifier, state="running")
        await save()
        await _docker("start", identifier)

        async def wait_ready() -> None:
            logs = await asyncio.create_subprocess_exec(
                "docker",
                "logs",
                "--follow",
                identifier,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            lines: list[bytes] = []
            log_bytes = 0
            try:
                assert logs.stdout is not None
                async with asyncio.timeout(120):
                    while line := await logs.stdout.readline():
                        lines.append(line)
                        log_bytes += len(line)
                        if log_bytes > 2 * 1024 * 1024:
                            raise RuntimeError(
                                "Database startup output exceeded its limit"
                            )
                        ready = (
                            b"ready for connections" in line and b"port: 3306" in line
                            if provider == "mysql"
                            else b"Ready to accept connections" in line
                        )
                        if ready:
                            break
                    else:
                        raise RuntimeError("The owned database exited before readiness")
            finally:
                if logs.returncode is None:
                    logs.terminate()
                await logs.wait()
                await asyncio.to_thread(log_path.write_bytes, b"".join(lines))

        # Retain the CLI reader through process creation and readiness so caller
        # cancellation cannot lose an already created log-follow subprocess.
        await _settle(wait_ready())
        address = (await _docker("port", identifier, f"{port}/tcp")).strip()
        record.update(address=address, state="ready")
        await save()
        yield (
            f"mysql+asyncmy://root@{address}/contract?charset=utf8mb4"
            if provider == "mysql"
            else f"redis://{address}"
        )
    except BaseException as error:
        interrupted = error
        raise
    finally:
        try:
            await _settle(cleanup())
        except Exception as cleanup_error:
            if interrupted is not None and not isinstance(interrupted, Exception):
                raise interrupted from cleanup_error
            raise
