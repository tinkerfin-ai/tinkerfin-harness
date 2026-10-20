"""构建缓存必须命中固定内容，失败下载不能成为下次构建输入"""

import hashlib
import importlib.util
import io
import sys
from pathlib import Path

import pytest


@pytest.fixture
def inputs(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/runtime_inputs.py"
    spec = importlib.util.spec_from_file_location("runtime_inputs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_verified_cache_is_reused_across_worktrees(inputs, tmp_path, monkeypatch):
    payload = b"official runtime fixture"
    archive = inputs.RuntimeArchive(
        "fixture",
        "1.0",
        "https://example.invalid/runtime",
        hashlib.sha256(payload).hexdigest(),
    )
    requested = []

    def download(request, *, timeout):
        requested.append(request.full_url)
        assert timeout == 30
        return io.BytesIO(payload)

    monkeypatch.setattr(inputs.urllib.request, "urlopen", download)
    first = inputs.obtain(archive, tmp_path)
    assert inputs.obtain(archive, tmp_path) == first
    assert first.read_bytes() == payload
    assert requested == [archive.url]
    first.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="缓存损坏"):
        inputs.obtain(archive, tmp_path)
    assert requested == [archive.url]


def test_incomplete_download_is_not_published(inputs, tmp_path, monkeypatch):
    archive = inputs.RuntimeArchive(
        "fixture", "1.0", "https://example.invalid", "a" * 64
    )
    monkeypatch.setattr(
        inputs.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(b"truncated"),
    )
    with pytest.raises(ValueError, match="摘要不符"):
        inputs.obtain(archive, tmp_path)
    assert not (tmp_path / archive.sha256).exists()
    assert not list(tmp_path.glob(".download-*"))
