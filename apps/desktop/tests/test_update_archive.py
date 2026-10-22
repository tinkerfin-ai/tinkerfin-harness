"""更新包的文件树、路径、签名和解压边界，全部使用独占临时应用"""

import asyncio
import base64
import importlib.util
import json
import plistlib
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


@pytest.fixture(scope="module")
def archive_tools():
    path = Path(__file__).resolve().parents[1] / "scripts/update_archive.py"
    spec = importlib.util.spec_from_file_location("update_archive", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def update_app(tmp_path):
    app = tmp_path / "source/TinkerFin.app"
    executable = app / "Contents/MacOS/TinkerFin"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic signed executable")
    executable.chmod(0o755)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.tinkerfin.companion",
                "CFBundleShortVersionString": "0.1.2",
                "CFBundleExecutable": "TinkerFin",
            }
        )
    )
    resources = app / "Contents/Resources"
    resources.mkdir()
    (resources / "内容.txt").write_text("offline resources")
    (resources / "é.txt").write_text("canonical unicode path")
    (resources / "linked").symlink_to("内容.txt")
    (resources / "directory-link").symlink_to("../MacOS", target_is_directory=True)
    (resources / "root-link").symlink_to("../..", target_is_directory=True)
    return app


def test_signed_tree_roundtrip_preserves_internal_links_and_permissions(
    archive_tools, update_app, tmp_path
):
    archive = tmp_path / "update.zip"
    archive_tools.create_archive(update_app, archive)
    digest = archive_tools.tree_digest(update_app)
    result = archive_tools.extract_verified(
        archive,
        tmp_path / "unpacked",
        size=archive.stat().st_size,
        sha256=archive_tools.file_digest(archive),
        tree=digest,
        release="0.1.2",
    )
    assert archive_tools.tree_digest(result) == digest
    assert (result / "Contents/Resources/linked").is_symlink()
    assert (result / "Contents/MacOS/TinkerFin").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    "name,mode,content",
    [
        ("../outside", stat.S_IFREG | 0o644, b"escape"),
        ("TinkerFin.app/../outside", stat.S_IFREG | 0o644, b"escape"),
        ("/outside", stat.S_IFREG | 0o644, b"escape"),
        ("TinkerFin.app/Contents/Info.plist", stat.S_IFREG | 0o644, b"duplicate"),
        ("TinkerFin.app/Contents/info.plist", stat.S_IFREG | 0o644, b"case collision"),
        ("TinkerFin.app/Contents/evil", stat.S_IFLNK | 0o777, b"../../../outside"),
        ("TinkerFin.app/Contents/evil", stat.S_IFLNK | 0o777, b"/tmp/outside"),
        ("TinkerFin.app/Contents/evil", stat.S_IFLNK | 0o777, b"evil"),
        ("TinkerFin.app/Contents/special", stat.S_IFIFO | 0o600, b""),
        ("TinkerFin.app/Contents/setuid", stat.S_IFREG | 0o4755, b"code"),
        (
            "TinkerFin.app/Contents/Resources/linked/child",
            stat.S_IFREG | 0o644,
            b"code",
        ),
    ],
)
def test_extraction_rejects_unsafe_entries_and_cleans_only_owned_destination(
    archive_tools, update_app, tmp_path, name, mode, content
):
    archive = tmp_path / "update.zip"
    archive_tools.create_archive(update_app, archive)
    with zipfile.ZipFile(archive, "a") as package:
        item = zipfile.ZipInfo(name)
        item.create_system = 3
        item.external_attr = mode << 16
        package.writestr(item, content)
    borrowed = tmp_path / "outside"
    borrowed.write_text("retained")
    with pytest.raises((ValueError, RuntimeError, OSError)):
        archive_tools.extract_verified(
            archive,
            tmp_path / "unpacked",
            size=archive.stat().st_size,
            sha256=archive_tools.file_digest(archive),
            tree=archive_tools.tree_digest(update_app),
            release="0.1.2",
        )
    assert borrowed.read_text() == "retained"
    assert not (tmp_path / "unpacked").exists()


