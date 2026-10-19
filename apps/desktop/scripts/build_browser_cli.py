"""从固定官方源码构建受客户端进程作用域管理的浏览器 CLI"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.request import urlopen

from pydantic import BaseModel, JsonValue

VERSION = "0.33.2"
COMMIT = "93cdda5709e8861c0c26b0b955d8d746e9fda0d7"
SOURCE_URL = f"https://codeload.github.com/vercel-labs/agent-browser/tar.gz/{COMMIT}"
SOURCE_SHA256 = "3a81b2980ac9f010257f6b7b61606480b5f22105d5dde33c3ebea95e8bacf46c"
RUST_TOOLCHAIN = "1.99.1"
TARGET = "aarch64-apple-darwin"
RUSTUP_URL = (
    "https://static.rust-lang.org/rustup/archive/1.29.1/"
    "aarch64-apple-darwin/rustup-init"
)
RUSTUP_SHA256 = "ec1b9233e7f72990ecd8e62063fa7f6c3dfc2bec8e97f88bff165f9100ac696a"
ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "apps/desktop/runtime/patches/agent-browser-managed.patch"
TOOLS = ROOT / ".agents/tools"


@dataclass(frozen=True, slots=True)
class LicenseSource:
    """发布 crate 未携带正文时，补充固定来源原文及其完整性摘要"""

    filename: str
    url: str
    sha256: str
    origin: Literal["upstream", "spdx"] = "upstream"


SPDX_MIT = LicenseSource(
    "LICENSE-SPDX-MIT.txt",
    "https://raw.githubusercontent.com/spdx/license-list-data/v3.27.0/text/MIT.txt",
    "b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5",
    "spdx",
)
PROFILING_LICENSES = (
    LicenseSource(
        "LICENSE-APACHE",
        "https://raw.githubusercontent.com/aclysma/profiling/"
        "740d74c82212f1f0584cfa5c6d884c5ab5367101/LICENSE-APACHE",
        "10d30a673cd5e9349bdc02aeb48f14b3386d27d0da32df8f0a555d4aa16aa551",
    ),
    LicenseSource(
        "LICENSE-MIT",
        "https://raw.githubusercontent.com/aclysma/profiling/"
        "740d74c82212f1f0584cfa5c6d884c5ab5367101/LICENSE-MIT",
        "c8167fdeeed46d3f244d3f85c5bf998ce889343691c32be2c61a8bc4b5c08333",
    ),
)
SUPPLEMENTAL_LICENSES: dict[tuple[str, str], tuple[LicenseSource, ...]] = {
    ("fax", "0.2.6"): (SPDX_MIT,),
    ("fax_derive", "0.2.0"): (SPDX_MIT,),
    ("simd_helpers", "0.1.0"): (SPDX_MIT,),
    ("profiling", "1.0.17"): PROFILING_LICENSES,
    ("profiling-procmacros", "1.0.17"): PROFILING_LICENSES,
}
for _name, _version, _commit, _notice_hash in (
    (
        "zune-core",
        "0.4.12",
        "f8fbb123d5ed04441e8324a555bfcda0cb1bd28f",
        "c6dff146a9f31848ac296faa5a08a4253caf2c384c86f906dc99e7fc0a39cc8c",
    ),
    (
        "zune-inflate",
        "0.2.54",
        "69502ce83fdfecdd0beefd677e2abb3781b29d98",
        "e870d7fd7819a9f0453c62798cb52ae95d148a8c91dc2de1d983f997143c72de",
    ),
    (
        "zune-jpeg",
        "0.4.21",
        "fa2c767a01d7d9373911d0bf63e0588553d67e0e",
        "c6dff146a9f31848ac296faa5a08a4253caf2c384c86f906dc99e7fc0a39cc8c",
    ),
):
    _source = f"https://raw.githubusercontent.com/etemesi254/zune-image/{_commit}"
    SUPPLEMENTAL_LICENSES[(_name, _version)] = (
        LicenseSource("LICENSE.md", f"{_source}/LICENSE.md", _notice_hash),
        LicenseSource(
            "LICENSE-ZLIB",
            f"{_source}/LICENSE-ZLIB",
            "7fa429541e55b1509909e058f2d21a37467e4958ec713b357f6e0cf9dc4ee352",
        ),
    )


class CargoPackage(BaseModel):
    """Cargo 依赖身份及随源码发布的许可证位置"""

    id: str
    name: str
    version: str
    license: str | None = None
    license_file: str | None = None
    source: str | None = None
    manifest_path: Path


class DependencyKind(BaseModel):
    """区分实际构建所需依赖与仅用于测试的依赖"""

    kind: Literal["dev", "build"] | None


class CargoDependency(BaseModel):
    """当前目标平台解析后的依赖边"""

    pkg: str
    dep_kinds: tuple[DependencyKind, ...]


class CargoNode(BaseModel):
    """当前特性组合中参与依赖解析的包"""

    id: str
    deps: tuple[CargoDependency, ...]


class CargoResolve(BaseModel):
    """构建根包与当前启用特性的完整依赖图"""

    root: str
    nodes: tuple[CargoNode, ...]


class CargoMetadata(BaseModel):
    """固定目标平台的 Cargo 元数据输入"""

    packages: tuple[CargoPackage, ...]
    resolve: CargoResolve


def digest(path: Path) -> str:
    """计算构建输入、原生程序和随附资源的 SHA-256"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(
    *arguments: str | Path, cwd: Path, environment: dict[str, str], capture: bool = True
) -> str:
    """串行构建命令最多运行二十分钟，中断时停止并等待自有进程组"""
    command = [str(argument) for argument in arguments]
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE if capture else None,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            output, _ = process.communicate(timeout=1200)
        except BaseException:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate()
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, output)
        return (output or "").strip()


