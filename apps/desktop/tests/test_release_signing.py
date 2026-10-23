"""使用合成证书和受控系统边界验证签名资源归属，不调用本机 Security 命令"""

import base64
import importlib
import importlib.util
import json
import os
import plistlib
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NOW = datetime(2026, 1, 1, tzinfo=UTC)
START = datetime(2000, 1, 1, tzinfo=UTC)
END = datetime(2100, 1, 1, tzinfo=UTC)
PASSWORD = "synthetic-signing-password"


@pytest.fixture
def signing(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "release_signing_contract", scripts / "release_signing.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def synthetic_certificate(
    path: Path, *, signing_usage: bool = True
) -> tuple[bytes, bytes]:
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Isolated release signing")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(1)
        .not_valid_before(START)
        .not_valid_after(END)
        .add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.CODE_SIGNING
                    if signing_usage
                    else ExtendedKeyUsageOID.SERVER_AUTH
                ]
            ),
            critical=True,
        )
        .sign(private, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    container = pkcs12.serialize_key_and_certificates(
        b"synthetic",
        private,
        certificate,
        None,
        serialization.BestAvailableEncryption(PASSWORD.encode()),
    )
    return container, certificate.public_bytes(serialization.Encoding.DER)


@pytest.fixture
def material(signing, tmp_path, monkeypatch):
    certificate = tmp_path / "public.pem"
    container, der = synthetic_certificate(certificate)
    monkeypatch.setattr(signing, "CERTIFICATE", certificate)
    monkeypatch.setenv("DESKTOP_CODE_SIGNING_P12", base64.b64encode(container).decode())
    monkeypatch.setenv("DESKTOP_CODE_SIGNING_PASSWORD", PASSWORD)
    return container, der


class FakeSecurity:
    def __init__(self, module, root):
        self.module = module
        self.original_search = [str(root / "borrowed.keychain-db")]
        self.search = list(self.original_search)
        self.default = self.search[0]
        self.trust = {"user": {"A" * 40: {"certName": "borrowed"}}, "admin": {}}
        self.events = []
        self.fail_import = False
        self.fail_admin_read = False

    def command(self, *arguments, redactions=()):
        assert not any(name in os.environ for name in self.module.SECRET_NAMES)
        args = list(map(str, arguments))
        assert args[0] == "security"
        action = args[1]
        self.events.append(action)
        if action == "list-keychains":
            if "-s" in args:
                self.search = args[args.index("-s") + 1 :]
            return self.module.CommandResult(
                0, "\n".join(json.dumps(path) for path in self.search), ""
            )
        if action == "default-keychain":
            return self.module.CommandResult(0, json.dumps(self.default), "")
        if action == "trust-settings-export":
            domain = "admin" if "-d" in args else "user"
            if domain == "admin" and self.fail_admin_read:
                return self.module.CommandResult(1, "", "snapshot unavailable")
            Path(args[-1]).write_bytes(
                plistlib.dumps({"trustList": self.trust[domain]})
            )
        elif action == "create-keychain":
            Path(args[-1]).write_bytes(b"owned test keychain")
        elif action == "delete-keychain":
            Path(args[-1]).unlink()
        elif action == "import":
            assert Path(args[2]).is_file()
            if self.fail_import:
                return self.module.CommandResult(1, "", "import rejected")
        elif action not in {"set-keychain-settings", "set-key-partition-list"}:
            raise AssertionError(f"unowned operation: {action}")
        return self.module.CommandResult(0, "", "")


@pytest.fixture
def isolated(signing, material, tmp_path, monkeypatch):
    scope = signing.RunnerScope(tmp_path.resolve(), "12345", "1", os.getuid())
    monkeypatch.setattr(signing, "runner_scope", lambda: scope)
    monkeypatch.setattr(signing, "datetime", SimpleNamespace(now=lambda tz: NOW))
    system = FakeSecurity(signing, tmp_path)
    monkeypatch.setattr(signing, "command", system.command)
    state = scope.temporary / "tinkerfin-release-signing-12345-1"
    return system, state


@pytest.mark.parametrize(
    "missing", ["DESKTOP_CODE_SIGNING_P12", "DESKTOP_CODE_SIGNING_PASSWORD"]
)
def test_missing_secret_is_rejected_before_resource_creation(
    signing, material, monkeypatch, missing
):
    monkeypatch.delenv(missing)
    with pytest.raises(signing.SigningError, match="未配置"):
        signing.signing_material(now=NOW)
    assert not any(name in os.environ for name in signing.SECRET_NAMES)


def test_wrong_certificate_is_rejected_and_secrets_are_consumed(
    signing, material, tmp_path, monkeypatch
):
    wrong, _ = synthetic_certificate(tmp_path / "different.pem")
    monkeypatch.setenv("DESKTOP_CODE_SIGNING_P12", base64.b64encode(wrong).decode())
    with pytest.raises(signing.SigningError, match="固定证书不符"):
        signing.signing_material(now=NOW)
    assert not any(name in os.environ for name in signing.SECRET_NAMES)


@pytest.mark.parametrize(
    "now", [START - timedelta(microseconds=1), END + timedelta(microseconds=1)]
)
def test_certificate_validity_uses_explicit_time(signing, material, now):
    with pytest.raises(signing.SigningError, match="有效期"):
        signing.signing_material(now=now)


@pytest.mark.parametrize("now", [START, END])
def test_certificate_validity_boundary_is_accepted(signing, material, now):
    selected = signing.signing_material(now=now)
    assert selected.container == material[0]
    assert PASSWORD not in repr(selected)


def test_certificate_must_be_for_code_signing(signing, material, tmp_path, monkeypatch):
    wrong = tmp_path / "server.pem"
    synthetic_certificate(wrong, signing_usage=False)
    monkeypatch.setattr(signing, "CERTIFICATE", wrong)
    with pytest.raises(signing.SigningError, match="代码签名用途"):
        signing.signing_material(now=NOW)


def test_session_preserves_borrowed_resources_and_is_idempotently_cleaned(
    signing, isolated
):
    system, state = isolated
    previous = signal.getsignal(signal.SIGTERM)
    with signing.SigningSession(state) as identity:
        assert identity.keychain.exists()
        assert system.search == [*system.original_search, str(identity.keychain)]
        assert not (state / "identity.p12").exists()
        assert PASSWORD not in (state / "state.json").read_text()
        assert not any(name in os.environ for name in signing.SECRET_NAMES)
    assert not state.exists()
    assert system.search == system.original_search
    assert system.trust == {"user": {"A" * 40: {"certName": "borrowed"}}, "admin": {}}
    assert signal.getsignal(signal.SIGTERM) == previous
    events = list(system.events)
    signing.cleanup(state)
    assert system.events == events


def test_import_failure_cleans_initialized_resources(signing, isolated):
    system, state = isolated
    system.fail_import = True
    with pytest.raises(signing.SigningError, match="导入固定签名身份失败"):
        with signing.SigningSession(state):
            pytest.fail("must not enter a failed signing session")
    assert not state.exists()
    assert system.search == system.original_search
    assert "delete-keychain" in system.events


@pytest.mark.parametrize("cancelled", [False, True])
def test_build_failure_and_cancel_restore_resources(signing, isolated, cancelled):
    system, state = isolated
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt if cancelled else RuntimeError):
        with signing.SigningSession(state):
            if cancelled:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
            raise RuntimeError("synthetic build failure")
    assert not state.exists()
    assert system.search == system.original_search
    assert signal.getsignal(signal.SIGTERM) == previous


