"""真实AppKit退出等待已提交任务，使用操作信号而非计时判定"""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def termination_contract(tmp_path_factory) -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("应用退出契约需要macOS与Swift SDK")
    desktop = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("application-exit") / "contract"
    result = subprocess.run(
        [
            "swiftc",
            "-swift-version",
            "6",
            "-target",
            "arm64-apple-macosx15.0",
            str(desktop / "native/ApplicationLifecycle.swift"),
            str(desktop / "tests/native/TerminationContracts.swift"),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return binary


@pytest.mark.companion_integration
@pytest.mark.parametrize("origin", ["task", "dispatch"])
async def test_application_exit_joins_async_cleanup_without_blocking_main_queue(
    termination_contract, origin
):
    process = await asyncio.create_subprocess_exec(
        str(termination_contract),
        origin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.decode().splitlines() == [
            "operation returned",
            "operation joined",
        ]
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
