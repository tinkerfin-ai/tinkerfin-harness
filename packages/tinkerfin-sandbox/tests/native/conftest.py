"""Discover the explicitly installed native sandbox integration."""

import os
import shutil
import sys
from pathlib import Path

import pytest


@pytest.fixture
def sandbox() -> Path:
    if sys.platform != "darwin":
        pytest.skip("native local isolation requires macOS")
    selected = os.environ.get("TINKERFIN_TEST_SRT") or shutil.which("srt")
    if selected is None:
        probe = (
            Path(__file__).resolve().parents[4]
            / ".agents/tools/sandbox-runtime/node_modules/.bin/srt"
        )
        if probe.is_file():
            selected = str(probe)
    if selected is None:
        pytest.skip("install the fixed SRT runtime to run native isolation contracts")
    return Path(selected)
