"""从锁定依赖构建自包含 macOS 应用；只写入本轮临时目录与新的输出路径"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import zstandard
from build_browser_cli import (
    COMMIT,
    RUST_TOOLCHAIN,
    SOURCE_SHA256,
    SOURCE_URL,
    TARGET,
    VERSION,
)
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[3]
COMPANION = ROOT / "apps/desktop"
PYTHON_RELEASE = "3.11.14"
PYTHON_SHA256 = "da32f9b54c581c203d96f55d802aca5adce2d1e72da532b7b1c83d40a4cd89aa"
NODE_RELEASE = "22.23.3"
NODE_SHA256 = "23b25245dcfb9af7262f8ff142e9e2e0af025368117329e7a7458a51e5922f53"
MACHO = {
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
}


class RustDependency(BaseModel):
    """浏览器构建随附的静态依赖许可证清单"""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    license: str = Field(min_length=1)
    source: str | None
    files: list[str] = Field(min_length=1)


class BrowserBuild(BaseModel):
    """只接收固定源码、补丁与工具链生成的完整浏览器构建输入"""

    model_config = ConfigDict(extra="forbid", strict=True)

    upstream_version: str
    upstream_commit: str
    source_url: str
    source_sha256: str
    patch_sha256: str
    rust_toolchain: str
    target: str
    binary_sha256: str
    features: list[str]
    assets: dict[str, str]
    rust_dependencies: list[RustDependency] = Field(min_length=1)


def run(
    *arguments: str | Path, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> str:
    """构建命令串行执行，单次最多十分钟；失败时保留命令的诊断输出"""
    result = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        timeout=600,
    )
    return result.stdout.strip()


def digest(path: Path) -> str:
    """按文件流计算构建输入与产物摘要"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_python(source: Path, target: Path) -> None:
    """复制发行包解释器，清空预装包后只安装锁定依赖"""
    executable = source / "bin/python3"
    probe = json.loads(
        run(
            executable,
            "-I",
            "-B",
            "-c",
            "import json,sys,platform;print(json.dumps([platform.python_version(),sys.prefix,sys.base_prefix,platform.machine()]))",
        )
    )
    if probe[0] != PYTHON_RELEASE or probe[1] != probe[2] or probe[3] != "arm64":
        raise ValueError("需要 Apple Silicon 独立 Python 3.11.14，不能使用 venv")

    def ignored(directory: str, names: list[str]) -> set[str]:
        if Path(directory).name == "site-packages":
            return set(names)
        return {
            name
            for name in names
            if name == "__pycache__" or name.endswith((".pyc", ".pyo"))
        }

    shutil.copytree(source, target, symlinks=True, ignore=ignored)
    # 仅在应用构建副本中开放依赖安装；完成后恢复只读环境标记
    (target / "lib/python3.11/EXTERNALLY-MANAGED").unlink(missing_ok=True)
    run(target / "bin/python3", "-I", "-B", "-m", "ensurepip")


def python_runtime(archive: Path, resources: Path, temporary: Path) -> None:
    """从固定完整发行包提取解释器与静态链接依赖的许可证，不复制宿主安装"""
    if digest(archive) != PYTHON_SHA256:
        raise ValueError("Python 完整发行包摘要与固定官方发行包不符")
    unpacked = temporary / "python-download"
    unpacked.mkdir()
    with (
        archive.open("rb") as source,
        zstandard.ZstdDecompressor().stream_reader(source) as stream,
    ):
        with tarfile.open(fileobj=stream, mode="r|") as content:
            content.extractall(unpacked, filter="data")
    distribution = unpacked / "python"
    metadata = json.loads((distribution / "PYTHON.json").read_text())
    if (
        metadata["python_version"] != PYTHON_RELEASE
        or metadata["target_triple"] != "aarch64-apple-darwin"
    ):
        raise ValueError("Python 发行包的平台与版本不符")
    copy_python(distribution / "install", resources / "python")
    licenses = resources / "licenses"
    licenses.mkdir()
    shutil.copytree(distribution / "licenses", licenses / "python-runtime")
    shutil.copy2(distribution / "PYTHON.json", licenses / "python-distribution.json")


