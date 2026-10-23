"""真实 ad-hoc 身份的更新凭证交接，只使用本轮独占账户和临时应用"""

import asyncio
import base64
import fcntl
import importlib.util
import json
import os
import plistlib
import select
import shutil
import signal
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


@pytest.fixture(scope="module")
def credential_apps(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("更新凭证需要 macOS Keychain 与真实代码签名")
    root = tmp_path_factory.mktemp("credential-apps").resolve()
    desktop = Path(__file__).resolve().parents[1]
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    trust = root / "UpdateTrust.swift"
    trust.write_text(
        f'import Foundation\nenum UpdateTrust {{ static let publicKey = Data(base64Encoded: "{public}")! }}'
    )
    service = "com.tinkerfin.desktop.update-test." + uuid4().hex
    store = root / "KeychainStore.swift"
    store.write_text(
        (desktop / "native/KeychainStore.swift")
        .read_text()
        .replace('"com.tinkerfin.companion.device"', json.dumps(service))
    )
    sources = [
        path
        for path in (desktop / "native").glob("*.swift")
        if path.name
        not in {"CompanionApp.swift", "UpdateTrust.swift", "KeychainStore.swift"}
    ]
    binary = root / "TinkerFin"
    compiled = subprocess.run(
        [
            "swiftc",
            "-swift-version",
            "6",
            "-target",
            "arm64-apple-macosx15.0",
            *map(str, sources),
            str(trust),
            str(store),
            str(desktop / "tests/native/UpdateCredentialContracts.swift"),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    apps = []
    for name, version in [
        ("source", "0.1.4"),
        ("target", "0.1.5"),
        ("untrusted", "0.1.5"),
    ]:
        app = root / name / "TinkerFin.app"
        executable = app / "Contents/MacOS/TinkerFin"
        executable.parent.mkdir(parents=True)
        shutil.copy2(binary, executable)
        (app / "Contents/Resources").mkdir()
        (app / "Contents/Resources/build.txt").write_text(name)
        (app / "Contents/Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "com.tinkerfin.companion",
                    "CFBundleShortVersionString": version,
                    "CFBundleExecutable": "TinkerFin",
                    "CFBundlePackageType": "APPL",
                }
            )
        )
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(app)],
            check=True,
            capture_output=True,
        )
        apps.append(app)
    spec = importlib.util.spec_from_file_location(
        "update_archive", desktop / "scripts/update_archive.py"
    )
    assert spec is not None and spec.loader is not None
    archive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archive)
    yield apps, key, archive, service


