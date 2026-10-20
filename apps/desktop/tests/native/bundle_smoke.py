"""在随包 Python 中验证原生沙箱、依赖与浏览器，不使用开发环境模块"""

import asyncio
import importlib
import json
import shlex
import sys
import tempfile
from pathlib import Path

from tinkerfin_desktop.browser import BrowserBinding, BrowserManager
from tinkerfin_desktop.browser_install import BrowserProvider
from tinkerfin_desktop.configuration import WorkspacePermissions
from tinkerfin_desktop.installation import Installation
from tinkerfin_sandbox import LocalAccess, LocalWorkspace


async def main() -> None:
    resources, directory = (Path(value).resolve() for value in sys.argv[1:])
    program = Installation.read(resources)
    for name in (
        "tinkerfin_remote",
        "tinkerfin_sandbox",
        "tinkerfin_desktop",
        "mcp",
        "websockets",
        "pydantic",
    ):
        origin = importlib.import_module(name).__file__
        assert origin is not None
        assert Path(origin).resolve().is_relative_to(resources)
    assert Path(sys.prefix).resolve().is_relative_to(resources)
    with tempfile.TemporaryDirectory(prefix="tf-bundle-", dir="/tmp") as temporary:
        page = directory / "index.html"
        page.write_text("<button onclick=\"this.textContent='done'\">Run</button>")
        profile = directory / ".tinkerfin/browser-profiles/bundle-smoke"
        cache = directory / "cache"
        aliases = Path(temporary).resolve()
        for path in (profile, cache):
            path.mkdir(parents=True)
        manager = BrowserManager(
            program.browser,
            BrowserProvider(cache),
            sandbox=program.sandbox,
            projects_root=directory.parent,
            protected_paths=(),
            read_only_paths=(resources, cache, aliases),
            aliases=aliases,
        )
        try:
            async with BrowserBinding(
                manager,
                directory=directory,
                thread="bundle-smoke",
                profile=profile,
                permissions=WorkspacePermissions(network="normal", browser=True),
            ) as binding:
                module = importlib.import_module("tinkerfin_desktop.browser_command")
                assert module.__file__ is not None
                async with (
                    LocalWorkspace(
                        directory,
                        sandbox_executable=program.sandbox,
                        access=LocalAccess(
                            filesystem="user",
                            read_only_paths=(resources, cache, aliases),
                        ),
                        commands={
                            "node": resources / "node/bin/node",
                            "npm": resources / "node/bin/npm",
                            "npx": resources / "node/bin/npx",
                            "tinkerfin-browser": Path(module.__file__),
                        },
                        environment={
                            "TINKERFIN_BROWSER_SOCKET": str(binding.socket),
                            "TINKERFIN_BROWSER_TOKEN": binding.token,
                        },
                        on_command_abort=binding.abort,
                    ) as workspace,
                    workspace.open() as session,
                ):
                    dependencies = await session.aexecute(
                        "python -m venv .venv && .venv/bin/python -m pip --version && node --version && npm --version"
                    )
                    assert dependencies.exit_code == 0, dependencies.output
                    assert "v22.23.3" in dependencies.output
                    commands = [
                        ("browser_navigate", {"url": page.as_uri()}),
                        (
                            "browser_evaluate",
                            {
                                "function": "() => document.querySelector('button').click()"
                            },
                        ),
                        ("browser_snapshot", {}),
                        (
                            "browser_take_screenshot",
                            {"type": "png", "filename": str(directory / "screen.png")},
                        ),
                    ]
                    result = await session.aexecute(
                        " && ".join(
                            shlex.join(
                                (
                                    "tinkerfin-browser",
                                    "call",
                                    name,
                                    json.dumps(arguments),
                                )
                            )
                            for name, arguments in commands
                        )
                    )
                    assert result.exit_code == 0, result.output
                    assert "done" in result.output
                    assert (
                        (directory / "screen.png").read_bytes().startswith(b"\x89PNG")
                    )
        finally:
            await manager.aclose()
    print(
        json.dumps(
            {"runtime": "verified", "browser": "verified", "dependencies": "verified"}
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