def node_runtime(archive: Path, target: Path, temporary: Path) -> None:
    """使用官方固定发行包及其 SHA-256，拒绝替换的 Node 输入"""
    if digest(archive) != NODE_SHA256:
        raise ValueError("Node 发行包摘要与固定官方发行包不符")
    unpacked = temporary / "node-download"
    unpacked.mkdir()
    with tarfile.open(archive) as content:
        content.extractall(unpacked, filter="data")
    shutil.move(unpacked / f"node-v{NODE_RELEASE}-darwin-arm64", target)
    if run(target / "bin/node", "--version") != f"v{NODE_RELEASE}":
        raise ValueError("Node 运行时版本不符")


def install_python(resources: Path, temporary: Path) -> None:
    """从 uv.lock 安装第三方包，并复用仓库的源码一致性 wheel 构建入口"""
    python = resources / "python/bin/python3"
    requirements = temporary / "requirements.txt"
    run(
        "uv",
        "export",
        "--locked",
        "--package",
        "tinkerfin-companion",
        "--no-dev",
        "--no-emit-workspace",
        "-o",
        requirements,
    )
    run(
        "uv",
        "pip",
        "install",
        "--python",
        python,
        "--no-deps",
        "--require-hashes",
        "-r",
        requirements,
    )
    wheels = temporary / "wheels"
    run(
        sys.executable,
        ROOT / "scripts/build_wheels.py",
        "--python",
        python,
        "--out-dir",
        wheels,
        "--package",
        "tinkerfin-companion",
    )
    run(
        "uv",
        "pip",
        "install",
        "--python",
        python,
        "--no-deps",
        *sorted(wheels.glob("*.whl")),
    )
    run("uv", "pip", "check", "--python", python)
    # 工具直接执行该入口，不能依赖 wheel 安装保留 Python 模块的执行位
    (
        resources
        / "python/lib/python3.11/site-packages/tinkerfin_companion/browser_command.py"
    ).chmod(0o755)
    shutil.copy2(requirements, resources / "python-requirements.txt")
    (resources / "python/lib/python3.11/EXTERNALLY-MANAGED").write_text(
        "[externally-managed]\nError=This Python belongs to TinkerFin. Create a virtual environment in your working directory with python -m venv .venv\n"
    )


def install_node(resources: Path) -> None:
    """安装锁文件固定的原生沙箱运行时，供受限执行模式使用"""
    runtime = resources / "runtime"
    runtime.mkdir()
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(COMPANION / "runtime" / name, runtime / name)
    environment = {
        **os.environ,
        "PATH": str(resources / "node/bin") + ":/usr/bin:/bin:/usr/sbin:/sbin",
    }
    run(
        resources / "node/bin/node",
        resources / "node/lib/node_modules/npm/bin/npm-cli.js",
        "ci",
        "--omit=dev",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        cwd=runtime,
        env=environment,
    )


