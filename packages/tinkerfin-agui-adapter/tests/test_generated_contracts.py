"""Drift gate for checked-in Python and Web AG-UI contract artifacts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_GENERATOR = _PACKAGE_ROOT / "scripts" / "generate_contracts.py"


def test_generated_contracts_match_the_current_public_models() -> None:
    """Require every checked-in cross-language artifact to match its model."""

    completed = subprocess.run(
        [sys.executable, str(_GENERATOR), "--check"],
        cwd=_PACKAGE_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
