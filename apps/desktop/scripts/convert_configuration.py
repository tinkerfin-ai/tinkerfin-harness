"""客户端退出后一次性启用正常联网并撤销选定设备的旧工作区授权"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID

from pydantic import JsonValue, TypeAdapter

from tinkerfin_companion.configuration import WorkspacePermissions

_OBJECT = TypeAdapter(dict[str, JsonValue])


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("转换文件必须属于当前用户")
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise ValueError("转换文件超过容量")
    return content


def _directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("转换目录归属无效或被链接替换")


def _permissions(value: JsonValue) -> WorkspacePermissions:
    if not isinstance(value, dict) or set(value) != {
        "allowed_domains",
        "browser",
        "desktop",
        "mcp_servers",
    }:
        raise ValueError("配置不是待转换的域名授权形态")
    return WorkspacePermissions.model_validate({"network": "restricted", **value})


def _encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _replace(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".network-policy-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def convert(
    directory: Path, *, devices: Sequence[str], exclude_scopes: Sequence[str] = ()
) -> Path:
    """只转换显式设备；保留全部原文件并原位拒绝旧授权恢复

    调用前必须退出客户端并收束云端旧运行。排除的 scope 仅按文件名跳过，
    不读取其快照；数据库、目录索引、项目文件和 Keychain 均不在转换范围内。

    Args:
        directory: 包含 settings.json 与 state 子目录的应用配置根目录。
        devices: 本次核验后允许转换的设备标识。
        exclude_scopes: 保留原样且不读取的工作区授权标识。

    Returns:
        原文件备份与转换清单所在目录。
    """
    _directory(directory)
    directory = directory.resolve(strict=True)
    state_directory = directory / "state"
    _directory(state_directory)
    if not devices or any(str(UUID(device)) != device for device in devices):
        raise ValueError("必须明确指定规范 UUID 设备目录")
    if any(
        len(scope) != 64 or any(c not in "0123456789abcdef" for c in scope)
        for scope in exclude_scopes
    ):
        raise ValueError("排除的工作区身份无效")
    excluded = {
        hashlib.sha256(scope.encode()).hexdigest() + ".json" for scope in exclude_scopes
    }
    backup = state_directory / "network-policy-backup"
    if backup.exists() or backup.is_symlink():
        raise ValueError("转换备份已存在，必须先核验上次操作")
    with ExitStack() as resources:
        for lock in (directory / "application.lock", state_directory / "host.lock"):
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            resources.callback(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        settings = directory / "settings.json"
        original = _read(settings)
        saved = _OBJECT.validate_json(original, strict=True)
        allowed = {
            "server",
            "name",
            "pending",
            "keychainAccount",
            "device",
            "permissions",
            "retiredAccounts",
        }
        if set(saved) - allowed or not {
            "server",
            "name",
            "permissions",
            "retiredAccounts",
        }.issubset(saved):
            raise ValueError("配置不是待转换的当前客户端形态")
        policy = _permissions(saved.get("permissions"))
        current = {
            **saved,
            "permissions": policy.model_copy(
                update={"network": "normal", "allowed_domains": ()}
            ).model_dump(mode="json"),
        }
        changes: list[tuple[Path, bytes, bytes]] = []
        skipped: list[str] = []
        for device in dict.fromkeys(devices):
            state = state_directory / device
            _directory(state)
            snapshots = state / "workspace-permissions"
            if not snapshots.exists() and not snapshots.is_symlink():
                continue
            _directory(snapshots)
            for path in sorted(snapshots.iterdir()):
                if path.name in excluded:
                    skipped.append(str(path.relative_to(directory)))
                    continue
                if path.suffix != ".json":
                    raise ValueError("授权目录包含未识别的文件，需先核验")
                content = _read(path)
                grant = _OBJECT.validate_json(content, strict=True)
                if set(grant) != {"project_id", "workspace_scope_id", "permissions"}:
                    raise ValueError("工作区授权不是待转换形态")
                project, scope = grant["project_id"], grant["workspace_scope_id"]
                if (
                    not isinstance(project, str)
                    or str(UUID(project)) != project
                    or not isinstance(scope, str)
                ):
                    raise ValueError("工作区授权身份无效")
                if hashlib.sha256(scope.encode()).hexdigest() + ".json" != path.name:
                    raise ValueError("工作区授权与文件身份不一致")
                previous = _permissions(grant["permissions"])
                grant.update(permissions=previous.model_dump(mode="json"), revoked=True)
                changes.append((path, content, _encoded(grant)))
        changes.append((settings, original, _encoded(current)))
        backup.mkdir(mode=0o700)
        for path, content, _converted in changes:
            target = backup / path.relative_to(directory)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        (backup / "manifest.json").write_bytes(
            _encoded(
                {
                    "paths": [
                        str(path.relative_to(directory)) for path, _, _ in changes
                    ],
                    "excluded": skipped,
                }
            )
        )
        os.chmod(backup / "manifest.json", 0o600)
        written: list[tuple[Path, bytes]] = []
        try:
            for path, content, converted in changes:
                _replace(path, converted)
                written.append((path, content))
        except BaseException:
            for path, content in reversed(written):
                _replace(path, content)
            raise
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application_directory", type=Path)
    parser.add_argument("--device", action="append", required=True)
    parser.add_argument("--exclude-scope", action="append", default=[])
    args = parser.parse_args()
    try:
        backup = convert(
            args.application_directory,
            devices=args.device,
            exclude_scopes=args.exclude_scope,
        )
    except (ValueError, TypeError, OSError):
        parser.exit(2, "转换未完成；请保留备份并核验状态\n")
    print(backup)


if __name__ == "__main__":
    main()