def source_archive(cache: Path) -> Path:
    """只接受固定提交对应的官方源码压缩包，不复用摘要不符的缓存"""
    target = cache / f"source-{COMMIT}.tar.gz"
    if not target.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".source-", dir=cache)
        try:
            with os.fdopen(descriptor, "wb") as output:
                with urlopen(SOURCE_URL, timeout=60) as response:
                    shutil.copyfileobj(response, output)
            if digest(Path(temporary)) != SOURCE_SHA256:
                raise ValueError("浏览器 CLI 官方源码摘要不符")
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if target.is_symlink() or digest(target) != SOURCE_SHA256:
        raise ValueError("浏览器 CLI 源码缓存无效")
    return target


def prepare_toolchain(cargo: Path, environment: dict[str, str]) -> None:
    """缺少私有编译器时安装固定 minimal 工具链，不改全局 Rust 或 shell 配置"""
    if cargo != TOOLS / "rust/cargo/bin/cargo":
        return
    directory = TOOLS / "rust"
    compiler = directory / f"rustup/toolchains/{RUST_TOOLCHAIN}-{TARGET}/bin/rustc"
    if compiler.is_file():
        return
    directory.mkdir(parents=True, exist_ok=True)
    installer = directory / "rustup-init"
    if not installer.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".rustup-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as output:
                with urlopen(RUSTUP_URL, timeout=60) as response:
                    shutil.copyfileobj(response, output)
            if digest(Path(temporary)) != RUSTUP_SHA256:
                raise ValueError("Rust 安装程序与固定官方摘要不符")
            os.chmod(temporary, 0o700)
            os.replace(temporary, installer)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if installer.is_symlink() or digest(installer) != RUSTUP_SHA256:
        raise ValueError("Rust 安装程序缓存无效")
    run(
        installer,
        "-y",
        "--no-modify-path",
        "--profile",
        "minimal",
        "--default-toolchain",
        RUST_TOOLCHAIN,
        cwd=ROOT,
        environment=environment,
        capture=False,
    )


def runtime_packages(metadata: CargoMetadata) -> list[CargoPackage]:
    """按 Cargo 解析图收集编译与链接依赖，排除仅供测试的边"""
    nodes = {node.id: node for node in metadata.resolve.nodes}
    selected: set[str] = set()
    pending = [metadata.resolve.root]
    while pending:
        identifier = pending.pop()
        if identifier in selected:
            continue
        selected.add(identifier)
        pending.extend(
            dependency.pkg
            for dependency in nodes[identifier].deps
            if any(kind.kind != "dev" for kind in dependency.dep_kinds)
        )
    return sorted(
        (
            package
            for package in metadata.packages
            if package.id in selected and package.id != metadata.resolve.root
        ),
        key=lambda package: (package.name, package.version),
    )


