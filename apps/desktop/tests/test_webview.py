"""真实系统WebView与窗口验收，只读构建工件并使用本轮独占目录"""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def webview_contract(tmp_path_factory) -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("原生WebView验收需要macOS与Swift SDK")
    source = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("webview-contract") / "contract"
    sources = [
        path
        for path in (source / "native").glob("*.swift")
        if path.name != "CompanionApp.swift"
    ]
    result = subprocess.run(
        [
            "swiftc",
            "-swift-version",
            "6",
            "-target",
            "arm64-apple-macosx15.0",
            *map(str, sources),
            str(source / "tests/native/UIContracts.swift"),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return binary


@pytest.fixture
def webview_resources(tmp_path) -> Path:
    source = Path(__file__).resolve().parents[1]
    built = source / "ui/dist"
    assert (built / "index.html").is_file(), "先执行桌面UI的pnpm build"
    resources = tmp_path / "resources"
    shutil.copytree(built, resources / "ui")
    outside = tmp_path / "outside.svg"
    outside.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect width="1" height="1"/></svg>'
    )
    (resources / "ui/escaped.svg").symlink_to(outside)
    return resources


@pytest.mark.companion_integration
async def test_system_webview_resources_bridge_and_native_window_lifecycle(
    webview_contract, webview_resources
):
    process = await asyncio.create_subprocess_exec(
        str(webview_contract),
        str(webview_resources),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.decode().strip() == "verified webview-and-windows"
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