def install_browser(resources: Path, executable: Path) -> None:
    """校验托管构建与全部附属文件，拒绝裸程序、篡改或遗漏的构建输入"""
    metadata_file = executable.with_name(executable.name + ".json")
    metadata_bytes = metadata_file.read_bytes()
    metadata = BrowserBuild.model_validate_json(metadata_bytes)
    expected = {
        "upstream_version": VERSION,
        "upstream_commit": COMMIT,
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "patch_sha256": digest(
            COMPANION / "runtime/patches/agent-browser-managed.patch"
        ),
        "rust_toolchain": RUST_TOOLCHAIN,
        "target": TARGET,
        "binary_sha256": digest(executable),
        "features": ["managed-process"],
    }
    actual = metadata.model_dump()
    if any(actual[key] != value for key, value in expected.items()):
        raise ValueError("浏览器工具与固定托管构建不一致")
    with executable.open("rb") as stream:
        if stream.read(4) not in MACHO:
            raise ValueError("浏览器工具必须是 Apple Silicon 原生程序")
    if run("lipo", "-archs", executable) != "arm64":
        raise ValueError("浏览器工具的目标平台不符")
    assets = executable.parent / "agent-browser-assets"
    if assets.is_symlink() or not assets.is_dir():
        raise ValueError("浏览器工具缺少完整附属文件")
    files: dict[str, Path] = {}
    for path in assets.rglob("*"):
        if path.is_symlink():
            raise ValueError("浏览器工具附属文件不能含链接")
        if path.is_file():
            files[path.relative_to(assets).as_posix()] = path
        elif not path.is_dir():
            raise ValueError("浏览器工具附属文件类型无效")
    if files.keys() != metadata.assets.keys() or any(
        digest(path) != metadata.assets[relative] for relative, path in files.items()
    ):
        raise ValueError("浏览器工具附属文件与构建摘要不符")
    required = {
        "skills/agent-browser/SKILL.md",
        "skill-data/core/SKILL.md",
        "licenses/agent-browser/LICENSE",
        "licenses/agent-browser/LICENSE-axe-core.txt",
        "licenses/agent-browser/LICENSE-axe-core-THIRD-PARTY.txt",
    }
    for dependency in metadata.rust_dependencies:
        if any(not path.startswith("licenses/") for path in dependency.files):
            raise ValueError("浏览器静态依赖缺少许可证路径")
        required.update(dependency.files)
    if not required <= files.keys():
        raise ValueError("浏览器工具缺少技能或许可证")
    target = resources / "runtime/agent-browser"
    shutil.copytree(assets, target)
    (target / "bin").mkdir()
    binary = target / "bin/agent-browser"
    shutil.copy2(executable, binary)
    (target / "build.json").write_bytes(metadata_bytes)
    with tempfile.TemporaryDirectory(prefix="tinkerfin-browser-check-") as raw:
        temporary = Path(raw)
        configuration = temporary / "config.json"
        configuration.write_text("{}\n")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AGENT_BROWSER_")
        }
        environment["AGENT_BROWSER_CONFIG"] = str(configuration)
        if run(binary, "--version", cwd=temporary, env=environment) != (
            f"agent-browser {VERSION}"
        ):
            raise ValueError("浏览器工具版本不符")
        if not run(binary, "skills", "get", "core", cwd=temporary, env=environment):
            raise ValueError("浏览器工具无法读取随附技能")


def license_inventory(resources: Path) -> None:
    """随包保留第三方许可证原文，并生成便于查找的依赖清单"""
    licenses = resources / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", licenses / "TinkerFin-LICENSE.txt")
    inventory = run(
        resources / "python/bin/python3",
        "-I",
        "-B",
        "-c",
        """
import importlib.metadata,json
items=[]
for package in importlib.metadata.distributions():
    paths=['python/lib/python3.11/site-packages/'+str(item) for item in package.files or [] if item.name.lower().startswith(('license','licence','copying','notice'))]
    items.append({'name':package.metadata['Name'],'version':package.version,'license':package.metadata.get('License-Expression') or package.metadata.get('License'),'files':paths})
print(json.dumps(sorted(items,key=lambda item:item['name'].lower()),ensure_ascii=False,indent=2))
""",
    )
    packages = json.loads(inventory)
    supplemental = COMPANION / "runtime/licenses"
    manifest = json.loads((supplemental / "manifest.json").read_text())
    for item in manifest:
        package = next(
            package for package in packages if package["name"] == item["distribution"]
        )
        if (
            package["version"] != item["release"]
            or digest(supplemental / item["file"]) != item["sha256"]
        ):
            raise ValueError(f"补充许可证与当前依赖不一致：{item['distribution']}")
        package["files"].append("licenses/supplemental/" + item["file"])
    shutil.copytree(supplemental, licenses / "supplemental")
    for package in packages:
        if not package["files"] or any(
            not (resources / name).is_file() for name in package["files"]
        ):
            raise ValueError(f"随包依赖缺少许可证文件：{package['name']}")
    (licenses / "python-packages.json").write_text(
        json.dumps(packages, ensure_ascii=False, indent=2) + "\n"
    )
    node_packages = []
    for path in (resources / "runtime/node_modules").rglob("package.json"):
        package = json.loads(path.read_text())
        if not package.get("name") or not package.get("version"):
            continue
        node_packages.append(
            {
                "name": package["name"],
                "version": package["version"],
                "license": package.get("license"),
                "directory": str(path.parent.relative_to(resources)),
            }
        )
    (licenses / "node-packages.json").write_text(
        json.dumps(sorted(node_packages, key=lambda item: item["name"]), indent=2)
        + "\n"
    )
    browser_root = resources / "runtime/agent-browser"
    browser = BrowserBuild.model_validate_json(
        (browser_root / "build.json").read_bytes()
    )
    (licenses / "native-programs.json").write_text(
        json.dumps(
            [
                {
                    "name": "agent-browser",
                    "version": browser.upstream_version,
                    "license": "Apache-2.0",
                    "directory": "runtime/agent-browser",
                    "build_manifest": "runtime/agent-browser/build.json",
                    "files": [
                        "runtime/agent-browser/" + path
                        for path in sorted(browser.assets)
                        if path.startswith("licenses/")
                    ],
                }
            ],
            indent=2,
        )
        + "\n"
    )