def test_snapshot_failure_does_not_block_cleanup_and_keeps_recoverable_state(
    signing, isolated
):
    system, state = isolated
    with pytest.raises(signing.SigningError, match="管理员域信任"):
        with signing.SigningSession(state):
            system.fail_admin_read = True
    assert not (state / "owned.keychain-db").exists()
    assert (state / "state.json").is_file()
    assert system.search == system.original_search
    system.fail_admin_read = False
    signing.cleanup(state)
    assert not state.exists()


@pytest.mark.parametrize(
    "field,value", [("run_id", "9999"), ("certificate_sha256", "0" * 64)]
)
def test_foreign_journal_is_not_used_to_delete_resources(
    signing, isolated, field, value
):
    system, state = isolated
    original = b""
    with pytest.raises(signing.SigningError, match="资源归属不符"):
        with signing.SigningSession(state):
            original = (state / "state.json").read_bytes()
            changed = json.loads(original)
            changed[field] = value
            (state / "state.json").write_text(json.dumps(changed))
    assert (state / "owned.keychain-db").exists()
    assert "delete-keychain" not in system.events
    (state / "state.json").write_bytes(original)
    signing.cleanup(state)
    assert not state.exists()


def test_unknown_state_path_is_rejected_without_commands(signing, isolated):
    system, state = isolated
    with pytest.raises(signing.SigningError, match="不属于本轮"):
        signing.cleanup(state.with_name("unowned-state"))
    assert not system.events


