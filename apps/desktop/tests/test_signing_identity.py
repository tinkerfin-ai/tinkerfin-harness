"""仅在独占 GitHub macOS runner 验证正式签名入口及合成凭证的身份隔离"""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Literal
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import BaseModel, ConfigDict


def isolated_runner() -> bool:
    return (
        sys.platform == "darwin"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("GITHUB_JOB") in {"desktop", "release"}
    )


pytestmark = [
    pytest.mark.companion_integration,
    pytest.mark.skipif(
        not isolated_runner(),
        reason="签名信任实验仅允许 GitHub 托管 macOS 的 desktop/release 独占 job",
    ),
]


@dataclass(frozen=True, slots=True)
class CommandResult:
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class SigningApps:
    source: Path
    target: Path
    unrelated: Path
    keychain: Path
    service: str


@dataclass(frozen=True, slots=True)
class TrustSettings:
    fingerprints: frozenset[str]
    serialized: bytes


class ACLRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    authorizations: list[str]
    description: str
    applicationCount: int


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    build: Literal["A", "B"]
    status: int
    length: int | None = None
    acl: list[ACLRecord] | None = None


def command(*arguments: str | Path, password: str = "") -> CommandResult:
    __tracebackhide__ = True
    try:
        result = subprocess.run(
            [str(argument) for argument in arguments],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(-1, "", "命令未在操作预算内结束")
    stdout, stderr = result.stdout, result.stderr
    if password:
        stdout = stdout.replace(password, "<redacted>")
        stderr = stderr.replace(password, "<redacted>")
    return CommandResult(result.returncode, stdout, stderr)


def checked(label: str, *arguments: str | Path, password: str = "") -> str:
    __tracebackhide__ = True
    result = command(*arguments, password=password)
    if result.code:
        pytest.fail(f"{label}: exit {result.code}; {result.stderr}", pytrace=False)
    return result.stdout


def trust_settings(root: Path, label: str, *, admin: bool = False) -> TrustSettings:
    destination = root / f"trust-{label}.plist"
    result = command(
        "security", "trust-settings-export", *(["-d"] if admin else []), destination
    )
    if result.code and "no trust settings were found" in result.stderr.lower():
        return TrustSettings(frozenset(), plistlib.dumps({}, sort_keys=True))
    assert result.code == 0, result.stderr
    parsed = plistlib.loads(destination.read_bytes())
    assert isinstance(parsed, dict) and isinstance(parsed.get("trustList"), dict)
    entries = parsed["trustList"]
    assert all(isinstance(fingerprint, str) for fingerprint in entries)
    return TrustSettings(
        frozenset(fingerprint.upper() for fingerprint in entries),
        plistlib.dumps(entries, sort_keys=True),
    )


def remove_owned_trust(root: Path, pem: Path, fingerprint: str) -> CommandResult:
    removed = command(
        "sudo", "-n", "/usr/bin/security", "remove-trusted-cert", "-d", pem
    )
    # remove-trusted-cert 的缺失诊断也可能返回 1，不能把其他 Security 命令的退出码当作此命令契约
    not_found = (
        removed.stderr.strip()
        == "SecTrustSettingsRemoveTrustSettings: The specified item could not be found in the keychain."
    )
    if removed.code and not not_found:
        return removed
    after = trust_settings(root, "remove-after", admin=True)
    if fingerprint in after.fingerprints:
        if removed.code:
            return removed
        return CommandResult(-1, "", "独占合成证书的管理员域信任仍存在")
    return CommandResult(0, removed.stdout, "")


def certificate(root: Path, name: str, password: str) -> tuple[Path, Path, str]:
    __tracebackhide__ = True
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    signed = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2000, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=True
        )
        .sign(private, hashes.SHA256())
    )
    pem, container = root / f"{name}.pem", root / f"{name}.p12"
    pem.write_bytes(signed.public_bytes(serialization.Encoding.PEM))
    encryption = (
        serialization.PrivateFormat.PKCS12.encryption_builder()
        .kdf_rounds(20_000)
        .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
        .hmac_hash(hashes.SHA1())
        .build(password.encode())
    )
    container.write_bytes(
        pkcs12.serialize_key_and_certificates(
            name.encode(), private, signed, None, encryption
        )
    )
    container.chmod(0o600)
    return pem, container, signed.fingerprint(hashes.SHA1()).hex().upper()


