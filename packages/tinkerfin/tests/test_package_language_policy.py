"""Language policy checks for publishable package code and tests."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

_PACKAGES_ROOT = Path(__file__).parents[2]
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_IGNORED_DIRECTORIES = frozenset({"__pycache__", "build", "dist"})


def _python_files() -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(_PACKAGES_ROOT.glob("**/*.py"))
        if _IGNORED_DIRECTORIES.isdisjoint(path.relative_to(_PACKAGES_ROOT).parts)
        if not any(
            part.startswith(".") for part in path.relative_to(_PACKAGES_ROOT).parts
        )
    )


def _docstrings(path: Path) -> tuple[tuple[int, str], ...]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    values: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        values.append((first.lineno, first.value.value))
    return tuple(values)


def _comments(path: Path) -> tuple[tuple[int, str], ...]:
    source = path.read_text(encoding="utf-8")
    return tuple(
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    )


def test_package_comments_and_docstrings_use_english() -> None:
    """Reject CJK prose only in comments and docstrings, not protocol fixtures."""

    violations: list[str] = []
    for path in _python_files():
        # Most files contain no CJK text. Parse only candidates so this repository
        # policy does not repeatedly tokenize the entire suite under coverage.
        if _CJK.search(path.read_text(encoding="utf-8")) is None:
            continue
        relative = path.relative_to(_PACKAGES_ROOT.parent)
        for kind, records in (
            ("docstring", _docstrings(path)),
            ("comment", _comments(path)),
        ):
            for line, value in records:
                if _CJK.search(value):
                    violations.append(f"{relative}:{line}: {kind}")
    assert violations == [], "Package language policy violations:\n" + "\n".join(
        violations
    )