async def command(
    app: Path,
    mode: str,
    directory: Path,
    account: str | None = None,
    *,
    payload: bytes | None = None,
) -> int:
    process = None
    inherited = None
    try:
        if mode in {"--receive-update-credential", "--remove-update-credential"}:
            inherited = os.open(
                directory / "credentials.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            fcntl.flock(inherited, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process = await asyncio.create_subprocess_exec(
            str(app / "Contents/MacOS/TinkerFin"),
            mode,
            str(directory),
            *([account] if account else []),
            stdin=asyncio.subprocess.PIPE
            if payload is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=inherited if inherited is not None else asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(payload)
        assert process.returncode is not None
        if process.returncode not in {0, 41, 42}:
            print(mode, process.returncode, stdout.decode(), (stderr or b"").decode())
        return process.returncode
    finally:
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if inherited is not None:
            os.close(inherited)


@dataclass(frozen=True, slots=True)
class _PreparedUpdate:
    state: Path
    directory: Path
    installed: Path
    target: Path
    source_account: str
    handoff: dict[str, str | int | None]


def _prepare_update(credential_apps, tmp_path: Path) -> _PreparedUpdate:
    (source, candidate, _), key, archive, _ = credential_apps
    tmp_path = tmp_path.resolve()
    identifier, source_account = uuid4().hex, str(uuid4())
    state = tmp_path / "state"
    directory = state / "state/updates" / identifier
    directory.mkdir(parents=True, mode=0o700)
    installed = tmp_path / "Installed.app"
    target = directory / "unpacked/TinkerFin.app"
    shutil.copytree(source, installed)
    shutil.copytree(candidate, target)
    handoff: dict[str, str | int | None] = {
        "identifier": identifier,
        "stateDirectory": str(state),
        "application": str(installed),
        "currentVersion": "0.1.4",
        "targetVersion": "0.1.5",
        "currentTreeSHA256": archive.tree_digest(installed),
        "parentPID": 42,
        "view": None,
        "language": "system",
        "locale": "en",
    }
    (directory / "handoff.json").write_text(json.dumps(handoff))
    manifest = json.dumps(
        {
            "version": "0.1.5",
            "target": "macos-arm64",
            "bundle_id": "com.tinkerfin.companion",
            "archive_url": "https://github.com/tinkerfin-ai/tinkerfin-harness/releases/download/desktop-v0.1.5/TinkerFin.zip",
            "archive_size": 42,
            "archive_sha256": "a" * 64,
            "tree_sha256": archive.tree_digest(target),
        }
    ).encode()
    (directory / "update.json").write_bytes(manifest)
    (directory / "update.json.sig").write_bytes(key.sign(manifest))
    return _PreparedUpdate(state, directory, installed, target, source_account, handoff)


async def _remove_owned_credentials(
    service: str, directory: Path, source_account: str, target_account: str | None
) -> None:
    # 回执丢失时也从本轮交接记录找回账户；所有删除限定唯一测试service
    accounts = {source_account}
    if target_account:
        accounts.add(target_account)
    cleanup = []
    try:
        journal_path = directory / "credentials.json"
        if journal_path.exists():
            recorded = json.loads(journal_path.read_bytes())
            assert recorded["sourceAccount"] == source_account
            recovered = recorded["targetAccount"]
            assert str(UUID(recovered)) == recovered
            assert recovered != source_account
            accounts.add(recovered)
    finally:
        for account in sorted(accounts):
            removal = await asyncio.create_subprocess_exec(
                "security",
                "delete-generic-password",
                "-s",
                service,
                "-a",
                account,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            removed = await removal.wait()
            checked = await asyncio.create_subprocess_exec(
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            cleanup.append((removed, await checked.wait()))
        assert all(removed in {0, 44} and found == 44 for removed, found in cleanup)


def _wait_credential_settlement(directory: Path) -> None:
    path = directory / "credentials.lock"
    if not path.exists():
        return
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    finally:
        os.close(descriptor)


@pytest.mark.companion_integration
@pytest.mark.parametrize(
    "outcome",
    ["cancel", "prepared-crash", "adopted-crash", "settled-crash", "complete"],
)
async def test_signed_update_credentials_recover_without_exposing_or_orphaning_secrets(
    credential_apps, tmp_path, outcome
):
    (source, _, untrusted), _, _, service = credential_apps
    prepared = _prepare_update(credential_apps, tmp_path)
    state, directory = prepared.state, prepared.directory
    installed, target = prepared.installed, prepared.target
    source_account, handoff = prepared.source_account, prepared.handoff
    target_account = None
    try:
        assert await command(installed, "setup", directory, source_account) == 0
        initial = json.loads((state / "settings.json").read_text())
        prepare = "prepared-crash" if outcome == "prepared-crash" else "prepare"
        assert await command(installed, prepare, directory) == (
            41 if outcome == "prepared-crash" else 0
        )
        journal = json.loads((directory / "credentials.json").read_text())
        target_account = journal["targetAccount"]
        assert journal["sourceAccount"] == source_account
        assert json.loads((state / "settings.json").read_text()) == initial
        assert await command(target, "read", directory, target_account) == 0
        assert await command(untrusted, "read", directory, target_account) == 1
        assert await command(untrusted, "remove", directory, target_account) == 1
        assert await command(target, "read", directory, target_account) == 0
        assert await command(installed, "read", directory, target_account) == 1
        assert await command(installed, "cleanup", directory) == 1
        assert (
            await command(
                target, "--remove-update-credential", directory, payload=b"0" * 64
            )
            == 1
        )
        assert await command(target, "read", directory, target_account) == 0
        if outcome == "cancel":
            for length in [63, 65]:
                assert (
                    await command(
                        target,
                        "--remove-update-credential",
                        directory,
                        payload=b"0" * length,
                    )
                    == 1
                )
        altered = {**handoff, "currentTreeSHA256": "b" * 64}
        (directory / "handoff.json").write_text(json.dumps(altered))
        assert await command(installed, "cancel", directory) == 1
        (directory / "handoff.json").write_text(json.dumps(handoff))
        if outcome in {"cancel", "prepared-crash"}:
            assert await command(installed, "cancel", directory) == 0
            assert await command(target, "read", directory, target_account) == 1
            assert await command(installed, "read", directory, source_account) == 0
        else:
            # 重复准备使用同一目标账户，丢失回执不会另建条目
            assert await command(installed, "prepare", directory) == 0
            assert (
                json.loads((directory / "credentials.json").read_text())[
                    "targetAccount"
                ]
                == target_account
            )
            assert await command(installed, "install", directory) == 0
            assert await command(source, "read", directory, source_account) == 0
            if outcome == "adopted-crash":
                assert await command(installed, "adopted-crash", directory) == 42
                assert (directory / "credentials.json").exists()
                assert await command(source, "read", directory, source_account) == 0
            persisted = (directory / "credentials.json").read_bytes()
            assert await command(installed, "adopt", directory) == 0
            assert await command(source, "read", directory, source_account) == 1
            if outcome == "settled-crash":
                # 模拟旧条目已删除、交接记录尚未删除的持久化边界
                (directory / "credentials.json").write_bytes(persisted)
                assert await command(installed, "adopt", directory) == 0
            assert await command(installed, "read", directory, target_account) == 0
            assert (
                json.loads((state / "settings.json").read_text())["keychainAccount"]
                == target_account
            )
        assert not (directory / "credentials.json").exists()
        assert await command(installed, "cleanup", directory) == 0
        assert not directory.exists()
        assert not (tmp_path / (".TinkerFin-update-" + directory.name)).exists()
        active_account = (
            source_account
            if outcome in {"cancel", "prepared-crash"}
            else target_account
        )
        assert await command(installed, "remove", directory, active_account) == 0
        assert await command(installed, "read", directory, active_account) == 1
    finally:
        await _remove_owned_credentials(
            service, directory, source_account, target_account
        )


@pytest.mark.companion_integration
async def test_update_credential_lock_preserves_inflight_import_after_parent_crash(
    credential_apps, tmp_path
):
    if sys.platform != "darwin":
        pytest.skip("原生进程退出通知需要 macOS kqueue")
    _, _, _, service = credential_apps
    prepared = _prepare_update(credential_apps, tmp_path)
    directory, installed = prepared.directory, prepared.installed
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[int] = loop.create_future()
    exited: asyncio.Future[None] = loop.create_future()
    pipe_closed: asyncio.Future[None] = loop.create_future()
    parent = None
    parent_io = None
    target_account = None
    with ExitStack() as resources:
        ready_path = directory / "keychain-ready.fifo"
        gate_path = directory / "keychain-continue.fifo"
        for path in (ready_path, gate_path):
            os.mkfifo(path, 0o600)
            resources.callback(path.unlink, missing_ok=True)
        event_descriptor = os.open(ready_path, os.O_RDONLY | os.O_NONBLOCK)
        resources.callback(os.close, event_descriptor)
        sentinel = os.open(ready_path, os.O_WRONLY | os.O_NONBLOCK)

        def close_sentinel() -> None:
            nonlocal sentinel
            if sentinel is not None:
                os.close(sentinel)
                sentinel = None

        resources.callback(close_sentinel)
        gate_descriptor = os.open(gate_path, os.O_RDWR | os.O_NONBLOCK)
        resources.callback(os.close, gate_descriptor)
        event_buffer = bytearray()

        def announced() -> None:
            try:
                data = os.read(event_descriptor, 64)
            except BlockingIOError:
                return
            if not data:
                if not pipe_closed.done():
                    pipe_closed.set_result(None)
                loop.remove_reader(event_descriptor)
                return
            event_buffer.extend(data)
            if b"\n" not in event_buffer or ready.done():
                return
            try:
                helper_pid = int(event_buffer.split(b"\n", 1)[0])
                assert helper_pid > 1
                if sys.platform == "darwin":
                    queue = select.kqueue()
                    resources.callback(queue.close)
                    queue.control(
                        [
                            select.kevent(
                                helper_pid,
                                filter=select.KQ_FILTER_PROC,
                                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                                fflags=select.KQ_NOTE_EXIT,
                            )
                        ],
                        0,
                        0,
                    )

                    def completed() -> None:
                        events = queue.control(None, 1, 0)
                        if events and not exited.done():
                            if events[0].ident == helper_pid:
                                exited.set_result(None)
                            else:
                                exited.set_exception(
                                    AssertionError("收到其他进程的退出事件")
                                )
                            loop.remove_reader(queue.fileno())

                    loop.add_reader(queue.fileno(), completed)
                    resources.callback(loop.remove_reader, queue.fileno())
                ready.set_result(helper_pid)
            except (AssertionError, OSError, ValueError) as error:
                ready.set_exception(error)

        loop.add_reader(event_descriptor, announced)
        resources.callback(loop.remove_reader, event_descriptor)
        try:
            assert (
                await command(installed, "setup", directory, prepared.source_account)
                == 0
            )
            parent = await asyncio.create_subprocess_exec(
                str(installed / "Contents/MacOS/TinkerFin"),
                "prepare",
                str(directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            parent_io = asyncio.create_task(parent.communicate())
            completed, _ = await asyncio.wait(
                {ready, parent_io}, return_when=asyncio.FIRST_COMPLETED
            )
            assert ready in completed, await parent_io
            assert ready.result() != parent.pid
            journal_path = directory / "credentials.json"
            journal = journal_path.read_bytes()
            target_account = json.loads(journal)["targetAccount"]
            parent.kill()
            await parent_io
            assert parent.returncode == -signal.SIGKILL

            assert await command(installed, "cancel", directory) == 1
            assert journal_path.read_bytes() == journal
            assert prepared.target.is_dir()
            assert await command(installed, "cleanup", directory) == 1
            assert os.write(gate_descriptor, b"x") == 1
            await asyncio.shield(exited)

            assert (
                await command(prepared.target, "read", directory, target_account) == 0
            )
            assert await command(installed, "cancel", directory) == 0
            assert (
                await command(prepared.target, "read", directory, target_account) == 1
            )
            assert (
                await command(installed, "read", directory, prepared.source_account)
                == 0
            )
            assert not journal_path.exists()
            assert await command(installed, "cleanup", directory) == 0
            assert not directory.exists()
        finally:
            if parent is not None and parent.returncode is None:
                parent.kill()
            if parent_io is not None:
                await asyncio.shield(parent_io)
            os.write(gate_descriptor, b"x")
            # 助手可能尚未抵达写入门闩；先结算其继承的真实OS锁，再以FIFO EOF确认进程结束
            settlement = asyncio.create_task(
                asyncio.to_thread(_wait_credential_settlement, directory)
            )
            await asyncio.shield(settlement)
            close_sentinel()
            announced()
            await asyncio.shield(pipe_closed)
            if ready.done() and not ready.cancelled() and ready.exception() is None:
                await asyncio.shield(exited)
            await _remove_owned_credentials(
                service, directory, prepared.source_account, target_account
            )
