"""Build verified wheels from temporary copies of the current working tree."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PATHS = (
    "packages/tinkerfin-contracts",
    "packages/tinkerfin-notifications",
    "packages/tinkerfin-native-stream",
    "packages/tinkerfin-agui-adapter",
    "packages/tinkerfin",
    "packages/tinkerfin-automation",
    "packages/tinkerfin-messaging",
    "packages/tinkerfin-gateway",
    "packages/tinkerfin-tracing",
    "packages/tinkerfin-sandbox",
    "packages/tinkerfin-langgraph-store",
    "packages/tinkerfin-sqlalchemy",
    "apps/studio/server",
)

_IGNORE_ARTIFACTS = shutil.ignore_patterns(
    ".git",
    ".agents",
    ".codex",
    ".claude",
    ".DS_Store",
    ".venv",
    ".cache",
    ".*-cache",
    ".*_cache",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.egg-info",
    "*.dist-info",
    "node_modules",
)


def _copy_project(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        excluded = set(_IGNORE_ARTIFACTS(directory, names))
        if Path(directory) == source:
            excluded.update({"build", "dist"}.intersection(names))
        if Path(directory) in {source, source / "deploy"}:
            excluded.update(shutil.ignore_patterns(".env", ".env.*")(directory, names))
        if Path(directory) == source / "deploy":
            excluded.update({"secrets"}.intersection(names))
        return excluded

    # Copy filesystem contents, including uncommitted and untracked source files.
    # Setuptools writes build/lib and egg-info only into this owned temporary copy.
    shutil.copytree(source, destination, ignore=ignore)


def _source_payload(source_root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }


def _verify_wheel(wheel: Path, expected: dict[str, bytes]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"{wheel.name}: duplicate archive entries")
        actual = {
            name: archive.read(name)
            for name in names
            if not name.endswith("/")
            and not name.partition("/")[0].endswith(".dist-info")
        }
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(
            name
            for name in expected.keys() & actual.keys()
            if expected[name] != actual[name]
        )
        raise ValueError(
            f"{wheel.name}: wheel/source mismatch; "
            f"missing={missing}, extra={extra}, changed={changed}"
        )


def build_wheels(
    project_paths: tuple[str, ...],
    output: Path,
    *,
    python: str,
    offline: bool = False,
) -> tuple[Path, ...]:
    """Build selected repository projects and publish only verified wheel payloads.

    Each build uses a private copy of the current project files. Python modules,
    stubs, typing markers, and package resources must match that copy byte for byte.
    All projects pass verification before any wheel is copied to the destination.
    Existing output wheels are rejected, and working-tree artifacts are untouched.

    Args:
        project_paths: Repository-relative paths from ``PROJECT_PATHS``.
        output: Destination directory without existing wheels.
        python: Interpreter executable or version accepted by ``uv build``.
        offline: Resolve build requirements from the existing uv cache only.

    Returns:
        Paths to the verified output wheels.

    Raises:
        ValueError: Projects are unknown, output contains wheels, or payloads differ.
        OSError: Project files or the output directory cannot be read or written.
        subprocess.SubprocessError: A build fails or exceeds its five-minute limit.
    """

    if not project_paths or len(project_paths) != len(set(project_paths)):
        raise ValueError("Select at least one project, without duplicates")
    unknown = set(project_paths) - set(PROJECT_PATHS)
    if unknown:
        raise ValueError(f"Unknown build projects: {sorted(unknown)}")
    output = output.resolve()
    if any(output.glob("*.whl")):
        raise ValueError(f"Output already contains wheels: {output}")

    with tempfile.TemporaryDirectory(prefix="tinkerfin-wheel-build-") as raw:
        temporary = Path(raw)
        sources = temporary / "sources"
        payloads: dict[str, dict[str, bytes]] = {}
        for project_path in project_paths:
            project = sources / project_path
            _copy_project(ROOT / project_path, project)
            payloads[project_path] = _source_payload(project / "src")

        verified: list[Path] = []
        for index, project_path in enumerate(project_paths):
            project_output = temporary / "wheels" / str(index)
            command = [
                "uv",
                "build",
                "--quiet",
                "--wheel",
                "--python",
                python,
                "--out-dir",
                str(project_output),
                "--no-create-gitignore",
            ]
            if offline:
                command.append("--offline")
            command.append(str(sources / project_path))
            subprocess.run(command, cwd=ROOT, check=True, timeout=300)
            wheels = tuple(project_output.glob("*.whl"))
            if len(wheels) != 1:
                raise ValueError(
                    f"{project_path}: expected one wheel, got {len(wheels)}"
                )
            _verify_wheel(wheels[0], payloads[project_path])
            verified.append(wheels[0])

        output.mkdir(parents=True, exist_ok=True)
        published: list[Path] = []
        for wheel in verified:
            target = output / wheel.name
            with wheel.open("rb") as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination)
            published.append(target)
        return tuple(published)


def main() -> None:
    """Run the shared repository wheel build command."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "projects",
        nargs="*",
        metavar="PROJECT_PATH",
        help="repository-relative project paths; defaults to all framework and Studio projects",
    )
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    try:
        wheels = build_wheels(
            tuple(args.projects) or PROJECT_PATHS,
            args.out_dir,
            python=args.python,
            offline=args.offline,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Wheel build failed: {error}\n")
    for wheel in wheels:
        print(wheel)


if __name__ == "__main__":
    main()
