"""Select checks from changed sources and the locked dependency graph."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = ("scripts", "server", "web", "packages", "wheels", "desktop")


def _runtime_change(path: str) -> bool:
    return not path.endswith(".md") or bool(
        {"src", "public", "resources", "skills"} & set(Path(path).parts)
    )


def lock_scope(
    content: str, roots: tuple[str, ...], *, groups: tuple[str, ...] = ()
) -> dict[str, str]:
    """Return canonical records for the selected locked dependency closure."""
    records = tomllib.loads(content)["package"]
    pending = [(name, ()) for name in roots]
    if groups:
        for record in records:
            if record["name"] == "tinkerfin-harness":
                for group in groups:
                    pending.extend(
                        (edge["name"], tuple(edge.get("extra", ())))
                        for edge in record.get("dev-dependencies", {}).get(group, [])
                    )
    visited: set[tuple[str, tuple[str, ...]]] = set()
    selected: dict[str, str] = {}
    while pending:
        name, extras = pending.pop()
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        matches = [item for item in records if item["name"] == name]
        if not matches:
            continue
        selected[name] = json.dumps(matches, sort_keys=True, separators=(",", ":"))
        for record in matches:
            edges = list(record.get("dependencies", []))
            for extra in extras:
                edges.extend(record.get("optional-dependencies", {}).get(extra, []))
            pending.extend(
                (edge["name"], tuple(edge.get("extra", ()))) for edge in edges
            )
    return selected


def source_paths(scope: dict[str, str]) -> set[str]:
    """Return workspace source roots that belong to a dependency closure."""
    return {
        record["source"]["editable"]
        for encoded in scope.values()
        for record in json.loads(encoded)
        if "editable" in record.get("source", {})
    }


def select_checks(
    paths: list[str], before_lock: str, after_lock: str
) -> dict[str, bool]:
    """Select checks; unrelated registry dependency updates do not select desktop."""
    selected: dict[str, bool] = dict.fromkeys(CHECKS, False)
    changed = set(paths)
    if changed & {
        "pyproject.toml",
        "scripts/ci_changes.py",
        "tests/test_ci_changes.py",
        ".github/workflows/packages-quality.yml",
    }:
        return dict.fromkeys(CHECKS, True)

    for key, target in (
        ("desktop", "tinkerfin-desktop"),
        ("server", "tinkerfin-studio"),
    ):
        before = lock_scope(before_lock, (target,))
        after = lock_scope(after_lock, (target,))
        roots = source_paths(before) | source_paths(after)
        selected[key] = any(
            path.startswith(root + "/") and _runtime_change(path)
            for path in changed
            for root in roots
        ) or ("uv.lock" in changed and before != after)
    tooling_changed = "uv.lock" in changed and lock_scope(
        before_lock, (), groups=("test", "lint", "packaging")
    ) != lock_scope(after_lock, (), groups=("test", "lint", "packaging"))
    if tooling_changed:
        for key in ("scripts", "server", "packages", "wheels", "desktop"):
            selected[key] = True
    selected["scripts"] |= any(
        path.startswith(("scripts/", "tests/")) for path in changed
    )
    selected["server"] |= any(
        path.startswith(("scripts/", "tests/"))
        or path == ".github/workflows/studio-image.yml"
        for path in changed
    )
    selected["web"] = any(
        path.startswith("apps/studio-web/")
        and _runtime_change(path)
        or path.startswith("packages/tinkerfin/tests/fixtures/")
        or path == "scripts/studio_checks.py"
        for path in changed
    )
    selected["packages"] |= any(
        path.startswith("packages/")
        and _runtime_change(path)
        or path
        in {"pyright-packages-strict.json", "tests/test_package_logging_contract.py"}
        for path in changed
    )
    if "uv.lock" in changed:
        local = tuple(
            item["name"]
            for item in tomllib.loads(after_lock)["package"]
            if item.get("source", {}).get("editable", "").startswith("packages/")
        )
        selected["packages"] |= lock_scope(before_lock, local) != lock_scope(
            after_lock, local
        )
    selected["desktop"] |= any(
        path.startswith(("apps/desktop/", "packages/tinkerfin/"))
        and _runtime_change(path)
        or path
        in {
            "apps/studio-web/public/brand/tinkerfin.svg",
            ".github/workflows/desktop-release.yml",
            "scripts/build_wheels.py",
        }
        for path in changed
    )
    selected["wheels"] |= (
        selected["packages"]
        or any(
            path.startswith(("apps/desktop/src/", "apps/studio-server/src/"))
            or path
            in {"apps/desktop/pyproject.toml", "apps/studio-server/pyproject.toml"}
            for path in changed
        )
        or ("uv.lock" in changed and (selected["desktop"] or selected["server"]))
        or any(path.startswith("tests/packaging/") for path in changed)
        or "scripts/build_wheels.py" in changed
    )
    return selected


def git(*arguments: str) -> str:
    """Read an exact Git state; an unknown base fails instead of skipping checks."""
    return subprocess.check_output(("git", *arguments), cwd=ROOT, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("BASE", ""))
    parser.add_argument("--head", default=os.environ.get("HEAD", "HEAD"))
    args = parser.parse_args()
    if not args.base or set(args.base) == {"0"}:
        checks = dict.fromkeys(CHECKS, True)
    else:
        checks = select_checks(
            git(
                "diff", "--name-only", "--no-renames", args.base, args.head
            ).splitlines(),
            git("show", f"{args.base}:uv.lock"),
            git("show", f"{args.head}:uv.lock"),
        )
    print(json.dumps(checks, sort_keys=True))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a") as stream:
            for key, value in checks.items():
                stream.write(f"{key}={str(value).lower()}\n")


if __name__ == "__main__":
    main()
