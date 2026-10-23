"""应用构建输入与显式选择的开发包验收，所有测试状态均使用临时目录"""

import asyncio
import json
import os
import platform
import plistlib
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from tinkerfin_remote import RemoteTools


def test_installation_accepts_bundled_cli_without_a_browser(tmp_path, monkeypatch):
    from tinkerfin_desktop import installation

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    for name in (
        "node/bin/node",
        "node/bin/npm",
        "node/bin/npx",
        "python/lib/python3.11/site-packages/tinkerfin_desktop/browser_command.py",
        "runtime/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
        "runtime/browser-service.mjs",
        "runtime/node_modules/@playwright/mcp/package.json",
        "runtime/node_modules/playwright/package.json",
        "python/lib/python3.11/site-packages/tinkerfin_desktop/browser_help.md",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic resource")
        path.chmod(0o700)
    selected = installation.Installation.read(tmp_path)
    assert selected.browser.service == tmp_path / "runtime/browser-service.mjs"
    assert not (tmp_path / "browsers").exists()


@pytest.fixture
def app_bundle() -> Path:
    selected = os.environ.get("TINKERFIN_TEST_APP")
    if sys.platform != "darwin" or not selected:
        pytest.skip("需要通过 TINKERFIN_TEST_APP 显式选择本轮自包含应用")
    app = Path(selected).resolve(strict=True)
    assert (app / "Contents/MacOS/TinkerFin").is_file()
    return app


def environment(home: Path) -> dict[str, str]:
    home.mkdir(exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "LANG": "en_US.UTF-8",
    }


def test_bundled_menu_bar_resources_and_client_url_scheme(app_bundle):
    resources = app_bundle / "Contents/Resources"
    source = Path(__file__).resolve().parents[1]
    metadata = plistlib.loads((app_bundle / "Contents/Info.plist").read_bytes())
    assert metadata["CFBundleIdentifier"] == "com.tinkerfin.companion"
    assert metadata["CFBundleName"] == "TinkerFin"
    assert metadata["CFBundleDisplayName"] == "TinkerFin"
    assert metadata["CFBundleExecutable"] == "TinkerFin"
    assert metadata["CFBundleDevelopmentRegion"] == "en"
    assert set(metadata["CFBundleLocalizations"]) == {"en", "zh-Hans"}
    assert metadata["LSUIElement"] is True
    assert metadata["LSMultipleInstancesProhibited"] is True
    assert "tinkerfin" in [
        scheme
        for item in metadata["CFBundleURLTypes"]
        for scheme in item["CFBundleURLSchemes"]
    ]
    app_icon = (resources / metadata["CFBundleIconFile"]).read_bytes()
    assert app_icon[:4] == b"icns"
    assert int.from_bytes(app_icon[4:8], "big") == len(app_icon)
    assert (resources / "ui/index.html").is_file()
    assert (resources / "ui/THIRD_PARTY_NOTICES.txt").stat().st_size > 0
    assert (resources / "licenses/desktop-ui.txt").read_bytes() == (
        resources / "ui/THIRD_PARTY_NOTICES.txt"
    ).read_bytes()
    assert (resources / "TinkerFinUpdater").is_file()
    assert (resources / "update_archive.py").read_bytes() == (
        source / "scripts/update_archive.py"
    ).read_bytes()
    brand = source / "ui/public/brand.svg"
    assert (resources / "brand.svg").read_bytes() == brand.read_bytes()
    for language in ("en", "zh-Hans"):
        catalog = Path(f"{language}.lproj/Localizable.strings")
        assert (resources / catalog).read_bytes() == (
            source / "resources" / catalog
        ).read_bytes()
    expected_icons = {
        path.name: path.read_bytes() for path in (source / "resources/icons").iterdir()
    }
    assert "log-out.svg" in expected_icons
    assert {
        path.name: path.read_bytes() for path in (resources / "icons").iterdir()
    } == expected_icons
    assert (resources / "licenses/supplemental/lucide-ISC.txt").read_bytes() == (
        source / "runtime/licenses/lucide-ISC.txt"
    ).read_bytes()
    assert (resources / "runtime/browser-service.mjs").is_file()
    inventory = json.loads((resources / "licenses/node-packages.json").read_bytes())
    packages = {item["name"]: item for item in inventory}
    assert packages["@playwright/mcp"]["version"] == "0.0.83"
    assert all(item["files"] for item in inventory)
    assert all(
        (resources / path).is_file() for item in inventory for path in item["files"]
    )
    package = resources / "python/lib/python3.11/site-packages/tinkerfin_desktop"
    assert os.access(package / "browser_command.py", os.X_OK)
    assert (package / "browser_release.json").read_bytes() == (
        source / "src/tinkerfin_desktop/browser_release.json"
    ).read_bytes()
    assert not (resources / "browsers").exists()
    assert (resources / "runtime/node_modules/playwright").is_dir()
    assert (package / "browser_help.md").is_file()
    assert not (resources / "connection.json").exists()


async def test_bundled_runtime_uses_owned_python_node_browser_tool_and_native_sandbox(
    app_bundle, tmp_path
):
    resources = app_bundle / "Contents/Resources"
    project = tmp_path / "project"
    project.mkdir()
    process = await asyncio.create_subprocess_exec(
        str(resources / "python/bin/python3"),
        "-I",
        "-B",
        str(Path(__file__).parent / "native/bundle_smoke.py"),
        str(resources),
        str(project),
        env=environment(tmp_path / "home"),
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout) == {
            "runtime": "verified",
            "browser": "verified",
            "dependencies": "verified",
        }
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def test_native_process_connects_bundled_host_stops_and_reopens_owned_state(
    app_bundle, swift_contracts, tmp_path
):
    resources = app_bundle / "Contents/Resources"
    device_id = str(uuid4())
    credential = "b" * 64
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cloud.sqlite'}")

    async def broker(socket: ServerConnection) -> None:
        assert socket.request is not None
        assert (
            socket.request.headers["Authorization"]
            == f"Bearer {device_id}.{credential}"
        )
        assert json.loads(await socket.recv()) == {
            "capabilities": {
                "browser": True,
                "desktop": False,
                "desktop_capture": False,
                "open_url": True,
                "mcp": False,
            }
        }
        await socket.send(
            json.dumps(
                {
                    "device_id": device_id,
                    "namespace": "ns_1",
                    "name": "Test Mac",
                    "connection_id": str(uuid4()),
                }
            )
        )
        try:

            async def receive() -> str:
                value = await socket.recv()
                assert isinstance(value, str)
                return value

            await endpoint.serve(send=socket.send, receive=receive)
        except ConnectionClosed:
            pass

    try:
        async with RemoteTools(engine) as remote:
            endpoint = remote.endpoint(namespace="ns_1", target=device_id)
            async with serve(broker, "127.0.0.1", 0) as server:
                port = next(iter(server.sockets)).getsockname()[1]
                for _ in range(2):
                    process = await asyncio.create_subprocess_exec(
                        str(swift_contracts),
                        "bundle",
                        str(resources),
                        str(tmp_path / "state"),
                        f"http://127.0.0.1:{port}",
                        device_id,
                        env=environment(tmp_path / "home"),
                        cwd=tmp_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, stderr = await process.communicate()
                        assert process.returncode == 0, stderr.decode()
                        assert stdout.strip() == b"verified bundle"
                        assert credential.encode() not in stdout + stderr
                    finally:
                        if process.returncode is None:
                            process.kill()
                        await process.wait()
    finally:
        await engine.dispose()
