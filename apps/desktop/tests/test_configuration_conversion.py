"""离线转换仅处理显式设备，保留数据并拒绝恢复旧权限"""

import fcntl
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_cloud import configuration

from tinkerfin_companion.configuration import WorkspacePermissions
from tinkerfin_companion.projects import ManagedProjects

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/convert_configuration.py"
DEVICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_DEVICE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PROJECT = "cccccccc-cccc-cccc-cccc-cccccccccccc"
SCOPE = "a" * 64


def snapshot(directory: Path, device: str, scope: str) -> Path:
    root = directory / "state" / device / "workspace-permissions"
    root.mkdir(parents=True, exist_ok=True)
    target = root / (hashlib.sha256(scope.encode()).hexdigest() + ".json")
    target.write_text(
        json.dumps(
            {
                "project_id": PROJECT,
                "workspace_scope_id": scope,
                "permissions": {
                    "allowed_domains": ["example.com"],
                    "browser": True,
                    "desktop": False,
                    "mcp_servers": [],
                },
            }
        )
    )
    return target


@pytest.fixture
def previous_settings(tmp_path):
    content = {
        "server": "https://studio.test",
        "name": "Contract Mac",
        "device": {"device_id": DEVICE, "namespace": "ns_1", "name": "Contract Mac"},
        "keychainAccount": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "retiredAccounts": [],
        "permissions": {
            "allowed_domains": ["example.com"],
            "browser": True,
            "desktop": False,
            "mcp_servers": [],
        },
    }
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(content))
    snapshot(tmp_path, DEVICE, SCOPE)
    (tmp_path / "state" / DEVICE / "executions.sqlite").write_bytes(
        b"owned execution receipts"
    )
    (tmp_path / "state" / DEVICE / "projects.json").write_bytes(
        b"unchanged directory identity"
    )
    return target


