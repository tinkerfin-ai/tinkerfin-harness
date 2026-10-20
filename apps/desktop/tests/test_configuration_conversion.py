"""离线连接配置转换保留身份、权限和原始备份"""

import fcntl
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/convert_configuration.py"
DEVICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ACCOUNT = "dddddddd-dddd-dddd-dddd-dddddddddddd"


@pytest.fixture
def previous_settings(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps(
            {
                "server": "https://api.example.com/root",
                "name": "Contract Mac",
                "device": {
                    "device_id": DEVICE,
                    "namespace": "ns_1",
                    "name": "Contract Mac",
                },
                "keychainAccount": ACCOUNT,
                "retiredAccounts": [],
                "permissions": {
                    "network": "normal",
                    "allowed_domains": [],
                    "browser": True,
                    "desktop": False,
                    "mcp_servers": [],
                },
                "pending": {
                    "account": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                    "server": "https://api.example.com/root",
                    "authorization": None,
                },
            }
        )
    )
    (tmp_path / "state").mkdir()
    (tmp_path / "state/executions.sqlite").write_bytes(b"retained execution state")
    return target


def convert(directory: Path, studio: str = "https://studio.example.com"):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(directory), "--studio-url", studio],
        capture_output=True,
        text=True,
        check=False,
    )


def test_conversion_retains_device_permissions_and_pending_origin(
    previous_settings, swift_contracts
):
    original = previous_settings.read_bytes()
    directory = previous_settings.parent
    result = convert(directory)
    assert result.returncode == 0, result.stderr
    assert (directory / "connection-backup/settings.json").read_bytes() == original
    actual = json.loads(previous_settings.read_bytes())
    expected = json.loads(original)
    expected["connection"] = {
        "studio_url": "https://studio.example.com",
        "api_url": expected.pop("server"),
    }
    pending = expected["pending"]
    pending["connection"] = {
        "studio_url": "https://studio.example.com",
        "api_url": pending.pop("server"),
    }
    assert actual == expected
    assert previous_settings.stat().st_mode & 0o777 == 0o600
    assert (
        directory / "state/executions.sqlite"
    ).read_bytes() == b"retained execution state"
    decoded = subprocess.run(
        [str(swift_contracts), "converted-configuration", str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr
    assert convert(directory).returncode != 0
    assert (directory / "connection-backup/settings.json").read_bytes() == original


@pytest.mark.parametrize("lock_path", ["application.lock", "state/host.lock"])
def test_conversion_refuses_running_client(previous_settings, lock_path):
    original = previous_settings.read_bytes()
    with (previous_settings.parent / lock_path).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert convert(previous_settings.parent).returncode != 0
    assert previous_settings.read_bytes() == original
    assert not (previous_settings.parent / "connection-backup").exists()


@pytest.mark.parametrize(
    "studio", ["http://example.com", "https://user:secret@example.com", "file:///tmp"]
)
def test_conversion_rejects_untrusted_studio_address(previous_settings, studio):
    original = previous_settings.read_bytes()
    assert convert(previous_settings.parent, studio).returncode != 0
    assert previous_settings.read_bytes() == original


def test_conversion_does_not_guess_studio_for_other_pending_service(previous_settings):
    data = json.loads(previous_settings.read_text())
    data["pending"]["server"] = "https://other.example.com"
    previous_settings.write_text(json.dumps(data))
    original = previous_settings.read_bytes()
    assert convert(previous_settings.parent).returncode != 0
    assert previous_settings.read_bytes() == original
    assert not (previous_settings.parent / "connection-backup").exists()


def test_conversion_rejects_linked_settings(previous_settings):
    outside = previous_settings.with_name("outside.json")
    previous_settings.rename(outside)
    previous_settings.symlink_to(outside)
    original = outside.read_bytes()
    assert convert(previous_settings.parent).returncode != 0
    assert outside.read_bytes() == original
    assert previous_settings.is_symlink()


def test_conversion_preserves_original_if_atomic_replacement_fails(
    previous_settings, monkeypatch
):
    spec = importlib.util.spec_from_file_location("configuration_conversion", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = previous_settings.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("synthetic replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic"):
        module.convert(
            previous_settings.parent, studio_url="https://studio.example.com"
        )
    assert previous_settings.read_bytes() == original
    assert (
        previous_settings.parent / "connection-backup/settings.json"
    ).read_bytes() == original
    assert not list(previous_settings.parent.glob(".connection-*"))
