"""Changes select consumers through actual workspace and registry dependencies."""

import pytest
from scripts.ci_changes import CHECKS, select_checks

LOCK = """
[[package]]
name = "tinkerfin-desktop"
source = { editable = "apps/desktop" }
dependencies = [{name = "shared"}, {name = "desktop-only"}]
[[package]]
name = "tinkerfin-studio"
source = { editable = "apps/studio-server" }
dependencies = [{name = "shared"}, {name = "server-only"}]
[[package]]
name = "shared"
source = { editable = "packages/shared" }
[[package]]
name = "desktop-only"
version = "1.0"
[[package]]
name = "server-only"
version = "1.0"
"""


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/cn/development.md", set()),
        ("apps/desktop/README.md", set()),
        ("apps/desktop/native/Main.swift", {"desktop"}),
        ("apps/desktop/src/tinkerfin_desktop/browser_help.md", {"desktop", "wheels"}),
        ("apps/studio-server/src/main.py", {"server", "wheels"}),
        ("apps/studio-server/src/prompts/system.md", {"server", "wheels"}),
        ("apps/studio-web/src/App.tsx", {"web"}),
        ("packages/shared/src/api.py", {"desktop", "server", "packages", "wheels"}),
        (
            "packages/shared/src/skills/browser/SKILL.md",
            {"desktop", "server", "packages", "wheels"},
        ),
        ("packages/unrelated/src/api.py", {"packages", "wheels"}),
        ("apps/studio-web/public/brand/tinkerfin.svg", {"web"}),
        ("apps/desktop/ui/src/App.tsx", {"desktop"}),
        ("apps/desktop/ui/pnpm-lock.yaml", {"desktop"}),
        (".github/workflows/packages-quality.yml", set(CHECKS)),
    ],
)
def test_changed_source_selects_its_consumers(path, expected):
    result = select_checks([path], LOCK, LOCK)
    assert {key for key, value in result.items() if value} == expected


def test_server_dependency_update_does_not_select_desktop():
    updated = LOCK.replace(
        'name = "server-only"\nversion = "1.0"', 'name = "server-only"\nversion = "2.0"'
    )
    result = select_checks(["uv.lock"], LOCK, updated)
    assert result["server"]
    assert not result["desktop"]
    assert not result["web"]


def test_removed_workspace_dependency_still_selects_former_consumer():
    updated = LOCK.replace('{name = "shared"}, ', "")
    result = select_checks(["packages/shared/src/api.py", "uv.lock"], LOCK, updated)
    assert result["desktop"] and result["server"]