def build_app(root: Path, name: str, version: str, source: Path) -> Path:
    app = root / name / "Probe.app"
    executable = app / "Contents/MacOS/Probe"
    executable.parent.mkdir(parents=True)
    checked(
        f"编译 {name}",
        "swiftc",
        "-parse-as-library",
        "-swift-version",
        "6",
        "-D",
        f"BUILD_{name}",
        source,
        "-o",
        executable,
    )
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.tinkerfin.signing-experiment",
                "CFBundleExecutable": "Probe",
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": version,
            }
        )
    )
    return app


@pytest.fixture(scope="module")
def signing_apps(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SigningApps]:
    __tracebackhide__ = True
    if not isolated_runner():
        pytest.skip("禁止在本机或自托管 runner 创建签名信任")
    root = tmp_path_factory.mktemp("signing-identity").resolve()
    root.chmod(0o700)
    desktop = Path(__file__).resolve().parents[1]
    original_search = checked(
        "读取搜索清单", "security", "list-keychains", "-d", "user"
    )
    original_default = checked(
        "读取默认钥匙串", "security", "default-keychain", "-d", "user"
    )
    original_user_trust = trust_settings(root, "user-before")
    original_admin_trust = trust_settings(root, "admin-before", admin=True)
    keychain = root / "owned.keychain-db"
    password = secrets.token_hex(32)
    trusted_certificates: list[tuple[Path, str]] = []
    created = False
    original_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupted(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt(f"取消独占签名测试：{signum}")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        checked(
            "创建独占钥匙串",
            "security",
            "create-keychain",
            "-p",
            password,
            keychain,
            password=password,
        )
        created = True
        # codesign 的证书链检索仍需要搜索清单；保留全部借用项并在 finally 原样恢复
        checked(
            "追加独占钥匙串",
            "security",
            "list-keychains",
            "-d",
            "user",
            "-s",
            *shlex.split(original_search),
            keychain,
        )
        identities = []
        for role in ("A", "C"):
            pem, container, fingerprint = certificate(
                root, f"TF-sign-{role}-{uuid4().hex[:12]}", password
            )
            assert fingerprint not in original_user_trust.fingerprints
            assert fingerprint not in original_admin_trust.fingerprints
            checked(
                "导入合成身份",
                "security",
                "import",
                container,
                "-k",
                keychain,
                "-P",
                password,
                "-T",
                "/usr/bin/codesign",
                password=password,
            )
            trusted_certificates.append((pem, fingerprint))
            # SecTrustSettings.h 的 Authentication 契约：用户域需要 GUI；管理员域 root 无需弹窗
            checked(
                "信任合成签名证书",
                "sudo",
                "-n",
                "/usr/bin/security",
                "add-trusted-cert",
                "-d",
                "-r",
                "trustRoot",
                "-p",
                "codeSign",
                "-k",
                keychain,
                pem,
            )
            identities.append(fingerprint)
        checked(
            "允许系统签名工具访问测试私钥",
            "security",
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:",
            "-s",
            "-k",
            password,
            keychain,
            password=password,
        )
        source = desktop / "tests/native/SigningIdentityContracts.swift"
        a = build_app(root, "A", "1", source)
        b = build_app(root, "B", "2", source)
        assert (a / "Contents/MacOS/Probe").read_bytes() != (
            b / "Contents/MacOS/Probe"
        ).read_bytes()
        c = root / "C/Probe.app"
        shutil.copytree(b, c)
        scripts = desktop / "scripts"
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.syspath_prepend(str(scripts))
            spec = importlib.util.spec_from_file_location(
                "build_macos_signing_test", scripts / "build_macos.py"
            )
            assert spec is not None and spec.loader is not None
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            for app, identity in (
                (a, identities[0]),
                (b, identities[0]),
                (c, identities[1]),
            ):
                builder.sign(app, identity, root, keychain=keychain, timestamp=False)
        # 用户端不安装签名证书；验收前先撤回构建机信任和搜索路径，验证包内身份自足
        while trusted_certificates:
            removed = remove_owned_trust(root, *trusted_certificates[-1])
            assert removed.code == 0, removed.stderr
            trusted_certificates.pop()
        checked(
            "恢复构建前搜索清单",
            "security",
            "list-keychains",
            "-d",
            "user",
            "-s",
            *shlex.split(original_search),
        )
        assert trust_settings(root, "user-before-verification") == original_user_trust
        assert (
            trust_settings(root, "admin-before-verification", admin=True)
            == original_admin_trust
        )
        yield SigningApps(
            a, b, c, keychain, "com.tinkerfin.signing-test." + uuid4().hex
        )
    finally:
        failures = []
        for pem, fingerprint in trusted_certificates:
            try:
                result = remove_owned_trust(root, pem, fingerprint)
                if result.code:
                    failures.append(f"撤回合成证书信任：{result.code} {result.stderr}")
            except (AssertionError, OSError, ValueError) as error:
                failures.append(f"核对合成证书信任失败：{error}")
        if created or keychain.exists():
            result = command(
                "security",
                "list-keychains",
                "-d",
                "user",
                "-s",
                *shlex.split(original_search),
            )
            if result.code:
                failures.append(f"恢复搜索清单：{result.code} {result.stderr}")
            result = command("security", "delete-keychain", keychain)
            if result.code:
                failures.append(f"删除独占钥匙串：{result.code} {result.stderr}")
        if keychain.exists():
            failures.append("独占钥匙串文件仍存在")
        if (
            command("security", "list-keychains", "-d", "user").stdout
            != original_search
        ):
            failures.append("钥匙串搜索清单未恢复")
        if (
            command("security", "default-keychain", "-d", "user").stdout
            != original_default
        ):
            failures.append("默认钥匙串发生变化")
        try:
            if trust_settings(root, "user-after") != original_user_trust:
                failures.append("用户域证书信任未恢复")
            if trust_settings(root, "admin-after", admin=True) != original_admin_trust:
                failures.append("管理员域证书信任未恢复")
        finally:
            signal.signal(signal.SIGTERM, original_sigterm)
            shutil.rmtree(root)
        if failures:
            pytest.fail("；".join(failures), pytrace=False)


def requirement(app: Path) -> str:
    result = command("codesign", "-d", "-r-", app)
    assert result.code == 0, result.stderr
    return next(
        line.removeprefix("# ").removeprefix("designated => ")
        for line in result.stdout.splitlines()
        if line.removeprefix("# ").startswith("designated => ")
    )


def test_same_signer_retains_identity_and_other_signer_is_rejected(
    signing_apps: SigningApps,
):
    source, target, unrelated = (
        signing_apps.source,
        signing_apps.target,
        signing_apps.unrelated,
    )
    requirements = [requirement(app) for app in (source, target, unrelated)]
    assert requirements[0] == requirements[1]
    assert requirements[0] != requirements[2]
    for app in (source, target, unrelated):
        checked("严格校验", "codesign", "--verify", "--deep", "--strict", app)
        metadata = command("codesign", "-d", "--verbose=4", app)
        assert metadata.code == 0
        assert "Timestamp=" not in metadata.stderr
    checked(
        "目标满足源身份",
        "codesign",
        "--verify",
        "--strict",
        "-R=" + requirements[0],
        target,
    )
    checked(
        "源满足目标身份",
        "codesign",
        "--verify",
        "--strict",
        "-R=" + requirements[1],
        source,
    )
    for app, other in ((unrelated, requirements[0]), (source, requirements[2])):
        rejected = command("codesign", "--verify", "--strict", "-R=" + other, app)
        assert rejected.code == 3, rejected.stderr


def probe(app: Path, mode: str, apps: SigningApps) -> ProbeResult:
    output = checked(
        "合成凭证实验",
        app / "Contents/MacOS/Probe",
        mode,
        apps.keychain,
        apps.service,
        "owned-account",
    )
    return ProbeResult.model_validate_json(output)


def test_keychain_rejects_other_signer_and_records_cross_build_access(
    signing_apps: SigningApps, capsys: pytest.CaptureFixture[str]
):
    created = probe(signing_apps.source, "create", signing_apps)
    assert created.status == 0
    source = probe(signing_apps.source, "read", signing_apps)
    assert source.status == 0 and source.length == 32
    acl = probe(signing_apps.source, "acl", signing_apps)
    assert acl.status == 0
    target = probe(signing_apps.target, "read", signing_apps)
    unrelated = probe(signing_apps.unrelated, "read", signing_apps)
    assert unrelated.status in {-25293, -25308}, unrelated
    # 跨构建读取由系统 partition ACL 决定；本实验记录事实，不预设 DR 通过就可读取
    with capsys.disabled():
        print(
            "self-signed-keychain="
            + json.dumps(
                {
                    "source": source.model_dump(),
                    "target": target.model_dump(),
                    "unrelated": unrelated.model_dump(),
                    "acl": acl.model_dump(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
