"""为官方桌面发行包签署 Ed25519 清单，私钥只从发布环境读取"""

from __future__ import annotations

import argparse
import base64
import json
import os
import plistlib
import re
import tomllib
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from update_archive import MAX_ARCHIVE, file_digest, tree_digest

DESKTOP = Path(__file__).resolve().parents[1]


def manifest(app: Path, archive: Path) -> bytes:
    """只声明当前桌面版本和官方仓库中的固定发行资产"""
    release = tomllib.loads((DESKTOP / "pyproject.toml").read_text())["project"][
        "version"
    ]
    if not re.fullmatch(
        r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})", release
    ):
        raise ValueError("桌面发行版本无效")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if not isinstance(info, dict) or (
        info.get("CFBundleIdentifier") != "com.tinkerfin.companion"
        or info.get("CFBundleShortVersionString") != release
        or info.get("CFBundleExecutable") != "TinkerFin"
    ):
        raise ValueError("发行应用身份与当前桌面版本不符")
    size = archive.stat().st_size
    if not 0 < size <= MAX_ARCHIVE:
        raise ValueError("发行包超过更新容量")
    payload = {
        "version": release,
        "target": "macos-arm64",
        "bundle_id": "com.tinkerfin.companion",
        "archive_url": f"https://github.com/tinkerfin-ai/tinkerfin-harness/releases/download/desktop-v{release}/TinkerFin.zip",
        "archive_size": size,
        "archive_sha256": file_digest(archive),
        "tree_sha256": tree_digest(app),
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sign(app: Path, archive: Path, output: Path) -> None:
    """拒绝与客户端固定公钥不对应的发布私钥"""
    private = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(os.environ["DESKTOP_UPDATE_SIGNING_KEY"], validate=True)
    )
    public = base64.b64decode(
        (DESKTOP / "resources/update-public-key.txt").read_text().strip(), validate=True
    )
    if private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) != public:
        raise ValueError("发布私钥与客户端信任公钥不符")
    content = manifest(app, archive)
    output.write_bytes(content)
    output.with_suffix(output.suffix + ".sig").write_bytes(private.sign(content))


def verify(app: Path, archive: Path, output: Path) -> None:
    """发布附件需同时满足固定公钥签名和本轮实际应用、ZIP的摘要"""
    public = base64.b64decode(
        (DESKTOP / "resources/update-public-key.txt").read_text().strip(), validate=True
    )
    content = output.read_bytes()
    Ed25519PublicKey.from_public_bytes(public).verify(
        output.with_suffix(output.suffix + ".sig").read_bytes(), content
    )
    if content != manifest(app, archive):
        raise ValueError("发布附件与本轮应用不符")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    (verify if args.verify else sign)(args.app, args.archive, args.output)


if __name__ == "__main__":
    main()
