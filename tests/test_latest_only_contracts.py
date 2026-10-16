"""Repository gate for TinkerFin-owned latest-only contracts."""

from __future__ import annotations

import re
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).parents[1]
_ROOTS = (
    *sorted((_REPOSITORY_ROOT / "packages").glob("*/src")),
    _REPOSITORY_ROOT / "apps" / "studio" / "server" / "src",
    _REPOSITORY_ROOT / "apps" / "studio" / "server" / "database",
    _REPOSITORY_ROOT / "apps" / "studio" / "web" / "src",
)
_TEXT_SUFFIXES = frozenset(
    {".json", ".md", ".py", ".pyi", ".sql", ".ts", ".tsx", ".txt"}
)
_FORBIDDEN = (
    re.compile(r"\bschemaVersion\b"),
    re.compile(r"\bschema_version\b"),
    re.compile(r"\bsnapshotVersion\b"),
    re.compile(r"\bsnapshot_version\b"),
    re.compile(r"\bplan_schema\b"),
    re.compile(r"\breview_actions\b"),
    re.compile(r"\btinkerfin(?:[.][A-Za-z0-9_-]+)+[.]v[0-9]+\b"),
    re.compile(r"\btinkerfin-plan-v[0-9]+\b"),
    re.compile(r"\btinkerfin:[A-Za-z0-9_-]+:v[0-9]+:"),
    re.compile(r"\btinkerfin(?:-[A-Za-z0-9_-]+)+:[A-Za-z0-9_-]+:v[0-9]+\b"),
    re.compile(
        r"\b(?:type_id|answer_type)\s*(?::[^=]+)?=\s*[\"']"
        r"[a-z][a-z0-9.-]*:[a-z][a-z0-9._-]*[.]v[0-9]+[\"']"
    ),
    re.compile(
        r"[\"']answerType[\"']\s*:\s*(?:\{\s*[\"']const[\"']\s*:\s*)?"
        r"[\"'][a-z][a-z0-9.-]*:[a-z][a-z0-9._-]*[.]v[0-9]+[\"']"
    ),
    re.compile(r"\bagui[.]event[.]v[0-9]+\b"),
    re.compile(r"\blanggraph[.]stream-part[.]v2[.]v[0-9]+\b"),
    re.compile(r"\btinkerfin_opensandbox_schema_versions\b"),
    re.compile(
        r"(?:tool-review|subagent-provenance)-v[0-9]+[.](?:fixture|schema)[.]json"
    ),
)


def _current_contract_files() -> tuple[Path, ...]:
    files: list[Path] = []
    for root in _ROOTS:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
                continue
            if ".test." in path.name or "tests" in path.parts:
                continue
            if any(
                excluded in path.parts
                for excluded in ("__pycache__", "build", "dist", "node_modules")
            ) or any(part.endswith(".egg-info") for part in path.parts):
                continue
            files.append(path)
    return tuple(sorted(files))


def test_tinkerfin_owned_sources_expose_only_current_contracts() -> None:
    violations: list[str] = []
    for path in _current_contract_files():
        content = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(content.splitlines(), start=1):
            for pattern in _FORBIDDEN:
                if pattern.search(line):
                    relative = path.relative_to(_REPOSITORY_ROOT)
                    violations.append(
                        f"{relative}:{line_number}: {pattern.pattern}: {line.strip()}"
                    )
    assert violations == []