def convert(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(directory), "--device", DEVICE, *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def test_conversion_preserves_identity_and_decodes_current_swift_configuration(
    previous_settings, swift_contracts
):
    original = previous_settings.read_bytes()
    directory = previous_settings.parent
    grant = (
        directory
        / "state"
        / DEVICE
        / "workspace-permissions"
        / (hashlib.sha256(SCOPE.encode()).hexdigest() + ".json")
    )
    original_grant = grant.read_bytes()
    result = convert(directory)
    assert result.returncode == 0, result.stderr
    backup = directory / "state" / "network-policy-backup"
    assert (backup / "settings.json").read_bytes() == original
    assert (backup / grant.relative_to(directory)).read_bytes() == original_grant
    actual = json.loads(previous_settings.read_bytes())
    expected = json.loads(original)
    expected["permissions"].update(network="normal", allowed_domains=[])
    assert actual == expected
    assert previous_settings.stat().st_mode & 0o777 == 0o600
    assert not (directory / "host.lock").exists()
    converted = json.loads(grant.read_bytes())
    assert converted["revoked"] is True
    assert converted["permissions"]["network"] == "restricted"
    assert converted["permissions"]["allowed_domains"] == ["example.com"]
    assert (
        directory / "state" / DEVICE / "executions.sqlite"
    ).read_bytes() == b"owned execution receipts"
    assert (
        directory / "state" / DEVICE / "projects.json"
    ).read_bytes() == b"unchanged directory identity"
    decoded = subprocess.run(
        [str(swift_contracts), "converted-configuration", str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr
    assert convert(directory).returncode != 0
    assert (backup / "settings.json").read_bytes() == original


async def test_converted_grant_rejects_resume_and_new_scope_keeps_same_project(
    previous_settings,
):
    directory = previous_settings.parent
    assert convert(directory).returncode == 0
    policy = WorkspacePermissions.model_validate_json(
        json.dumps(json.loads(previous_settings.read_bytes())["permissions"])
    )
    identity = configuration(directory).model_copy(update={"permissions": policy})
    manager = ManagedProjects(identity, directory / "state" / DEVICE)
    with pytest.raises(ValueError, match="撤销"):
        await manager.permissions_for(PROJECT, SCOPE)
    assert (await manager.permissions_for(PROJECT, "e" * 64)).network == "normal"
    assert (
        directory / "state" / DEVICE / "executions.sqlite"
    ).read_bytes() == b"owned execution receipts"


def test_conversion_does_not_read_excluded_scope_or_other_devices(previous_settings):
    directory = previous_settings.parent
    excluded = "b" * 64
    target = snapshot(directory, DEVICE, excluded)
    target.write_bytes(b"must not be parsed")
    other = snapshot(directory, OTHER_DEVICE, "c" * 64)
    original_other = other.read_bytes()
    result = convert(directory, "--exclude-scope", excluded)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == b"must not be parsed"
    assert other.read_bytes() == original_other
    assert not (
        directory / "state" / "network-policy-backup" / target.relative_to(directory)
    ).exists()


@pytest.mark.parametrize("lock_path", ["application.lock", "state/host.lock"])
def test_conversion_does_not_write_while_client_owns_state(
    previous_settings, lock_path
):
    original = previous_settings.read_bytes()
    with (previous_settings.parent / lock_path).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert convert(previous_settings.parent).returncode != 0
    assert previous_settings.read_bytes() == original
    assert not (previous_settings.parent / "state" / "network-policy-backup").exists()


def test_conversion_validates_all_selected_snapshots_before_mutation(previous_settings):
    directory = previous_settings.parent
    original = previous_settings.read_bytes()
    invalid = snapshot(directory, DEVICE, "f" * 64)
    invalid.write_bytes(b"invalid snapshot")
    assert convert(directory).returncode != 0
    assert previous_settings.read_bytes() == original
    assert not (directory / "state" / "network-policy-backup").exists()


def test_conversion_rejects_linked_snapshots_without_reading_target(previous_settings):
    directory = previous_settings.parent
    selected = snapshot(directory, DEVICE, "d" * 64)
    outside = directory / "unrelated.json"
    outside.write_bytes(b"outside content")
    selected.unlink()
    selected.symlink_to(outside)
    assert convert(directory).returncode != 0
    assert outside.read_bytes() == b"outside content"
    assert selected.is_symlink()


def test_conversion_rejects_linked_host_state(previous_settings):
    directory = previous_settings.parent
    original_settings = previous_settings.read_bytes()
    state = directory / "state"
    outside = directory / "unrelated-state"
    state.rename(outside)
    state.symlink_to(outside, target_is_directory=True)
    selected = next((outside / DEVICE / "workspace-permissions").iterdir())
    original_snapshot = selected.read_bytes()
    assert convert(directory).returncode != 0
    assert previous_settings.read_bytes() == original_settings
    assert selected.read_bytes() == original_snapshot
    assert not (directory / "state" / "network-policy-backup").exists()


def test_conversion_restores_changed_snapshots_if_configuration_write_fails(
    previous_settings, monkeypatch
):
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location("configuration_conversion", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = previous_settings.parent
    original_settings = previous_settings.read_bytes()
    selected = next((directory / "state" / DEVICE / "workspace-permissions").iterdir())
    original_snapshot = selected.read_bytes()
    replace = os.replace

    def fail_settings(source, target):
        if Path(target) == previous_settings:
            raise OSError("synthetic write failure")
        replace(source, target)

    monkeypatch.setattr(os, "replace", fail_settings)
    with pytest.raises(OSError, match="synthetic"):
        module.convert(directory, devices=[DEVICE])
    assert previous_settings.read_bytes() == original_settings
    assert selected.read_bytes() == original_snapshot
    assert (
        directory / "state" / "network-policy-backup" / "settings.json"
    ).read_bytes() == original_settings
    assert not list(directory.glob(".network-policy-*"))
