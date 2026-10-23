"""从锁定依赖构建自包含 macOS 应用；只写入本轮临时目录与新的输出路径"""

from __future__ import annotations

import argparse
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

import zstandard
from runtime_inputs import DEFAULT_CACHE, NODE, PYTHON, digest, obtain
from update_archive import create_archive

ROOT = Path(__file__).resolve().parents[3]
DESKTOP = ROOT / "apps/desktop"
PYTHON_RELEASE = PYTHON.version
NODE_RELEASE = NODE.version
MACHO = {
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
}


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
    if digest(archive) != PYTHON.sha256:
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
    if digest(archive) != NODE.sha256:
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
        "tinkerfin-desktop",
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
        "tinkerfin-desktop",
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
        / "python/lib/python3.11/site-packages/tinkerfin_desktop/browser_command.py"
    ).chmod(0o755)
    shutil.copy2(requirements, resources / "python-requirements.txt")
    (resources / "python/lib/python3.11/EXTERNALLY-MANAGED").write_text(
        "[externally-managed]\nError=This Python belongs to TinkerFin. Create a virtual environment in your working directory with python -m venv .venv\n"
    )


def install_node(resources: Path) -> None:
    """安装锁文件固定的沙箱、官方浏览器依赖与应用适配入口"""
    runtime = resources / "runtime"
    runtime.mkdir()
    for name in ("package.json", "package-lock.json", "browser-service.mjs"):
        shutil.copy2(DESKTOP / "runtime" / name, runtime / name)
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
    supplemental = DESKTOP / "runtime/licenses"
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
    runtime = resources / "runtime"
    lock = json.loads((runtime / "package-lock.json").read_text())
    for relative in lock["packages"]:
        path = runtime / relative
        if not relative or not (path / "package.json").is_file():
            continue
        package = json.loads((path / "package.json").read_text())
        files = [
            str(item.relative_to(resources))
            for item in path.iterdir()
            if item.is_file()
            and item.name.lower().startswith(
                ("license", "licence", "copying", "notice")
            )
        ]
        if not files:
            raise ValueError(f"Node 依赖缺少许可证：{relative}")
        node_packages.append(
            {
                "name": package["name"],
                "version": package["version"],
                "license": package.get("license"),
                "files": sorted(files),
            }
        )
    (licenses / "node-packages.json").write_text(
        json.dumps(sorted(node_packages, key=lambda item: item["name"]), indent=2)
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


def sign(
    app: Path,
    identity: str,
    temporary: Path,
    *,
    keychain: Path | None = None,
    timestamp: bool = False,
) -> None:
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
    signing = ["codesign", "--force", "--sign", identity, "--options", "runtime"]
    if keychain is not None:
        signing += ["--keychain", str(keychain)]
    signing.append("--timestamp" if timestamp else "--timestamp=none")
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
        args = list(signing)
        if path.name in {"python3.11", "node"}:
            args += ["--entitlements", str(entitlements)]
        run(*args, path)
    run(*signing, app)
    run("codesign", "--verify", "--deep", "--strict", app)


def install_interface(resources: Path) -> None:
    """从桌面自有依赖构建离线界面，签名前安装字体、许可、图标和语言资源"""
    interface = DESKTOP / "ui"
    run("pnpm", "install", "--frozen-lockfile", cwd=interface)
    run("pnpm", "build", cwd=interface)
    shutil.copytree(interface / "dist", resources / "ui")
    shutil.copy2(interface / "public/brand.svg", resources / "brand.svg")
    shutil.copy2(
        interface / "dist/THIRD_PARTY_NOTICES.txt",
        resources / "licenses/desktop-ui.txt",
    )
    shutil.copy2(DESKTOP / "scripts/update_archive.py", resources / "update_archive.py")
    shutil.copytree(DESKTOP / "resources/icons", resources / "icons")
    for language in ("en", "zh-Hans"):
        shutil.copytree(
            DESKTOP / f"resources/{language}.lproj", resources / f"{language}.lproj"
        )
    run(
        "xcrun",
        "swift",
        "-swift-version",
        "6",
        DESKTOP / "scripts/render_app_icon.swift",
        resources / "brand.svg",
        resources / "TinkerFin.icns",
    )


def build(
    *,
    output: Path,
    python_archive: Path,
    node_archive: Path,
    identity: str,
    keychain: Path | None = None,
    timestamp: bool = False,
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
        install_interface(resources)
        license_inventory(resources)
        relocate_entrypoints(resources)
        release = tomllib.loads((DESKTOP / "pyproject.toml").read_text())["project"][
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
                    "LSMultipleInstancesProhibited": True,
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
            *sorted((DESKTOP / "native").glob("*.swift")),
            "-o",
            executable,
        )
        run(
            "swiftc",
            "-swift-version",
            "6",
            "-O",
            "-target",
            "arm64-apple-macosx15.0",
            *sorted(
                path
                for path in (DESKTOP / "native").glob("*.swift")
                if path.name != "CompanionApp.swift"
            ),
            DESKTOP / "native/installer/InstallerApp.swift",
            "-o",
            resources / "TinkerFinUpdater",
        )
        # 所有链接必须位于应用中，防止把构建机目录当成安装依赖
        for path in app.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(app):
                raise ValueError(f"应用含外部链接：{path.relative_to(app)}")
        sign(app, identity, temporary, keychain=keychain, timestamp=timestamp)
        run(
            resources / "python/bin/python3",
            "-I",
            "-B",
            "-m",
            "tinkerfin_desktop",
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
        "tinkerfin_desktop",
        "--resources",
        output / "Contents/Resources",
        "--check",
    )
    run("codesign", "--verify", "--deep", "--strict", output)
    run(output / "Contents/Resources/python/bin/pip3", "--version")
    archive = output.with_suffix(".zip")
    create_archive(output, archive)
    archive.with_suffix(".zip.sha256").write_text(
        f"{digest(archive)}  {archive.name}\n"
    )
    return output


def main() -> None:
    """复用校验过的运行时输入；默认生成未经 Apple 公证的 ad-hoc 签名包"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python-archive", type=Path)
    parser.add_argument("--node-archive", type=Path)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--identity", default="-", help="签名证书指纹；- 使用临时签名")
    parser.add_argument("--keychain", type=Path, help="存放签名身份的钥匙串路径")
    parser.add_argument(
        "--timestamp", action="store_true", help="请求 Apple 签名时间戳"
    )
    args = parser.parse_args()
    print(
        build(
            output=args.output,
            python_archive=args.python_archive or obtain(PYTHON, args.cache),
            node_archive=args.node_archive or obtain(NODE, args.cache),
            identity=args.identity,
            keychain=args.keychain,
            timestamp=args.timestamp,
        )
    )


if __name__ == "__main__":
    main()