def relocate_entrypoints(resources: Path) -> None:
    """使安装时生成的 Python 命令随应用移动，不保留构建目录解释器路径"""
    for entry in (resources / "python/bin").iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        content = entry.read_bytes()
        if content[:4] in MACHO:
            continue
        text = content.decode()
        lines = text.splitlines(keepends=True)
        if (
            text.startswith("#!/bin/sh\n'''exec'")
            and len(lines) >= 3
            and lines[2].strip() == "' '''"
        ):
            body = "".join(lines[3:])
        elif lines and lines[0].startswith("#!") and "python" in lines[0]:
            body = "".join(lines[1:])
        else:
            raise ValueError(f"无法确认随包 Python 命令格式：{entry.name}")
        entry.write_text(
            "#!/bin/sh\n"
            """\'\'\'exec\' "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/python3" -B "$0" "$@"\n"""
            "' '''\n" + body
        )
    for cache in resources.rglob("__pycache__"):
        shutil.rmtree(cache)


def sign(app: Path, identity: str, temporary: Path) -> None:
    """从嵌套二进制到应用逐层签名，任务解释器保留 JIT 和已授权依赖加载能力"""
    entitlements = temporary / "runtime-entitlements.plist"
    entitlements.write_bytes(
        plistlib.dumps(
            {
                "com.apple.security.cs.allow-jit": True,
                "com.apple.security.cs.allow-unsigned-executable-memory": True,
                "com.apple.security.cs.disable-library-validation": True,
            }
        )
    )
    for path in sorted(app.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        bundle = path.is_dir() and path.suffix in {".app", ".framework", ".xpc"}
        if not bundle:
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                if stream.read(4) not in MACHO:
                    continue
        args = ["codesign", "--force", "--sign", identity, "--options", "runtime"]
        if path.name in {"python3.11", "node"}:
            args += ["--entitlements", str(entitlements)]
        if identity != "-":
            args.append("--timestamp")
        run(*args, path)
    run(
        "codesign",
        "--force",
        "--sign",
        identity,
        "--options",
        "runtime",
        *(["--timestamp"] if identity != "-" else []),
        app,
    )
    run("codesign", "--verify", "--deep", "--strict", app)


def install_interface(resources: Path, configuration: Path) -> None:
    """签名前安装界面图标、语言资源和开发者选择的连接目的地"""
    content = configuration.read_bytes()
    if len(content) > 4096:
        raise ValueError("连接配置超过容量")
    raw = json.loads(content)
    if not isinstance(raw, dict) or set(raw) != {"studio_url", "api_url"}:
        raise ValueError("连接配置必须只包含 studio_url 和 api_url")
    checked: dict[str, str] = {}
    for key in ("studio_url", "api_url"):
        value = raw[key]
        if not isinstance(value, str):
            raise TypeError("连接地址必须是字符串")
        url = urlsplit(value)
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (
                url.scheme != "https"
                and not (
                    url.scheme == "http"
                    and url.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
        ):
            raise ValueError("连接地址必须是 HTTPS，HTTP 仅用于 loopback 开发服务")
        checked[key] = value.rstrip("/")
    shutil.copy2(
        ROOT / "apps/studio/web/public/brand/tinkerfin.svg", resources / "brand.svg"
    )
    shutil.copytree(COMPANION / "resources/icons", resources / "icons")
    for language in ("en", "zh-Hans"):
        shutil.copytree(
            COMPANION / f"resources/{language}.lproj", resources / f"{language}.lproj"
        )
    run(
        "xcrun",
        "swift",
        "-swift-version",
        "6",
        COMPANION / "scripts/render_app_icon.swift",
        resources / "brand.svg",
        resources / "TinkerFin.icns",
    )
    (resources / "connection.json").write_text(
        json.dumps(checked, ensure_ascii=False, indent=2) + "\n"
    )


def build(
    *,
    output: Path,
    python_archive: Path,
    node_archive: Path,
    browser_cli: Path,
    identity: str,
    configuration: Path = COMPANION / "resources/connection.json",
) -> Path:
    """完成全部检查后才发布新的 .app 和 ZIP，不覆盖既有构建或用户安装"""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("构建需要 Apple Silicon Mac 与 macOS 15 SDK")
    output = output.resolve()
    if (
        output.exists()
        or output.with_suffix(".zip").exists()
        or output.suffix != ".app"
    ):
        raise ValueError("输出必须是不存在的 .app 路径，且同名 ZIP 尚不存在")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tinkerfin-app-", dir=output.parent) as raw:
        temporary = Path(raw)
        app = temporary / "TinkerFin.app"
        resources = app / "Contents/Resources"
        executable = app / "Contents/MacOS/TinkerFin"
        executable.parent.mkdir(parents=True)
        resources.mkdir()
        python_runtime(python_archive, resources, temporary)
        node_runtime(node_archive, resources / "node", temporary)
        install_python(resources, temporary)
        install_node(resources)
        install_browser(resources, browser_cli.resolve(strict=True))
        install_interface(resources, configuration)
        license_inventory(resources)
        relocate_entrypoints(resources)
        release = tomllib.loads((COMPANION / "pyproject.toml").read_text())["project"][
            "version"
        ]
        (app / "Contents/Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "com.tinkerfin.companion",
                    "CFBundleName": "TinkerFin",
                    "CFBundleDisplayName": "TinkerFin",
                    "CFBundleDevelopmentRegion": "en",
                    "CFBundleLocalizations": ["en", "zh-Hans"],
                    "CFBundleExecutable": executable.name,
                    "CFBundleIconFile": "TinkerFin.icns",
                    "CFBundleURLTypes": [
                        {
                            "CFBundleURLName": "com.tinkerfin.companion.authorization",
                            "CFBundleURLSchemes": ["tinkerfin"],
                        }
                    ],
                    "CFBundlePackageType": "APPL",
                    "CFBundleShortVersionString": release,
                    "CFBundleVersion": release,
                    "LSMinimumSystemVersion": "15.0",
                    "LSUIElement": True,
                    "NSHighResolutionCapable": True,
                }
            )
        )
        run(
            "swiftc",
            "-swift-version",
            "6",
            "-O",
            "-target",
            "arm64-apple-macosx15.0",
            *sorted((COMPANION / "native").glob("*.swift")),
            "-o",
            executable,
        )
        # 所有链接必须位于应用中，防止把构建机目录当成安装依赖
        for path in app.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(app):
                raise ValueError(f"应用含外部链接：{path.relative_to(app)}")
        sign(app, identity, temporary)
        run(
            resources / "python/bin/python3",
            "-I",
            "-B",
            "-m",
            "tinkerfin_companion",
            "--resources",
            resources,
            "--check",
        )
        app.rename(output)
    run(
        output / "Contents/Resources/python/bin/python3",
        "-I",
        "-B",
        "-m",
        "tinkerfin_companion",
        "--resources",
        output / "Contents/Resources",
        "--check",
    )
    run("codesign", "--verify", "--deep", "--strict", output)
    run(output / "Contents/Resources/python/bin/pip3", "--version")
    archive = output.with_suffix(".zip")
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", output, archive)
    archive.with_suffix(".zip.sha256").write_text(
        f"{digest(archive)}  {archive.name}\n"
    )
    return output


def main() -> None:
    """显式选择固定分发资源；签名身份默认仅用于本机开发验收"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python-archive", required=True, type=Path)
    parser.add_argument("--node-archive", required=True, type=Path)
    parser.add_argument("--browser-cli", required=True, type=Path)
    parser.add_argument("--identity", default="-")
    parser.add_argument(
        "--configuration", type=Path, default=COMPANION / "resources/connection.json"
    )
    args = parser.parse_args()
    print(
        build(
            output=args.output,
            python_archive=args.python_archive,
            node_archive=args.node_archive,
            browser_cli=args.browser_cli,
            identity=args.identity,
            configuration=args.configuration,
        )
    )


if __name__ == "__main__":
    main()