@pytest.mark.parametrize("field", ["size", "sha256", "tree", "release"])
def test_extraction_rejects_unsigned_size_content_tree_and_identity(
    archive_tools, update_app, tmp_path, field
):
    archive = tmp_path / "update.zip"
    archive_tools.create_archive(update_app, archive)
    options = {
        "size": archive.stat().st_size,
        "sha256": archive_tools.file_digest(archive),
        "tree": archive_tools.tree_digest(update_app),
        "release": "0.1.2",
    }
    options[field] = 1 if field == "size" else "invalid"
    with pytest.raises(ValueError):
        archive_tools.extract_verified(archive, tmp_path / "unpacked", **options)
    assert not (tmp_path / "unpacked").exists()


async def test_native_signature_identity_and_tree_match_release_tooling(
    archive_tools, update_app, tmp_path, swift_contracts
):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = {
        "version": "0.1.2",
        "target": "macos-arm64",
        "bundle_id": "com.tinkerfin.companion",
        "archive_url": "https://github.com/tinkerfin-ai/tinkerfin-harness/releases/download/desktop-v0.1.2/TinkerFin.zip",
        "archive_size": 42,
        "archive_sha256": "a" * 64,
        "tree_sha256": archive_tools.tree_digest(update_app),
    }
    content = json.dumps(payload).encode()

    def encode(value: bytes) -> str:
        return base64.b64encode(value).decode()

    cases = [
        {
            "manifest": encode(content),
            "signature": encode(key.sign(content)),
            "valid": True,
        },
        {
            "manifest": encode(content + b" "),
            "signature": encode(key.sign(content)),
            "valid": False,
        },
    ]
    for field, value in [
        ("target", "macos-x86_64"),
        ("bundle_id", "external.app"),
        ("archive_url", "https://example.com/TinkerFin.zip"),
        ("version", "01.2.3"),
        ("archive_size", 2**40),
    ]:
        invalid = json.dumps({**payload, field: value}).encode()
        cases.append(
            {
                "manifest": encode(invalid),
                "signature": encode(key.sign(invalid)),
                "valid": False,
            }
        )
    (tmp_path / "update-contract.json").write_text(
        json.dumps(
            {
                "publicKey": encode(public),
                "cases": cases,
                "tree": payload["tree_sha256"],
                "app": str(update_app),
            }
        )
    )
    process = await asyncio.create_subprocess_exec(
        str(swift_contracts),
        "update-artifact",
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.decode().strip() == "verified update-artifact"
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "changed-source",
        "existing-adjacent",
        "cancelled",
        "bad-signature",
        "interrupted",
    ],
)
async def test_atomic_installation_retains_old_app_on_every_precommit_failure(
    archive_tools, update_app, tmp_path, swift_contracts, case
):
    identifier = uuid4().hex
    state = tmp_path / "state"
    directory = state / "state/updates" / identifier
    directory.mkdir(mode=0o700, parents=True)
    original = tmp_path / "Installed.app"
    shutil.copytree(update_app, original, symlinks=True)
    info = original / "Contents/Info.plist"
    info.write_bytes(
        plistlib.dumps(
            {**plistlib.loads(info.read_bytes()), "CFBundleShortVersionString": "0.1.1"}
        )
    )
    candidate = directory / "unpacked/TinkerFin.app"
    candidate.parent.mkdir()
    shutil.copytree(update_app, candidate, symlinks=True)
    marker = state / "settings.json"
    marker.write_text("retained settings")
    adjacent = tmp_path / f".TinkerFin-update-{identifier}"
    payload = {
        "version": "0.1.2",
        "target": "macos-arm64",
        "bundle_id": "com.tinkerfin.companion",
        "archive_url": "https://github.com/tinkerfin-ai/tinkerfin-harness/releases/download/desktop-v0.1.2/TinkerFin.zip",
        "archive_size": 42,
        "archive_sha256": "a" * 64,
        "tree_sha256": archive_tools.tree_digest(candidate),
    }
    if case == "changed-source":
        (candidate / "Contents/MacOS/TinkerFin").write_text("changed executable")
    if case == "existing-adjacent":
        adjacent.mkdir()
        (adjacent / "borrowed").write_text("retained borrowed resource")
    key = Ed25519PrivateKey.generate()
    manifest = json.dumps(payload).encode()
    signature = key.sign(manifest)
    if case == "bad-signature":
        signature = b"x" * 64
    (tmp_path / "installation.json").write_text(
        json.dumps(
            {
                "handoff": {
                    "identifier": identifier,
                    "stateDirectory": str(state),
                    "application": str(original),
                    "currentVersion": "0.1.1",
                    "currentTreeSHA256": archive_tools.tree_digest(original),
                    "targetVersion": "0.1.2",
                    "parentPID": 42,
                    "view": None,
                    "language": "system",
                    "locale": "en",
                },
                "publicKey": base64.b64encode(
                    key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                ).decode(),
                "manifest": base64.b64encode(manifest).decode(),
                "signature": base64.b64encode(signature).decode(),
                "cancelled": case == "cancelled",
                "installed": case in {"success", "interrupted"},
                "interrupt": False,
            }
        )
    )
    if case == "interrupted":
        fixture_path = tmp_path / "installation.json"
        fixture = json.loads(fixture_path.read_text())
        fixture["interrupt"] = True
        fixture_path.write_text(json.dumps(fixture))
        interrupted = await asyncio.create_subprocess_exec(
            str(swift_contracts), "update-installation", str(tmp_path)
        )
        assert await interrupted.wait() == 41
        assert (
            plistlib.loads(info.read_bytes())["CFBundleShortVersionString"] == "0.1.1"
        )
        assert (directory / "adjacent.json").is_file()
        fixture["interrupt"] = False
        fixture_path.write_text(json.dumps(fixture))
    process = await asyncio.create_subprocess_exec(
        str(swift_contracts),
        "update-installation",
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.decode().strip() == "verified update-installation"
        assert marker.read_text() == "retained settings"
        if case == "existing-adjacent":
            assert (adjacent / "borrowed").read_text() == "retained borrowed resource"
        else:
            assert not adjacent.exists()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def test_update_subprocess_releases_child_and_supports_reuse_after_cancel(
    swift_contracts, tmp_path
):
    connected = asyncio.Queue(maxsize=1)
    writers = []

    def accept(reader, writer):
        writers.append(writer)
        connected.put_nowait(reader)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    child = tmp_path / "child"
    child.write_text(
        f"#!{Path(sys.executable).resolve()}\nimport socket\n"
        f"with socket.create_connection(('127.0.0.1', {server.sockets[0].getsockname()[1]})) as control:\n"
        "    control.sendall(b'started\\n')\n    control.recv(1)\n"
    )
    child.chmod(0o700)
    process = await asyncio.create_subprocess_exec(
        str(swift_contracts),
        "update-subprocess",
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    connection = asyncio.create_task(connected.get())
    exited = asyncio.create_task(process.wait())
    assert process.stderr is not None
    diagnostics = asyncio.create_task(process.stderr.read())
    try:
        assert process.stdout is not None and process.stdin is not None
        assert await process.stdout.readline() == b"checkpoint update-child-started\n"
        done, _ = await asyncio.wait(
            [connection, exited], return_when=asyncio.FIRST_COMPLETED
        )
        if connection not in done:
            raise AssertionError((await diagnostics).decode())
        child_reader = await connection
        assert await child_reader.readline() == b"started\n"
        process.stdin.write(b"update-child-started\n")
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
        stdout = await process.stdout.read()
        await exited
        assert process.returncode == 0, (await diagnostics).decode()
        assert stdout.decode().strip() == "verified update-subprocess"
        assert await child_reader.read() == b""
    finally:
        connection.cancel()
        if process.returncode is None:
            process.kill()
        await process.wait()
        await asyncio.gather(connection, exited, diagnostics, return_exceptions=True)
        server.close()
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        await server.wait_closed()
