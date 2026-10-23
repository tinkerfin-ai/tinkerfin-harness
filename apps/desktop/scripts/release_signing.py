"""在独占发布 runner 使用固定证书签名，清理钥匙串后才验收发行应用"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import plistlib
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, TracebackType

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DESKTOP = Path(__file__).resolve().parents[1]
CERTIFICATE = DESKTOP / "resources/code-signing.pem"
BUNDLE_ID = "com.tinkerfin.companion"
SECRET_NAMES = ("DESKTOP_CODE_SIGNING_P12", "DESKTOP_CODE_SIGNING_PASSWORD")


class SigningError(RuntimeError):
    """可直接报告的签名失败，不包含私钥、密码或子进程参数"""


@dataclass(frozen=True, slots=True)
class RunnerScope:
    temporary: Path
    run_id: str
    attempt: str
    uid: int


@dataclass(frozen=True, slots=True)
class SigningIdentity:
    fingerprint: str
    keychain: Path


@dataclass(frozen=True, slots=True)
class SigningMaterial:
    container: bytes = field(repr=False)
    password: str = field(repr=False)
    certificate: bytes
    sha1: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class TrustSnapshot:
    digest: str
    fingerprints: frozenset[str]


class SigningJournal(BaseModel):
    """只记录本轮资源归属和原环境，不保存任何签名秘密"""

    model_config = ConfigDict(extra="forbid", strict=True)
    directory: str
    run_id: str
    attempt: str
    uid: int = Field(ge=0)
    certificate_sha1: str = Field(pattern=r"^[0-9A-F]{40}$")
    certificate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_list: list[str]
    default_keychain: str
    user_trust_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    admin_trust_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def runner_scope() -> RunnerScope:
    if (
        sys.platform != "darwin"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or os.environ.get("GITHUB_JOB") != "release"
    ):
        raise SigningError("签名资源只允许在 GitHub 托管的 macOS release job 中使用")
    run_id, attempt = (
        os.environ.get("GITHUB_RUN_ID", ""),
        os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    )
    raw = os.environ.get("RUNNER_TEMP", "")
    if not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(
        r"[1-9][0-9]*", attempt
    ):
        raise SigningError("发布运行归属缺失")
    temporary = Path(raw)
    if not raw or not temporary.is_absolute() or not temporary.is_dir():
        raise SigningError("发布临时目录无效")
    return RunnerScope(temporary.resolve(), run_id, attempt, os.getuid())


def state_path(path: Path, scope: RunnerScope) -> Path:
    expected = (
        scope.temporary / f"tinkerfin-release-signing-{scope.run_id}-{scope.attempt}"
    )
    if path.is_symlink() or path.absolute() != expected:
        raise SigningError("签名状态目录不属于本轮发布")
    if path.exists():
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != scope.uid
            or info.st_mode & 0o077
        ):
            raise SigningError("签名状态目录归属或权限无效")
    return expected


def command(*arguments: str | Path, redactions: tuple[str, ...] = ()) -> CommandResult:
    environment = {
        key: value for key, value in os.environ.items() if key not in SECRET_NAMES
    }
    try:
        result = subprocess.run(
            [str(argument) for argument in arguments],
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(-1, "", "命令未在操作预算内完成")
    except OSError:
        return CommandResult(-1, "", "无法启动签名命令")
    stdout, stderr = result.stdout, result.stderr
    for value in redactions:
        if value:
            stdout, stderr = (
                stdout.replace(value, "<redacted>"),
                stderr.replace(value, "<redacted>"),
            )
    return CommandResult(result.returncode, stdout, stderr)


def checked(
    label: str, *arguments: str | Path, redactions: tuple[str, ...] = ()
) -> str:
    result = command(*arguments, redactions=redactions)
    if result.code:
        raise SigningError(f"{label}失败（{result.code}）：{result.stderr.strip()}")
    return result.stdout


def pinned_certificate() -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(CERTIFICATE.read_bytes())
    except (OSError, ValueError):
        raise SigningError("固定签名证书缺失或格式无效") from None


def signing_material(*, now: datetime) -> SigningMaterial:
    encoded = os.environ.pop(SECRET_NAMES[0], "")
    password = os.environ.pop(SECRET_NAMES[1], "")
    if not encoded or not password:
        raise SigningError("发布签名 Secrets 未配置")
    certificate = pinned_certificate()
    if (
        now.utcoffset() is None
        or not certificate.not_valid_before_utc
        <= now
        <= certificate.not_valid_after_utc
    ):
        raise SigningError("固定签名证书不在有效期内")
    try:
        usage = certificate.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        ).value
    except x509.ExtensionNotFound:
        raise SigningError("固定证书未限定代码签名用途") from None
    if set(usage) != {ExtendedKeyUsageOID.CODE_SIGNING}:
        raise SigningError("固定证书未限定代码签名用途")
    try:
        container = base64.b64decode(encoded, validate=True)
        private, supplied, additional = pkcs12.load_key_and_certificates(
            container, password.encode()
        )
    except (ValueError, TypeError, binascii.Error, UnsupportedAlgorithm):
        raise SigningError("发布签名容器或密码无效") from None
    expected = certificate.public_bytes(serialization.Encoding.DER)
    if (
        private is None
        or supplied is None
        or additional
        or supplied.public_bytes(serialization.Encoding.DER) != expected
    ):
        raise SigningError("发布签名容器与固定证书不符")
    if private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) != certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ):
        raise SigningError("发布签名私钥与固定证书不符")
    return SigningMaterial(
        container,
        password,
        certificate.public_bytes(serialization.Encoding.PEM),
        certificate.fingerprint(hashes.SHA1()).hex().upper(),
        hashlib.sha256(expected).hexdigest(),
    )


def keychain_list() -> list[str]:
    paths = shlex.split(
        checked("读取钥匙串搜索清单", "security", "list-keychains", "-d", "user")
    )
    if any(not Path(path).is_absolute() for path in paths):
        raise SigningError("钥匙串搜索清单无效")
    return paths


def default_keychain() -> str:
    paths = shlex.split(
        checked("读取默认钥匙串", "security", "default-keychain", "-d", "user")
    )
    if len(paths) != 1 or not Path(paths[0]).is_absolute():
        raise SigningError("默认钥匙串无效")
    return paths[0]


def trust_snapshot(directory: Path, *, admin: bool) -> TrustSnapshot:
    destination = directory / ("observe-admin.plist" if admin else "observe-user.plist")
    if destination.is_symlink():
        raise SigningError("签名状态中出现符号链接")
    destination.unlink(missing_ok=True)
    try:
        result = command(
            "security", "trust-settings-export", *(["-d"] if admin else []), destination
        )
        if result.code and "no trust settings were found" in result.stderr.lower():
            entries = {}
        elif result.code:
            raise SigningError("无法读取证书信任快照")
        else:
            payload = plistlib.loads(destination.read_bytes())
            if not isinstance(payload, dict) or not isinstance(
                payload.get("trustList"), dict
            ):
                raise SigningError("证书信任快照格式无效")
            entries = payload["trustList"]
        if any(not isinstance(key, str) for key in entries):
            raise SigningError("证书信任指纹无效")
        return TrustSnapshot(
            hashlib.sha256(plistlib.dumps(entries, sort_keys=True)).hexdigest(),
            frozenset(key.upper() for key in entries),
        )
    except (OSError, ValueError):
        raise SigningError("证书信任快照不可读") from None
    finally:
        destination.unlink(missing_ok=True)


def write_owned(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def read_owned(path: Path, scope: RunnerScope) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != scope.uid:
            raise SigningError("签名状态文件归属无效")
        return stream.read()


def prepare(
    directory: Path, scope: RunnerScope, material: SigningMaterial
) -> SigningIdentity:
    directory.mkdir(mode=0o700)
    journal_written = False
    container = directory / "identity.p12"
    try:
        keychain = directory / "owned.keychain-db"
        search = keychain_list()
        user, admin = (
            trust_snapshot(directory, admin=False),
            trust_snapshot(directory, admin=True),
        )
        if (
            str(keychain) in search
            or material.sha1 in user.fingerprints | admin.fingerprints
        ):
            raise SigningError("固定签名身份或钥匙串已被其他环境持有")
        journal = SigningJournal(
            directory=str(directory),
            run_id=scope.run_id,
            attempt=scope.attempt,
            uid=scope.uid,
            certificate_sha1=material.sha1,
            certificate_sha256=material.sha256,
            search_list=search,
            default_keychain=default_keychain(),
            user_trust_sha256=user.digest,
            admin_trust_sha256=admin.digest,
        )
        write_owned(directory / "certificate.pem", material.certificate)
        # 外部资源创建前持久化归属，进程中断后 always() 仍能精确结算
        write_owned(directory / "state.json", journal.model_dump_json().encode())
        journal_written = True
        password = secrets.token_urlsafe(32)
        checked(
            "创建签名钥匙串",
            "security",
            "create-keychain",
            "-p",
            password,
            keychain,
            redactions=(password,),
        )
        checked("设置独占钥匙串", "security", "set-keychain-settings", keychain)
        checked(
            "追加签名钥匙串",
            "security",
            "list-keychains",
            "-d",
            "user",
            "-s",
            *search,
            keychain,
        )
        write_owned(container, material.container)
        try:
            checked(
                "导入固定签名身份",
                "security",
                "import",
                container,
                "-k",
                keychain,
                "-P",
                material.password,
                "-T",
                "/usr/bin/codesign",
                redactions=(material.password,),
            )
        finally:
            container.unlink(missing_ok=True)
        checked(
            "允许签名工具使用独占私钥",
            "security",
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:",
            "-s",
            "-k",
            password,
            keychain,
            redactions=(password,),
        )
        return SigningIdentity(material.sha1, keychain)
    except BaseException:
        if not journal_written:
            shutil.rmtree(directory)
        raise


def load_journal(directory: Path, scope: RunnerScope) -> SigningJournal:
    try:
        journal = SigningJournal.model_validate_json(
            read_owned(directory / "state.json", scope)
        )
        certificate = x509.load_pem_x509_certificate(
            read_owned(directory / "certificate.pem", scope)
        )
    except (OSError, ValueError, ValidationError):
        raise SigningError("签名状态不可读，拒绝清理未知资源") from None
    der = certificate.public_bytes(serialization.Encoding.DER)
    if (
        (journal.directory, journal.run_id, journal.attempt, journal.uid)
        != (str(directory), scope.run_id, scope.attempt, scope.uid)
        or certificate.fingerprint(hashes.SHA1()).hex().upper()
        != journal.certificate_sha1
        or hashlib.sha256(der).hexdigest() != journal.certificate_sha256
        or der != pinned_certificate().public_bytes(serialization.Encoding.DER)
        or any(not Path(path).is_absolute() for path in journal.search_list)
        or str(directory / "owned.keychain-db") in journal.search_list
        or not Path(journal.default_keychain).is_absolute()
    ):
        raise SigningError("签名状态与本轮资源归属不符")
    return journal


def cleanup(state: Path) -> None:
    """恢复原搜索清单并删除本轮钥匙串；任一步失败保留公开状态供补偿清理"""
    for name in SECRET_NAMES:
        os.environ.pop(name, None)
    scope = runner_scope()
    directory = state_path(state, scope)
    if not directory.exists():
        return
    journal = load_journal(directory, scope)
    failures: list[str] = []
    keychain = directory / "owned.keychain-db"
    if keychain.is_symlink() or (directory / "identity.p12").is_symlink():
        raise SigningError("签名资源出现符号链接，拒绝清理")
    (directory / "identity.p12").unlink(missing_ok=True)
    result = command(
        "security", "list-keychains", "-d", "user", "-s", *journal.search_list
    )
    if result.code:
        failures.append("恢复钥匙串搜索清单失败")
    if keychain.exists():
        result = command("security", "delete-keychain", keychain)
        if result.code:
            failures.append("删除独占签名钥匙串失败")
    if keychain.exists():
        failures.append("独占签名钥匙串仍存在")
    # 先完成所有资源回收，再读取快照；查询失败不能阻止钥匙串删除
    checks = (
        ("搜索清单", lambda: keychain_list() == journal.search_list),
        ("默认钥匙串", lambda: default_keychain() == journal.default_keychain),
        (
            "用户域信任",
            lambda: (
                trust_snapshot(directory, admin=False).digest
                == journal.user_trust_sha256
            ),
        ),
        (
            "管理员域信任",
            lambda: (
                trust_snapshot(directory, admin=True).digest
                == journal.admin_trust_sha256
            ),
        ),
    )
    for label, check in checks:
        try:
            if not check():
                failures.append(f"{label}与构建前不同")
        except SigningError:
            failures.append(f"无法核验{label}")
    if failures:
        raise SigningError("；".join(failures))
    shutil.rmtree(directory)


class SigningSession:
    """拥有单轮签名钥匙串，进入时消费 Secrets，退出或取消时恢复原环境"""

    def __init__(self, state: Path) -> None:
        self.state = state
        self._previous_sigterm = signal.getsignal(signal.SIGTERM)

    @staticmethod
    def _interrupted(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt("发布签名已取消")

    def __enter__(self) -> SigningIdentity:
        material = signing_material(now=datetime.now(UTC))
        scope = runner_scope()
        self.state = state_path(self.state, scope)
        if self.state.exists():
            raise SigningError("签名状态已存在，请先完成本轮清理")
        signal.signal(signal.SIGTERM, self._interrupted)
        try:
            return prepare(self.state, scope, material)
        except BaseException:
            try:
                if self.state.exists():
                    cleanup(self.state)
            finally:
                signal.signal(signal.SIGTERM, self._previous_sigterm)
            raise

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            cleanup(self.state)
        finally:
            signal.signal(signal.SIGTERM, self._previous_sigterm)


def verify(app: Path) -> None:
    """校验完整签名、固定证书和应用标识，不安装任何证书信任"""
    for name in SECRET_NAMES:
        os.environ.pop(name, None)
    if sys.platform != "darwin":
        raise SigningError("应用签名校验需要 macOS")
    certificate = pinned_certificate()
    checked("严格校验应用", "codesign", "--verify", "--deep", "--strict", app)
    with tempfile.TemporaryDirectory(prefix="tinkerfin-signature-") as raw:
        prefix = Path(raw) / "certificate"
        checked(
            "提取应用签名证书",
            "codesign",
            "--display",
            "--extract-certificates=" + str(prefix),
            app,
        )
        if not prefix.with_name(prefix.name + "0").is_file() or prefix.with_name(
            prefix.name + "0"
        ).read_bytes() != certificate.public_bytes(serialization.Encoding.DER):
            raise SigningError("应用未使用固定发布证书签名")
    fingerprint = certificate.fingerprint(hashes.SHA1()).hex().upper()
    checked(
        "校验发布身份",
        "codesign",
        "--verify",
        "--strict",
        "-R=" + f'identifier "{BUNDLE_ID}" and certificate leaf = H"{fingerprint}"',
        app,
    )


def build(output: Path, state: Path) -> None:
    """清理成功后才验收应用；构建失败或取消仍回收本轮签名资源"""
    with SigningSession(state) as identity:
        from build_macos import build as build_app
        from runtime_inputs import DEFAULT_CACHE, NODE, PYTHON, obtain

        build_app(
            output=output,
            python_archive=obtain(PYTHON, DEFAULT_CACHE),
            node_archive=obtain(NODE, DEFAULT_CACHE),
            identity=identity.fingerprint,
            keychain=identity.keychain,
            timestamp=False,
        )
    verify(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    build_parser = subcommands.add_parser("build")
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--state", type=Path, required=True)
    cleanup_parser = subcommands.add_parser("cleanup")
    cleanup_parser.add_argument("--state", type=Path, required=True)
    verify_parser = subcommands.add_parser("verify")
    verify_parser.add_argument("--app", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "build":
            build(arguments.output, arguments.state)
        elif arguments.command == "cleanup":
            cleanup(arguments.state)
        else:
            verify(arguments.app)
    except SigningError as error:
        parser.exit(1, f"{error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "发布签名已取消\n")


if __name__ == "__main__":
    main()
