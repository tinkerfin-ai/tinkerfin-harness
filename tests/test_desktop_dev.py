"""Only bundled source and dependency changes invalidate a development app."""

from scripts.desktop_dev import fingerprint


def test_desktop_build_key_tracks_bundled_inputs(tmp_path):
    lock = """
[[package]]
name = "tinkerfin-desktop"
version = "0.1.1"
source = { editable = "apps/desktop" }
dependencies = [{name = "shared"}]
[[package]]
name = "shared"
version = "0.1.1"
source = { editable = "packages/shared" }
[[package]]
name = "server-only"
version = "1.0"
"""
    (tmp_path / "uv.lock").write_text(lock)
    source = tmp_path / "packages/shared/src/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n")
    initial = fingerprint(tmp_path)
    server = tmp_path / "apps/studio-server/src/server.py"
    server.parent.mkdir(parents=True)
    server.write_text("different server\n")
    (tmp_path / "uv.lock").write_text(
        lock.replace('version = "1.0"', 'version = "2.0"')
    )
    assert fingerprint(tmp_path) == initial
    source.write_text("value = 2\n")
    assert fingerprint(tmp_path) != initial
