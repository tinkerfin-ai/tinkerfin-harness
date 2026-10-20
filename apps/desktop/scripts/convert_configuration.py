"""客户端退出后一次性将固定部署地址收敛为可编辑的连接配置"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import tempfile
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import JsonValue, TypeAdapter

from tinkerfin_desktop.configuration import WorkspacePermissions, server_url

_OBJECT = TypeAdapter(dict[str, JsonValue])


def _address(value: str) -> str:
    value = value.strip()
    if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("服务地址格式无效")
    url = httpx.URL(server_url(value))
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("服务地址端口无效")
    return str(url).rstrip("/")


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


def _encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _replace(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".connection-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def convert(directory: Path, *, studio_url: str) -> Path:
    """保留设备身份和权限，仅转换指定配置目录的服务地址

    Args:
        directory: 已退出客户端的配置根目录。
        studio_url: 该安装此前使用的 Studio 网页地址。

    Returns:
        包含原 settings.json 的备份目录。

    Raises:
        ValueError: 地址或配置不符合待转换形态，或备份已存在。
        OSError: 客户端仍在使用目录，或文件归属不符。
    """
    _directory(directory)
    directory = directory.resolve(strict=True)
    studio = _address(studio_url)
    backup = directory / "connection-backup"
    if backup.exists() or backup.is_symlink():
        raise ValueError("转换备份已存在，必须先核验上次操作")
    with ExitStack() as resources:
        locks = [directory / "application.lock"]
        state = directory / "state"
        if state.exists() or state.is_symlink():
            _directory(state)
            locks.append(state / "host.lock")
        for lock in locks:
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
            raise ValueError("配置不是待转换的固定地址形态")
        WorkspacePermissions.model_validate(saved["permissions"])
        server = saved.pop("server")
        if not isinstance(server, str):
            raise TypeError("服务地址必须是字符串")
        if saved.get("device") is not None and not server:
            raise ValueError("设备缺少服务归属")
        if server:
            saved["connection"] = {"studio_url": studio, "api_url": _address(server)}
        pending = saved.get("pending")
        if pending is not None:
            if not isinstance(pending, dict) or set(pending) - {
                "account",
                "server",
                "authorization",
            }:
                raise ValueError("待授权记录形态无效")
            pending_server = pending.get("server")
            account = pending.get("account")
            if (
                not isinstance(pending_server, str)
                or not isinstance(account, str)
                or str(UUID(account)) != account
            ):
                raise ValueError("待授权记录身份无效")
            if server and _address(pending_server) != _address(server):
                raise ValueError("待授权目标与当前服务不同，请先取消待授权申请")
            pending.pop("server")
            pending["connection"] = {
                "studio_url": studio,
                "api_url": _address(pending_server),
            }
        converted = _encoded(saved)
        backup.mkdir(mode=0o700)
        with (backup / "settings.json").open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(settings, converted)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application_directory", type=Path)
    parser.add_argument("--studio-url", required=True)
    args = parser.parse_args()
    try:
        backup = convert(
            args.application_directory,
            studio_url=args.studio_url,
        )
    except (ValueError, TypeError, OSError):
        parser.exit(2, "转换未完成；请保留备份并核验状态\n")
    print(backup)


if __name__ == "__main__":
    main()
