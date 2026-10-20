"""本机集成测试拥有的原生运行时入口和独立 TLS 信任"""

import os
import shutil
import ssl
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@pytest.fixture
def native_sandbox() -> Path:
    if sys.platform != "darwin":
        pytest.skip("本机端到端任务需要 macOS 原生沙箱")
    selected = os.environ.get("TINKERFIN_TEST_SRT")
    path = (
        Path(selected)
        if selected
        else Path(__file__).resolve().parents[1]
        / "runtime/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
    )
    if not path.is_file():
        pytest.skip("需要安装固定版本原生沙箱")
    return path


@pytest.fixture
def tls_context(tmp_path, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2000, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    certificate = tmp_path / "test-cert.pem"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key = tmp_path / "test-key.pem"
    private_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    return context


@pytest.fixture(scope="module")
def swift_contracts(tmp_path_factory) -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("原生客户端契约需要 macOS Swift SDK")
    companion = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("swift-client") / "contracts"
    sources = [
        path
        for path in (companion / "native").glob("*.swift")
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
            str(companion / "tests/native/Contracts.swift"),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return binary
