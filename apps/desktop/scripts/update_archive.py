"""验证完整应用文件树，并在独占目录中解压已核验的桌面发行包"""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import shutil
import stat
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE = 2 * 1024**3
MAX_EXPANDED = 8 * 1024**3
MAX_FILE = 1024**3
MAX_ENTRIES = 250_000


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_name(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or len(value.encode()) > 4096
        or any(character in value for character in ("\0", "\n", "\r", "\\"))
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("安装包路径无效")
    return path


def tree_digest(root: Path) -> str:
    """摘要绑定NFC路径、类型、权限和内容；链接只能指向同一应用的现有文件"""
    root = root.resolve(strict=True)
    entries = sorted(
        root.rglob("*"),
        key=lambda item: unicodedata.normalize(
            "NFC", item.relative_to(root).as_posix()
        ).encode(),
    )
    if len(entries) > MAX_ENTRIES:
        raise ValueError("安装包文件数量超过容量")
    digest = hashlib.sha256()
    names: set[str] = set()
    total = 0
    for path in entries:
        name = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        checked_name(name)
        folded = unicodedata.normalize("NFC", name).casefold()
        if folded in names:
            raise ValueError("安装包路径在目标文件系统重叠")
        names.add(folded)
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if mode & ~0o777:
            raise ValueError("安装包含特殊权限")
        if path.is_symlink():
            content = unicodedata.normalize("NFC", os.readlink(path))
            if (
                os.path.isabs(content)
                or any(character in content for character in ("\0", "\n", "\r", "\\"))
                or not path.resolve(strict=True).is_relative_to(root)
            ):
                raise ValueError("安装包含外部或失效链接")
            kind, size = "l", len(content.encode())
        elif stat.S_ISDIR(info.st_mode):
            kind, size, content = "d", 0, ""
        elif stat.S_ISREG(info.st_mode):
            kind, size, content = "f", info.st_size, file_digest(path)
            total += size
            if size > MAX_FILE or total > MAX_EXPANDED:
                raise ValueError("安装包文件超过容量")
        else:
            raise ValueError("安装包含特殊文件")
        digest.update(f"{kind}\0{name}\0{mode}\0{size}\0{content}\n".encode())
    return digest.hexdigest()


def create_archive(app: Path, destination: Path) -> None:
    """以唯一应用根目录保存权限与内部链接，不携带构建机扩展属性"""
    tree_digest(app)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in [app, *sorted(app.rglob("*"))]:
            name = "TinkerFin.app"
            if path != app:
                name += "/" + path.relative_to(app).as_posix()
            details = path.lstat()
            if stat.S_ISDIR(details.st_mode):
                name += "/"
            item = zipfile.ZipInfo(name)
            item.create_system = 3
            item.external_attr = details.st_mode << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            if stat.S_ISDIR(details.st_mode):
                item.external_attr |= 0x10
                archive.writestr(item, b"")
            elif stat.S_ISLNK(details.st_mode):
                archive.writestr(item, os.readlink(path).encode())
            else:
                with (
                    path.open("rb") as source,
                    archive.open(item, "w", force_zip64=True) as output,
                ):
                    shutil.copyfileobj(source, output, length=1024 * 1024)


def extract_verified(
    archive: Path, destination: Path, *, size: int, sha256: str, tree: str, release: str
) -> Path:
    """拒绝路径穿越、重叠文件和不受限展开；失败仅清理本次创建的目录"""
    if (
        not 0 < size <= MAX_ARCHIVE
        or archive.is_symlink()
        or archive.stat().st_size != size
        or file_digest(archive) != sha256
        or destination.exists()
        or destination.is_symlink()
    ):
        raise ValueError("安装包摘要、大小或暂存目录无效")
    destination.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if (
                not entries
                or len(entries) > MAX_ENTRIES
                or sum(item.file_size for item in entries) > MAX_EXPANDED
            ):
                raise ValueError("安装包超过容量")
            names: dict[str, tuple[zipfile.ZipInfo, int]] = {}
            folded: set[str] = set()
            for item in entries:
                name = item.filename.removesuffix("/")
                path = checked_name(name)
                mode = item.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (
                    path.parts[0] != "TinkerFin.app"
                    or item.file_size > MAX_FILE
                    or item.flag_bits & 1
                    or kind not in {stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK}
                    or stat.S_IMODE(mode) & ~0o777
                    or (item.is_dir() != (kind == stat.S_IFDIR))
                    or (len(path.parts) == 1 and kind != stat.S_IFDIR)
                ):
                    raise ValueError("安装包条目无效")
                normalized = unicodedata.normalize("NFC", name).casefold()
                if normalized in folded:
                    raise ValueError("安装包含重复路径")
                folded.add(normalized)
                names[name] = (item, mode)
            for name in names:
                for parent in PurePosixPath(name).parents:
                    if str(parent) != "." and (
                        str(parent) not in names
                        or not stat.S_ISDIR(names[str(parent)][1])
                    ):
                        raise ValueError("安装包缺少目录或经过链接写入")
            directories = sorted(
                (name for name, (_, mode) in names.items() if stat.S_ISDIR(mode)),
                key=lambda value: value.count("/"),
            )
            for name in directories:
                (destination / name).mkdir(mode=0o700)
            for name, (item, mode) in names.items():
                target = destination / name
                if stat.S_ISREG(mode):
                    with package.open(item) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    if target.stat().st_size != item.file_size:
                        raise ValueError("安装包文件不完整")
                    target.chmod(stat.S_IMODE(mode))
                elif stat.S_ISLNK(mode):
                    if item.file_size > 4096:
                        raise ValueError("安装包链接超过容量")
                    value = package.read(item).decode("utf-8")
                    if os.path.isabs(value) or any(
                        char in value for char in ("\0", "\n", "\r", "\\")
                    ):
                        raise ValueError("安装包链接无效")
                    target.symlink_to(value)
                    if stat.S_IMODE(target.lstat().st_mode) != stat.S_IMODE(mode):
                        target.chmod(stat.S_IMODE(mode), follow_symlinks=False)
            for name in reversed(directories):
                (destination / name).chmod(stat.S_IMODE(names[name][1]))
        app = destination / "TinkerFin.app"
        if tree_digest(app) != tree:
            raise ValueError("安装包文件树与签名不符")
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if (
            info.get("CFBundleIdentifier") != "com.tinkerfin.companion"
            or info.get("CFBundleShortVersionString") != release
            or info.get("CFBundleExecutable") != "TinkerFin"
            or not os.access(app / "Contents/MacOS/TinkerFin", os.X_OK)
        ):
            raise ValueError("安装包应用身份不符")
        return app
    except BaseException:
        shutil.rmtree(destination)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    extract_verified(
        args.archive,
        args.destination,
        size=args.size,
        sha256=args.sha256,
        tree=args.tree,
        release=args.release,
    )


if __name__ == "__main__":
    main()
