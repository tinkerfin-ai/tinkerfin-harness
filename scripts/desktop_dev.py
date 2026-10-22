"""Build and launch a desktop app that matches this worktree's current sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from scripts.ci_changes import lock_scope, source_paths

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(root: Path) -> str:
    """Hash bundled sources and runtime dependencies, excluding unrelated apps."""
    scope = lock_scope((root / "uv.lock").read_text(), ("tinkerfin-desktop",))
    content = hashlib.sha256(json.dumps(scope, sort_keys=True).encode())
    paths: set[Path] = {
        root / "scripts/build_wheels.py",
        root / "LICENSE",
    }
    for relative in source_paths(scope):
        project = root / relative
        paths.update(
            project / name for name in ("pyproject.toml", "README.md", "LICENSE")
        )
        paths.update((project / "src").rglob("*"))
    desktop = root / "apps/desktop"
    for directory in ("native", "resources", "scripts", "runtime/licenses"):
        paths.update((desktop / directory).rglob("*"))
    paths.update(
        desktop / "runtime" / name
        for name in ("package.json", "package-lock.json", "browser-service.mjs")
    )
    interface = desktop / "ui"
    for name in (
        "package.json",
        "pnpm-lock.yaml",
        "index.html",
        "tsconfig.json",
        "vite.config.ts",
    ):
        paths.add(interface / name)
    for directory in ("src", "public"):
        paths.update((interface / directory).rglob("*"))
    for path in sorted(paths):
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or path.name == ".DS_Store"
        ):
            continue
        content.update(path.relative_to(root).as_posix().encode() + b"\0")
        content.update(path.read_bytes())
    return content.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args()
    app = ROOT / ".cache/desktop" / fingerprint(ROOT) / "TinkerFin.app"
    if not app.exists():
        subprocess.run(
            (
                sys.executable,
                str(ROOT / "apps/desktop/scripts/build_macos.py"),
                "--output",
                str(app),
            ),
            cwd=ROOT,
            check=True,
        )
    subprocess.run(("codesign", "--verify", "--deep", "--strict", str(app)), check=True)
    print(app)
    if not args.build_only:
        subprocess.run(("open", str(app)), check=True)


if __name__ == "__main__":
    main()