def test_runner_guard_rejects_local_and_self_hosted_execution(signing, monkeypatch):
    monkeypatch.setattr(signing, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    monkeypatch.setenv("GITHUB_JOB", "release")
    with pytest.raises(signing.SigningError, match="GitHub 托管"):
        signing.runner_scope()


def test_command_errors_never_expose_secret_arguments_or_environment(
    signing, material, monkeypatch
):
    def run(arguments, **kwargs):
        assert not any(name in kwargs["env"] for name in signing.SECRET_NAMES)
        return subprocess.CompletedProcess(arguments, 1, PASSWORD, PASSWORD)

    monkeypatch.setattr(signing.subprocess, "run", run)
    with pytest.raises(signing.SigningError) as failure:
        signing.checked(
            "导入",
            "security",
            "import",
            "synthetic.p12",
            "-P",
            PASSWORD,
            redactions=(PASSWORD,),
        )
    assert PASSWORD not in str(failure.value)
    assert "<redacted>" in str(failure.value)

    def timeout(arguments, **kwargs):
        raise subprocess.TimeoutExpired(arguments, 60, stderr=PASSWORD)

    monkeypatch.setattr(signing.subprocess, "run", timeout)
    with pytest.raises(signing.SigningError) as failure:
        signing.checked(
            "导入", "security", "import", "-P", PASSWORD, redactions=(PASSWORD,)
        )
    assert PASSWORD not in str(failure.value)


def test_build_calls_real_builder_contract_then_verifies_after_cleanup(
    signing, isolated, monkeypatch, tmp_path
):
    system, state = isolated
    builder = importlib.import_module("build_macos")
    inputs = importlib.import_module("runtime_inputs")
    output = tmp_path / "TinkerFin.app"
    called = []

    def build_app(
        *, output, python_archive, node_archive, identity, keychain, timestamp
    ):
        assert keychain.exists() and identity
        assert timestamp is False
        assert not any(name in os.environ for name in signing.SECRET_NAMES)
        output.mkdir()
        called.append("build")
        return output

    def verify(app):
        assert app == output
        assert not state.exists()
        assert system.search == system.original_search
        called.append("verify")

    monkeypatch.setattr(builder, "build", build_app)
    monkeypatch.setattr(
        inputs, "obtain", lambda source, cache: tmp_path / "synthetic-runtime"
    )
    monkeypatch.setattr(signing, "verify", verify)
    signing.build(output, state)
    assert called == ["build", "verify"]


@pytest.mark.parametrize("different_certificate", [False, True])
def test_verify_enforces_pinned_certificate_after_strict_validation(
    signing, material, monkeypatch, tmp_path, different_certificate
):
    monkeypatch.setattr(signing, "sys", SimpleNamespace(platform="darwin"))
    seen = []

    def command(*arguments, redactions=()):
        assert not any(name in os.environ for name in signing.SECRET_NAMES)
        args = list(map(str, arguments))
        seen.append(args)
        extracts = [
            argument
            for argument in args
            if argument.startswith("--extract-certificates=")
        ]
        if extracts:
            prefix = Path(extracts[0].split("=", 1)[1])
            prefix.with_name(prefix.name + "0").write_bytes(
                b"different certificate" if different_certificate else material[1]
            )
        return signing.CommandResult(0, "", "")

    monkeypatch.setattr(signing, "command", command)
    if different_certificate:
        with pytest.raises(signing.SigningError, match="固定发布证书"):
            signing.verify(tmp_path / "TinkerFin.app")
    else:
        signing.verify(tmp_path / "TinkerFin.app")
        assert any("certificate leaf" in argument for argument in seen[-1])
    assert "--strict" in seen[0]
