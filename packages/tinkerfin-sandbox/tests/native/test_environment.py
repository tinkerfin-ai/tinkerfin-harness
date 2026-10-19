"""Explicit workload values stay out of owned launcher arguments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import pytest

from tinkerfin_sandbox import LocalAccess, LocalWorkspace


@pytest.mark.parametrize(
    "name", ["_TINKERFIN_WORKLOAD_ENVIRONMENT", "_tinkerfin_workload_environment"]
)
def test_workload_cannot_override_the_private_environment_carrier(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(ValueError, match="launcher variables"):
        LocalWorkspace(tmp_path, environment={name: "{}"})


@pytest.mark.parametrize("network", ["normal", "restricted"])
async def test_workload_environment_is_private_to_activation_and_not_launcher_argv(
    tmp_path: Path,
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    network: Literal["normal", "restricted"],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unexpected = tmp_path / "host-loader-executed"
    preload = tmp_path / "preload.cjs"
    preload.write_text(
        "require('node:fs').writeFileSync("
        + json.dumps(str(unexpected))
        + ", 'unexpected host execution')"
    )
    values = {
        "WORKSPACE_TEST_SECRET": "synthetic-scoped-token-not-for-launcher-argv",
        "CONFIG_VALUE": "line one\nline 'two' \"quoted\" \\ 你好",
        "NODE_OPTIONS": "--require=" + str(preload),
    }
    monkeypatch.setenv("HOST_ONLY_TEST_SECRET", "synthetic-host-only-value")
    source = """
import hashlib, json, os, sys
keys = ('WORKSPACE_TEST_SECRET', 'CONFIG_VALUE', 'NODE_OPTIONS')
values = {key: os.environ[key] for key in keys}
token = values['WORKSPACE_TEST_SECRET']
print(json.dumps({
    'pid': os.getpid(), 'parent': os.getppid(), 'group': os.getpgrp(),
    'digest': hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
    'host_leaked': 'HOST_ONLY_TEST_SECRET' in os.environ,
    'carrier_retained': any(token in value for key, value in os.environ.items() if key not in keys),
    'proxy_present': bool(os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')),
}), flush=True)
sys.stdin.buffer.read(1)
"""
    async with LocalWorkspace(
        project,
        access=LocalAccess(network=network),
        environment=values,
        sandbox_executable=sandbox,
    ) as workspace:
        async with workspace.open() as session:
            async with session.open_process(("python", "-I", "-c", source)) as owned:
                observed = json.loads(await owned.stdout.readline())
                assert observed["group"] == owned.pid
                pids = {owned.pid, observed["pid"], observed["parent"]}
                assert all(os.getpgid(pid) == owned.pid for pid in pids)
                probe = await asyncio.create_subprocess_exec(
                    "/bin/ps",
                    "-p",
                    ",".join(str(pid) for pid in sorted(pids)),
                    "-o",
                    "pid=,command=",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, error = await probe.communicate()
                assert probe.returncode == 0 and not error
                leaked = values["WORKSPACE_TEST_SECRET"].encode() in output
                assert not leaked
                assert (
                    observed["digest"]
                    == hashlib.sha256(
                        json.dumps(values, sort_keys=True).encode()
                    ).hexdigest()
                )
                assert not observed["host_leaked"]
                assert not observed["carrier_retained"]
                assert observed["proxy_present"] is (network == "restricted")
                assert not unexpected.exists()
                owned.stdin.write(b"x")
                await owned.stdin.drain()
                assert await owned.wait() == 0