def license_text(source: LicenseSource, cache: Path) -> Path:
    """下载固定许可证原文，并拒绝摘要不符的下载或缓存"""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / source.sha256
    if not target.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".license-", dir=cache)
        try:
            with os.fdopen(descriptor, "wb") as output:
                with urlopen(source.url, timeout=60) as response:
                    shutil.copyfileobj(response, output)
            if digest(Path(temporary)) != source.sha256:
                raise ValueError("Rust 补充许可证与固定原文摘要不符")
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if target.is_symlink() or digest(target) != source.sha256:
        raise ValueError("Rust 补充许可证缓存无效")
    return target


def copy_licenses(
    metadata: CargoMetadata, assets: Path, cache: Path
) -> list[dict[str, JsonValue]]:
    """保留实际 Rust 依赖的许可证原文；缺失时拒绝交付不完整产物"""
    inventory: list[dict[str, JsonValue]] = []
    for package in runtime_packages(metadata):
        directory = package.manifest_path.parent.resolve(strict=True)
        files = {
            path
            for path in directory.rglob("*")
            if path.is_file()
            and path.name.upper().startswith(
                ("LICENSE", "LICENCE", "COPYING", "NOTICE")
            )
        }
        if package.license_file is not None:
            files.add(directory / package.license_file)
        supplements = SUPPLEMENTAL_LICENSES.get((package.name, package.version), ())
        if (not files and not supplements) or not package.license:
            raise ValueError(f"Rust 依赖缺少许可证：{package.name} {package.version}")
        if supplements:
            files.add(package.manifest_path)
            files.add(directory / ".cargo_vcs_info.json")
        copied: list[JsonValue] = []
        for path in sorted(files):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Rust 许可证路径无效：{package.name}")
            relative = path.resolve(strict=True).relative_to(directory)
            target = (
                assets
                / "licenses/rust"
                / f"{package.name}-{package.version}"
                / relative
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            copied.append(target.relative_to(assets).as_posix())
        sources: list[dict[str, str]] = []
        for supplement in supplements:
            target = (
                assets
                / "licenses/rust"
                / f"{package.name}-{package.version}"
                / supplement.filename
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(license_text(supplement, cache), target)
            copied.append(target.relative_to(assets).as_posix())
            sources.append(
                {
                    "file": supplement.filename,
                    "url": supplement.url,
                    "sha256": supplement.sha256,
                    "origin": supplement.origin,
                    "declaration": "Cargo.toml",
                }
            )
        if sources:
            provenance = (
                assets
                / "licenses/rust"
                / f"{package.name}-{package.version}"
                / "sources.json"
            )
            provenance.write_text(json.dumps(sources, indent=2) + "\n")
            copied.append(provenance.relative_to(assets).as_posix())
        inventory.append(
            {
                "name": package.name,
                "version": package.version,
                "license": package.license,
                "source": package.source,
                "files": copied,
            }
        )
    return inventory


def asset_hashes(directory: Path) -> dict[str, str]:
    """资源清单只包含普通文件，不接受指向构建目录外的链接"""
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("浏览器运行时资源不能包含符号链接")
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = digest(path)
    return result


def build(output: Path, cargo: Path, cache: Path) -> Path:
    """生成原生程序、来源清单及技能和许可证资源，不修改宿主工具安装

    Args:
        output: 受管原生程序的目标文件。
        cargo: 任务私有工具链的 Cargo 入口。
        cache: 本次构建拥有的源码下载与编译缓存目录。

    Returns:
        已生成原生程序的绝对路径。
    """
    output = output.absolute()
    cache.mkdir(parents=True, exist_ok=True)
    cache = cache.resolve(strict=True)
    cargo = cargo.absolute()
    environment = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AGENT_BROWSER_")
        },
        "CARGO_HOME": str(TOOLS / "rust/cargo"),
        "RUSTUP_HOME": str(TOOLS / "rust/rustup"),
        "RUSTUP_TOOLCHAIN": RUST_TOOLCHAIN,
        "CARGO_TARGET_DIR": str(cache / "target"),
        "PATH": str(cargo.parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    prepare_toolchain(cargo, environment)
    compiler = run(
        cargo.parent / "rustc", "--version", cwd=ROOT, environment=environment
    )
    if compiler.split()[1] != RUST_TOOLCHAIN:
        raise ValueError("Rust 工具链与固定构建版本不符")
    archive = source_archive(cache)
    with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as temporary:
        unpacked = Path(temporary)
        with tarfile.open(archive) as content:
            content.extractall(unpacked, filter="data")
        source = unpacked / f"agent-browser-{COMMIT}"
        staged_patch = unpacked / "managed.patch"
        shutil.copyfile(PATCH, staged_patch)
        patch_sha256 = digest(staged_patch)
        (source / "managed-build-config.json").write_text("{}\n")
        environment["AGENT_BROWSER_CONFIG"] = str(source / "managed-build-config.json")
        run("git", "init", "--quiet", cwd=source, environment=environment)
        run(
            "git", "apply", "--check", staged_patch, cwd=source, environment=environment
        )
        run("git", "apply", staged_patch, cwd=source, environment=environment)
        common = (
            "--manifest-path",
            source / "cli/Cargo.toml",
            "--locked",
            "--features",
            "managed-process",
        )
        run(
            cargo,
            "build",
            *common,
            "--release",
            "--target",
            TARGET,
            cwd=source,
            environment=environment,
            capture=False,
        )
        compiled = cache / "target" / TARGET / "release/agent-browser"
        if run(compiled, "--version", cwd=source, environment=environment) != (
            f"agent-browser {VERSION}"
        ):
            raise ValueError("编译产物的官方 CLI 版本不符")
        metadata = CargoMetadata.model_validate_json(
            run(
                cargo,
                "metadata",
                *common,
                "--format-version",
                "1",
                "--filter-platform",
                TARGET,
                cwd=source,
                environment=environment,
            )
        )
        assets = unpacked / "agent-browser-assets"
        shutil.copytree(
            source / "skills/agent-browser", assets / "skills/agent-browser"
        )
        shutil.copytree(source / "skill-data", assets / "skill-data")
        licenses = assets / "licenses/agent-browser"
        licenses.mkdir(parents=True)
        shutil.copyfile(source / "LICENSE", licenses / "LICENSE")
        for path in (source / "cli/src/native/a11y").glob("LICENSE-axe-core*.txt"):
            shutil.copyfile(path, licenses / path.name)
        dependencies = copy_licenses(metadata, assets, cache / "licenses")
        hashes = asset_hashes(assets)
        manifest = {
            "upstream_version": VERSION,
            "upstream_commit": COMMIT,
            "source_url": SOURCE_URL,
            "source_sha256": SOURCE_SHA256,
            "patch_sha256": patch_sha256,
            "rust_toolchain": RUST_TOOLCHAIN,
            "target": TARGET,
            "binary_sha256": digest(compiled),
            "features": ["managed-process"],
            "assets": hashes,
            "rust_dependencies": dependencies,
        }
        if digest(PATCH) != patch_sha256:
            raise ValueError("构建期间依赖补丁已变化，请重新构建当前产物")
        output.parent.mkdir(parents=True, exist_ok=True)
        saved_assets = output.parent / "agent-browser-assets"
        if saved_assets.exists():
            if saved_assets.is_symlink() or asset_hashes(saved_assets) != hashes:
                raise ValueError("已有浏览器资源不匹配，请使用新的输出目录")
        else:
            shutil.copytree(assets, saved_assets)
        descriptor, temporary_binary = tempfile.mkstemp(dir=output.parent)
        os.close(descriptor)
        try:
            shutil.copyfile(compiled, temporary_binary)
            os.chmod(temporary_binary, 0o755)
            os.replace(temporary_binary, output)
        finally:
            Path(temporary_binary).unlink(missing_ok=True)
        Path(str(output) + ".json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=TOOLS / "agent-browser-managed/agent-browser"
    )
    parser.add_argument("--cargo", type=Path, default=TOOLS / "rust/cargo/bin/cargo")
    parser.add_argument("--cache", type=Path, default=TOOLS / "agent-browser-managed")
    args = parser.parse_args()
    print(build(args.output, args.cargo, args.cache))


if __name__ == "__main__":
    main()
