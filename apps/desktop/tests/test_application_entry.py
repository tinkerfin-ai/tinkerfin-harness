"""正式进程入口须让 AppKit 异步任务运行，凭证助手不构造客户端"""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def entry_contract(tmp_path_factory) -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("应用入口契约需要 macOS 与 Swift SDK")
    desktop = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("application-entry") / "contract"
    result = subprocess.run(
        [
            "swiftc",
            "-swift-version",
            "6",
            "-target",
            "arm64-apple-macosx15.0",
            str(desktop / "native/CompanionApp.swift"),
            str(desktop / "native/ApplicationLifecycle.swift"),
            str(desktop / "tests/native/EntryContracts.swift"),
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
@pytest.mark.parametrize(
    "mode",
    ["client", "--receive-update-credential", "--remove-update-credential"],
)
async def test_application_entry_runs_client_or_headless_helper(entry_contract, mode):
    process = await asyncio.create_subprocess_exec(
        str(entry_contract),
        mode,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        expected = (
            [
                "client-lock",
                "client-model",
                "client-launched",
                "client-ready",
                "client-closed",
            ]
            if mode == "client"
            else ["helper-finished"]
        )
        assert stdout.decode().splitlines() == expected
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
