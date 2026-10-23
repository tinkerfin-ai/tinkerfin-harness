"""Build and install every public wheel boundary in isolated environments."""

from __future__ import annotations

import ast
import importlib.metadata
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from scripts.build_wheels import PROJECT_PATHS, project_paths_for

_ROOT = Path(__file__).resolve().parents[2]
_DESKTOP_VERSION = tomllib.loads(
    (_ROOT / "apps/desktop/pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]


@dataclass(frozen=True, slots=True)
class _WheelProject:
    """Describe one local distribution and its required wheel artifacts."""

    name: str
    path: Path
    marker: str | None


@dataclass(frozen=True, slots=True)
class _InstallCase:
    """Describe one independently resolvable public installation boundary."""

    name: str
    spec: str
    imports: tuple[str, ...]
    required_distributions: tuple[str, ...]
    forbidden_distributions: tuple[str, ...] = ()
    forbidden_modules: tuple[str, ...] = ()
    smoke: str | None = None


_PROJECTS = (
    _WheelProject(
        "tinkerfin-contracts",
        _ROOT / "packages/tinkerfin-contracts",
        "tinkerfin_contracts/py.typed",
    ),
    _WheelProject(
        "tinkerfin-notifications",
        _ROOT / "packages/tinkerfin-notifications",
        "tinkerfin_notifications/py.typed",
    ),
    _WheelProject(
        "tinkerfin-native-stream",
        _ROOT / "packages/tinkerfin-native-stream",
        "tinkerfin_native_stream/py.typed",
    ),
    _WheelProject(
        "tinkerfin-agui-adapter",
        _ROOT / "packages/tinkerfin-agui-adapter",
        "tinkerfin_agui_adapter/py.typed",
    ),
    _WheelProject("tinkerfin", _ROOT / "packages/tinkerfin", "tinkerfin/py.typed"),
    _WheelProject(
        "tinkerfin-automation",
        _ROOT / "packages/tinkerfin-automation",
        "tinkerfin_automation/py.typed",
    ),
    _WheelProject(
        "tinkerfin-messaging",
        _ROOT / "packages/tinkerfin-messaging",
        "tinkerfin_messaging/py.typed",
    ),
    _WheelProject(
        "tinkerfin-gateway",
        _ROOT / "packages/tinkerfin-gateway",
        "tinkerfin_gateway/py.typed",
    ),
    _WheelProject(
        "tinkerfin-tracing",
        _ROOT / "packages/tinkerfin-tracing",
        "tinkerfin_tracing/py.typed",
    ),
    _WheelProject(
        "tinkerfin-sandbox",
        _ROOT / "packages/tinkerfin-sandbox",
        "tinkerfin_sandbox/py.typed",
    ),
    _WheelProject(
        "tinkerfin-langgraph-store",
        _ROOT / "packages/tinkerfin-langgraph-store",
        "tinkerfin_langgraph_store/py.typed",
    ),
    _WheelProject(
        "tinkerfin-sqlalchemy",
        _ROOT / "packages/tinkerfin-sqlalchemy",
        "tinkerfin_sqlalchemy/py.typed",
    ),
    _WheelProject(
        "tinkerfin-remote",
        _ROOT / "packages/tinkerfin-remote",
        "tinkerfin_remote/py.typed",
    ),
    _WheelProject("tinkerfin-studio", _ROOT / "apps/studio-server", None),
    _WheelProject(
        "tinkerfin-desktop", _ROOT / "apps/desktop", "tinkerfin_desktop/py.typed"
    ),
)

_CORE_CASES = (
    _InstallCase(
        "sandbox-native",
        "tinkerfin-sandbox==0.1.1",
        ("tinkerfin_sandbox",),
        ("tinkerfin-sandbox", "tinkerfin-contracts", "deepagents"),
        ("mcp", "opensandbox", "tinkerfin-notifications", "tinkerfin-studio"),
        ("opensandbox", "tinkerfin_notifications", "tinkerfin_studio"),
        smoke="sandbox_native",
    ),
    _InstallCase(
        "sandbox-native-mcp",
        "tinkerfin-sandbox[mcp]==0.1.1",
        ("tinkerfin_sandbox", "mcp"),
        ("tinkerfin-sandbox", "mcp"),
        ("opensandbox", "tinkerfin-studio"),
        ("opensandbox", "tinkerfin_studio"),
        smoke="sandbox_native",
    ),
    _InstallCase(
        "remote-core",
        "tinkerfin-remote==0.1.1",
        ("tinkerfin_remote",),
        ("tinkerfin-remote", "tinkerfin-contracts", "tinkerfin-sqlalchemy"),
        ("tinkerfin-studio", "tinkerfin", "tinkerfin-sandbox", "opensandbox"),
    ),
    _InstallCase(
        "companion-core",
        f"tinkerfin-desktop=={_DESKTOP_VERSION}",
        ("tinkerfin_desktop", "tinkerfin_desktop.host"),
        (
            "tinkerfin-desktop",
            "tinkerfin-sandbox",
            "tinkerfin-remote",
            "mcp",
        ),
        ("tinkerfin-studio", "tinkerfin", "opensandbox"),
        smoke="companion",
    ),
    _InstallCase(
        "notifications-core",
        "tinkerfin-notifications==0.1.1",
        ("tinkerfin_notifications",),
        ("tinkerfin-notifications", "pydantic"),
        ("tinkerfin", "langgraph", "redis", "sqlalchemy", "fastapi", "starlette"),
        ("tinkerfin", "langgraph", "redis", "sqlalchemy", "fastapi", "starlette"),
        smoke="notifications",
    ),
    _InstallCase(
        "gateway-core",
        "tinkerfin-gateway==0.1.1",
        ("tinkerfin_gateway",),
        (
            "tinkerfin-gateway",
            "tinkerfin",
            "tinkerfin-messaging",
            "tinkerfin-notifications",
            "ag-ui-protocol",
        ),
        ("fastapi", "starlette", "redis"),
        ("fastapi", "starlette", "redis"),
        smoke="gateway",
    ),
    _InstallCase(
        "contracts-core",
        "tinkerfin-contracts==0.1.1",
        ("tinkerfin_contracts",),
        ("tinkerfin-contracts",),
        ("tinkerfin", "ag-ui-protocol", "redis", "sqlalchemy"),
        ("tinkerfin", "ag_ui", "redis", "sqlalchemy"),
    ),
    _InstallCase(
        "native-stream-core",
        "tinkerfin-native-stream==0.1.1",
        ("tinkerfin_native_stream",),
        (
            "tinkerfin-native-stream",
            "tinkerfin-contracts",
            "langchain-core",
            "langgraph",
        ),
        ("tinkerfin", "tinkerfin-agui-adapter", "deepagents", "redis", "sqlalchemy"),
        ("tinkerfin", "tinkerfin_agui_adapter", "deepagents", "redis", "sqlalchemy"),
    ),
    _InstallCase(
        "adapter-core",
        "tinkerfin-agui-adapter==0.1.1",
        ("tinkerfin_agui_adapter",),
        ("tinkerfin-agui-adapter", "ag-ui-protocol", "tinkerfin-native-stream"),
        ("tinkerfin", "deepagents", "redis", "sqlalchemy"),
        ("tinkerfin", "deepagents", "redis", "sqlalchemy"),
    ),
    _InstallCase(
        "runtime-core",
        "tinkerfin==0.1.1",
        ("tinkerfin",),
        ("tinkerfin", "deepagents", "langchain", "langchain-core", "langgraph"),
        ("ag-ui-protocol", "tinkerfin-agui-adapter", "redis"),
        ("ag_ui", "tinkerfin_agui_adapter", "redis"),
    ),
    _InstallCase(
        "automation-core",
        "tinkerfin-automation==0.1.1",
        ("tinkerfin_automation", "tinkerfin", "langchain", "langchain_core"),
        (
            "tinkerfin-automation",
            "tinkerfin-contracts",
            "tinkerfin",
            "langchain",
            "langchain-core",
            "pydantic",
            "tzdata",
        ),
        (
            "ag-ui-protocol",
            "tinkerfin-agui-adapter",
            "tinkerfin-gateway",
            "tinkerfin-messaging",
            "apscheduler",
            "sqlalchemy",
            "aiosqlite",
            "asyncmy",
        ),
        (
            "ag_ui",
            "tinkerfin_agui_adapter",
            "tinkerfin_gateway",
            "tinkerfin_messaging",
            "apscheduler",
            "sqlalchemy",
            "aiosqlite",
            "asyncmy",
        ),
        smoke="automation_core",
    ),
    _InstallCase(
        "messaging-core",
        "tinkerfin-messaging==0.1.1",
        ("tinkerfin_messaging",),
        ("tinkerfin-messaging", "tinkerfin-contracts"),
        ("tinkerfin", "tinkerfin-native-stream", "ag-ui-protocol", "redis"),
        ("tinkerfin", "tinkerfin_native_stream", "ag_ui", "redis"),
    ),
    _InstallCase(
        "tracing-core",
        "tinkerfin-tracing==0.1.1",
        ("tinkerfin_tracing",),
        ("tinkerfin-tracing", "tinkerfin-contracts"),
        ("tinkerfin", "sqlalchemy", "aiosqlite", "asyncmy"),
        ("tinkerfin", "sqlalchemy", "aiosqlite", "asyncmy"),
    ),
    _InstallCase(
        "sandbox-opensandbox",
        "tinkerfin-sandbox[opensandbox]==0.1.1",
        ("tinkerfin_sandbox",),
        ("tinkerfin-sandbox", "deepagents", "opensandbox"),
        ("sqlalchemy", "aiosqlite", "asyncmy"),
        ("sqlalchemy", "aiosqlite", "asyncmy", "tinkerfin_sandbox._native"),
        smoke="sandbox_opensandbox",
    ),
    _InstallCase(
        "langgraph-store-core",
        "tinkerfin-langgraph-store==0.1.1",
        ("tinkerfin_langgraph_store",),
        ("tinkerfin-langgraph-store", "langgraph-checkpoint"),
        (
            "sqlalchemy",
            "asyncmy",
            "asyncpg",
            "aiosqlite",
            "langgraph-checkpoint-mysql",
        ),
        (
            "sqlalchemy",
            "asyncmy",
            "asyncpg",
            "aiosqlite",
            "langgraph.store.mysql",
        ),
    ),
    _InstallCase(
        "sqlalchemy-transactions",
        "tinkerfin-sqlalchemy==0.1.1",
        ("tinkerfin_sqlalchemy",),
        ("tinkerfin-sqlalchemy", "sqlalchemy", "greenlet"),
        ("asyncmy", "asyncpg", "aiosqlite"),
        ("asyncmy", "asyncpg", "aiosqlite"),
    ),
)

_EXTRA_CASES = (
    _InstallCase(
        "notifications-redis",
        "tinkerfin-notifications[redis]==0.1.1",
        ("tinkerfin_notifications.redis",),
        ("tinkerfin-notifications", "redis"),
        ("tinkerfin", "langgraph", "sqlalchemy", "fastapi", "starlette"),
        ("tinkerfin", "langgraph", "sqlalchemy", "fastapi", "starlette"),
    ),
    _InstallCase(
        "gateway-starlette",
        "tinkerfin-gateway[starlette]==0.1.1",
        ("tinkerfin_gateway.starlette",),
        ("tinkerfin-gateway", "starlette"),
        ("fastapi", "redis"),
        ("fastapi", "redis"),
        smoke="gateway_starlette",
    ),
    _InstallCase(
        "messaging-sqlalchemy",
        "tinkerfin-messaging[sqlalchemy]==0.1.1",
        ("tinkerfin_messaging.sqlalchemy", "tinkerfin_sqlalchemy"),
        ("tinkerfin-messaging", "tinkerfin-sqlalchemy", "sqlalchemy"),
        ("asyncmy", "asyncpg", "aiosqlite", "redis"),
        ("asyncmy", "asyncpg", "aiosqlite", "redis"),
    ),
    _InstallCase(
        "langgraph-store-sqlalchemy",
        "tinkerfin-langgraph-store[sqlalchemy]==0.1.1",
        ("tinkerfin_langgraph_store.sqlalchemy",),
        ("tinkerfin-langgraph-store", "tinkerfin-sqlalchemy", "sqlalchemy"),
        ("asyncmy", "asyncpg", "aiosqlite"),
        ("asyncmy", "asyncpg", "aiosqlite"),
    ),
    _InstallCase(
        "automation-gateway",
        "tinkerfin-automation[gateway]==0.1.1",
        (
            "tinkerfin_automation",
            "tinkerfin_gateway",
            "tinkerfin_agui_adapter",
            "tinkerfin_messaging.agui",
            "ag_ui.core",
        ),
        (
            "tinkerfin-automation",
            "tinkerfin-gateway",
            "tinkerfin-agui-adapter",
            "tinkerfin-messaging",
            "ag-ui-protocol",
        ),
        ("redis", "starlette", "fastapi", "sqlalchemy", "asyncmy", "aiosqlite"),
        ("redis", "starlette", "fastapi", "sqlalchemy", "asyncmy", "aiosqlite"),
        smoke="automation_gateway",
    ),
    _InstallCase(
        "automation-sqlalchemy",
        "tinkerfin-automation[sqlalchemy]==0.1.1",
        ("tinkerfin_automation.sqlalchemy", "tinkerfin_sqlalchemy"),
        ("tinkerfin-automation", "tinkerfin-sqlalchemy", "sqlalchemy"),
        ("apscheduler", "asyncmy", "asyncpg", "aiosqlite"),
        ("apscheduler", "asyncmy", "asyncpg", "aiosqlite"),
    ),
    _InstallCase(
        "runtime-agui",
        "tinkerfin[agui]==0.1.1",
        ("tinkerfin", "tinkerfin_agui_adapter", "ag_ui"),
        ("tinkerfin", "tinkerfin-agui-adapter", "ag-ui-protocol"),
        ("redis",),
        ("redis",),
    ),
    _InstallCase(
        "runtime-redis",
        "tinkerfin[redis]==0.1.1",
        ("tinkerfin", "redis"),
        ("tinkerfin", "redis"),
        ("ag-ui-protocol", "tinkerfin-agui-adapter"),
        ("ag_ui", "tinkerfin_agui_adapter"),
    ),
    _InstallCase(
        "runtime-agui-redis",
        "tinkerfin[agui,redis]==0.1.1",
        ("tinkerfin", "tinkerfin_agui_adapter", "ag_ui", "redis"),
        ("tinkerfin", "tinkerfin-agui-adapter", "ag-ui-protocol", "redis"),
    ),
    _InstallCase(
        "messaging-agui",
        "tinkerfin-messaging[agui]==0.1.1",
        ("tinkerfin_messaging", "ag_ui"),
        ("tinkerfin-messaging", "ag-ui-protocol"),
        ("tinkerfin", "redis", "tinkerfin-native-stream"),
        ("tinkerfin", "redis", "tinkerfin_native_stream"),
    ),
    _InstallCase(
        "messaging-native",
        "tinkerfin-messaging[native]==0.1.1",
        ("tinkerfin_messaging", "tinkerfin_native_stream"),
        ("tinkerfin-messaging", "tinkerfin-native-stream"),
        ("tinkerfin", "deepagents", "ag-ui-protocol", "redis"),
        ("tinkerfin", "deepagents", "ag_ui", "redis"),
    ),
    _InstallCase(
        "messaging-redis",
        "tinkerfin-messaging[redis]==0.1.1",
        ("tinkerfin_messaging", "redis"),
        ("tinkerfin-messaging", "redis"),
        ("tinkerfin", "ag-ui-protocol", "tinkerfin-native-stream"),
        ("tinkerfin", "ag_ui", "tinkerfin_native_stream"),
    ),
    _InstallCase(
        "messaging-agui-redis",
        "tinkerfin-messaging[agui,redis]==0.1.1",
        ("tinkerfin_messaging", "ag_ui", "redis"),
        ("tinkerfin-messaging", "ag-ui-protocol", "redis"),
        ("tinkerfin", "tinkerfin-native-stream"),
        ("tinkerfin", "tinkerfin_native_stream"),
    ),
    _InstallCase(
        "tracing-sqlalchemy",
        "tinkerfin-tracing[sqlalchemy]==0.1.1",
        ("tinkerfin_tracing", "tinkerfin_sqlalchemy", "sqlalchemy"),
        ("tinkerfin-tracing", "tinkerfin-sqlalchemy", "sqlalchemy"),
        ("asyncmy", "asyncpg", "aiosqlite"),
        ("asyncmy", "asyncpg", "aiosqlite"),
    ),
    _InstallCase(
        "sandbox-sqlalchemy",
        "tinkerfin-sandbox[sqlalchemy]==0.1.1",
        ("tinkerfin_sandbox", "tinkerfin_sqlalchemy", "sqlalchemy"),
        ("tinkerfin-sandbox", "tinkerfin-sqlalchemy", "sqlalchemy", "opensandbox"),
        ("asyncmy", "asyncpg", "aiosqlite"),
        ("asyncmy", "asyncpg", "aiosqlite", "tinkerfin_sandbox._native"),
        smoke="sandbox_sqlalchemy",
    ),
)

_STUDIO_CASE = _InstallCase(
    "studio-production",
    "tinkerfin-studio==0.1.1",
    (
        "tinkerfin_studio",
        "tinkerfin",
        "tinkerfin_langgraph_store",
        "tinkerfin_messaging",
        "tinkerfin_notifications",
        "tinkerfin_gateway",
        "tinkerfin_sandbox",
        "tinkerfin_tracing",
    ),
    (
        "tinkerfin-studio",
        "tinkerfin",
        "tinkerfin-langgraph-store",
        "tinkerfin-messaging",
        "tinkerfin-notifications",
        "tinkerfin-gateway",
        "tinkerfin-sandbox",
        "tinkerfin-tracing",
    ),
    ("aiomysql", "pymysql", "langgraph-checkpoint-mysql"),
    ("aiomysql", "pymysql"),
)

_CASES = (*_CORE_CASES, *_EXTRA_CASES, _STUDIO_CASE)


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        command,
        cwd=_ROOT,
        env={
            **(os.environ if environment is None else environment),
            "UV_FIND_LINKS": str(_ROOT / ".cache/test-wheels"),
            "UV_NO_INDEX": "true",
            "UV_OFFLINE": "true",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _source_python_payload(source_root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".pyi"}
    }


def _wheel_python_payload(archive: zipfile.ZipFile) -> dict[str, bytes]:
    return {
        name: archive.read(name)
        for name in archive.namelist()
        if Path(name).suffix in {".py", ".pyi"}
    }


@pytest.mark.packaging_e2e
def test_python_payload_comparison_detects_same_path_stale_content(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    source_module = source_root / "example" / "module.py"
    source_module.parent.mkdir(parents=True)
    source_module.write_text("CURRENT = True\n", encoding="utf-8")
    wheel = tmp_path / "stale.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("example/module.py", "CURRENT = False\n")
    with zipfile.ZipFile(wheel) as archive:
        wheel_payload = _wheel_python_payload(archive)

    assert wheel_payload != _source_python_payload(source_root)


def _is_local_import(source_root: Path, module: str) -> bool:
    module_path = source_root.joinpath(*module.split("."))
    return module_path.is_dir() or module_path.with_suffix(".py").is_file()


def _absolute_imports(node: ast.AST, python_version: tuple[int, int]) -> set[str]:
    """Collect dependencies active under explicit minimum-Python guards."""
    match node:
        case ast.Import(names=names):
            return {alias.name for alias in names}
        case ast.ImportFrom(level=0, module=str() as module):
            return {module}
        case ast.If(
            test=ast.Compare(
                left=ast.Attribute(value=ast.Name(id="sys"), attr="version_info"),
                ops=[ast.GtE()],
                comparators=[
                    ast.Tuple(
                        elts=[
                            ast.Constant(value=int() as major),
                            ast.Constant(value=int() as minor),
                        ]
                    )
                ],
            ),
            body=body,
            orelse=orelse,
        ):
            children = body if python_version >= (major, minor) else orelse
        case _:
            children = list(ast.iter_child_nodes(node))
    return set().union(
        *(_absolute_imports(child, python_version) for child in children)
    )


@pytest.mark.parametrize("python_version", [(3, 11), (3, 14)])
def test_dependency_imports_respect_minimum_python_guard(
    python_version: tuple[int, int],
) -> None:
    source = ast.parse(
        "import sys\nimport httpx\n"
        "if sys.version_info >= (3, 14):\n"
        "    from compression.zstd import ZstdError\n"
    )
    expected = {"sys", "httpx"}
    if python_version >= (3, 14):
        expected.add("compression.zstd")
    assert _absolute_imports(source, python_version) == expected
    assert _absolute_imports(
        ast.parse("from compression.zstd import ZstdError"), python_version
    ) == {"compression.zstd"}


@pytest.mark.packaging_e2e
def test_first_party_projects_declare_every_direct_import_distribution() -> None:
    module_distributions = importlib.metadata.packages_distributions()
    violations: list[str] = []
    for project in _PROJECTS:
        source_root = project.path / "src"
        pyproject = tomllib.loads(
            (project.path / "pyproject.toml").read_text(encoding="utf-8")
        )
        project_metadata = pyproject["project"]
        declared = {
            canonicalize_name(Requirement(requirement).name)
            for requirement in project_metadata.get("dependencies", [])
        }
        for requirements in project_metadata.get("optional-dependencies", {}).values():
            declared.update(
                canonicalize_name(Requirement(requirement).name)
                for requirement in requirements
            )

        imports: set[str] = set()
        for source in source_root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            imports.update(_absolute_imports(tree, sys.version_info[:2]))

        for module in sorted(imports):
            root_module = module.partition(".")[0]
            if root_module in sys.stdlib_module_names or _is_local_import(
                source_root, module
            ):
                continue
            distributions = {
                canonicalize_name(distribution)
                for distribution in module_distributions.get(root_module, ())
            }
            if not distributions:
                violations.append(
                    f"{project.name}: import {module!r} has no installed distribution"
                )
            elif declared.isdisjoint(distributions):
                violations.append(
                    f"{project.name}: import {module!r} requires one of "
                    f"{sorted(distributions)!r}"
                )

    assert violations == []


@pytest.fixture(scope="session")
def dependency_wheels() -> Path:
    """Use the hash-verified wheelhouse prepared before running offline checks."""
    wheelhouse = _ROOT / ".cache/test-wheels"
    assert tuple(wheelhouse.glob("*.whl")), (
        "Prepare .cache/test-wheels using the packaging setup commands in "
        "docs/en/development.md before running isolated installation tests"
    )
    return wheelhouse


@pytest.fixture(scope="session")
def locked_requirements(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Constrain isolated installations to the workspace's verified dependencies."""
    requirements = tmp_path_factory.mktemp("wheel-constraints") / "requirements.txt"
    _run(
        [
            "uv",
            "export",
            "--locked",
            "--all-packages",
            "--all-groups",
            "--no-emit-workspace",
            "--no-hashes",
            "--no-header",
            "--output-file",
            str(requirements),
        ]
    )
    return requirements


@pytest.fixture(scope="session")
def wheel_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build current wheels once and verify package metadata and stale-file absence."""

    output = tmp_path_factory.mktemp("wheel-matrix-dist")
    _run(
        [
            sys.executable,
            str(_ROOT / "scripts/build_wheels.py"),
            "--offline",
            "--out-dir",
            str(output),
        ]
    )
    wheels = tuple(output.glob("*.whl"))
    assert len(wheels) == len(_PROJECTS)
    for project in _PROJECTS:
        prefix = project.name.replace("-", "_") + "-"
        wheel = next(path for path in wheels if path.name.startswith(prefix))
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            metadata_name = next(
                name for name in names if name.endswith(".dist-info/METADATA")
            )
            metadata = archive.read(metadata_name).decode("utf-8")
            wheel_python_payload = _wheel_python_payload(archive)
        first_party = {canonicalize_name(item.name) for item in _PROJECTS}
        for line in metadata.splitlines():
            if line.startswith("Requires-Dist: "):
                requirement = Requirement(line.removeprefix("Requires-Dist: "))
                if canonicalize_name(requirement.name) in first_party:
                    assert str(requirement.specifier) == "==0.1.1"
        source_root = project.path / "src"
        assert wheel_python_payload == _source_python_payload(source_root)
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
        assert "Description-Content-Type: text/markdown" in metadata
        if project.marker is not None:
            assert project.marker in names
        if project.name == "tinkerfin-langgraph-store":
            assert any(name.endswith(".dist-info/licenses/NOTICE") for name in names)
            assert not any(name.startswith("langgraph/") for name in names)
            assert not any("store_migrations" in name for name in names)
        if project.name == "tinkerfin":
            assert "tinkerfin/agui_native.py" not in names
        if project.name == "tinkerfin-agui-adapter":
            assert not any("-v1" in name for name in names)
        if project.name == "tinkerfin-sandbox":
            assert "tinkerfin_sandbox/_native/_sandbox_runner.mjs" in names
            assert "tinkerfin_sandbox/_native/_process_supervisor.py" in names
            assert "tinkerfin_sandbox/_native/_file_worker.py" in names
            assert not any(name.startswith("tinkerfin_local/") for name in names)
        if project.name == "tinkerfin-desktop":
            assert "tinkerfin_desktop/browser.mjs" not in names
            for resource in (
                "browser_command.py",
                "browser_help.md",
                "browser_release.json",
            ):
                assert f"tinkerfin_desktop/{resource}" in names
    return output


def _verification_script() -> str:
    return r"""
import asyncio
import importlib
import importlib.metadata
import importlib.resources
import json
import os
import sys

case = json.loads(os.environ["TINKERFIN_WHEEL_CASE"])
for module in case["imports"]:
    importlib.import_module(module)
for name in case["required_distributions"]:
    importlib.metadata.distribution(name)
for name in case["forbidden_distributions"]:
    try:
        importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError(f"forbidden distribution installed: {name}")

async def smoke() -> None:
    if case["smoke"] == "sandbox_native":
        from tinkerfin_sandbox import (
            LocalAccess, LocalError, LocalErrorCode, LocalLimits,
            LocalProcess, LocalSession, LocalWorkspace, SandboxError,
        )
        assert issubclass(LocalError, SandboxError)
        assert LocalErrorCode.UNAVAILABLE.value.startswith("sandbox.local.")
        assert LocalAccess().network == "normal"
        assert all(isinstance(value, type) for value in (LocalLimits, LocalProcess, LocalSession))
        workspace = LocalWorkspace("/not-opened", sandbox_executable="not-installed")
        assert callable(workspace.open)
        root = importlib.resources.files("tinkerfin_sandbox").joinpath("_native")
        for name in ("_sandbox_runner.mjs", "_process_supervisor.py", "_file_worker.py"):
            assert root.joinpath(name).is_file(), name
    elif case["smoke"] in {"sandbox_opensandbox", "sandbox_sqlalchemy"}:
        from tinkerfin_sandbox import (
            InMemoryOpenSandboxState, OpenSandboxBackend, OpenSandboxClient,
            OpenSandboxConfig, OpenSandboxError, OpenSandboxManager, SandboxError,
        )
        assert issubclass(OpenSandboxError, SandboxError)
        assert all(isinstance(value, type) for value in (
            InMemoryOpenSandboxState, OpenSandboxBackend, OpenSandboxClient, OpenSandboxManager,
        ))
        assert OpenSandboxConfig().image
        root = importlib.resources.files("tinkerfin_sandbox").joinpath("backends")
        assert root.joinpath("_rooted_helper.py.txt").is_file()
        assert root.joinpath("_workspace_files_helper.py.txt").is_file()
        if case["smoke"] == "sandbox_sqlalchemy":
            from tinkerfin_sandbox import SQLAlchemyOpenSandboxState, get_sqlalchemy_opensandbox_state_schema
            assert isinstance(SQLAlchemyOpenSandboxState, type)
            assert get_sqlalchemy_opensandbox_state_schema(dialect="sqlite").table_names
    elif case["smoke"] == "companion":
        root = importlib.resources.files("tinkerfin_desktop")
        assert not root.joinpath("browser.mjs").is_file()
        for name in ("browser_command.py", "browser_help.md", "browser_release.json"):
            assert root.joinpath(name).is_file(), name
    elif case["smoke"] == "notifications":
        from tinkerfin_notifications import Notification, Notifications, NotificationScope
        async with Notifications() as service:
            scope = NotificationScope("installed")
            async with service.subscribe(scope=scope) as changes:
                await service.publish(Notification(scope=scope, topic="task.changed", key="task"))
                assert (await anext(changes)).key == "task"
    elif case["smoke"] in {"gateway", "gateway_starlette"}:
        from tinkerfin_gateway import Gateway
        from tinkerfin_messaging import Messaging
        from tinkerfin_notifications import Notifications, NotificationScope
        async with Messaging() as messaging, Notifications() as notifications:
            gateway = Gateway(messaging=messaging, notifications=notifications)
            stream = await gateway.notifications(scopes=[NotificationScope("installed")])
            if case["smoke"] == "gateway_starlette":
                from tinkerfin_gateway.starlette import sse_response
                response = await sse_response(stream)
                await response.aclose()
            else:
                async with stream:
                    frames = stream.to_sse()
                    try:
                        assert await anext(frames) == b"event: ready\ndata: {}\n\n"
                    finally:
                        await frames.aclose()
    elif case["smoke"] == "automation_core":
        from tinkerfin_automation import (
            Automation,
            TinkerFinTarget,
            create_automation_tools,
        )
        automation = Automation(namespace="wheel-matrix")
        try:
            tools = create_automation_tools(
                automation.for_owner("wheel-owner"),
                allowed_targets={"summary"},
            )
            assert len(tools) == 9
            assert {tool.name for tool in tools} >= {
                "execute_automation_once",
                "run_automation_task_now",
            }
            assert TinkerFinTarget is not None
        finally:
            await automation.aclose()
    elif case["smoke"] == "automation_gateway":
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver
        from tinkerfin import TinkerFin
        from tinkerfin_automation import Automation, ExecutionStatus, TinkerFinTarget
        from tinkerfin_gateway import Gateway
        from tinkerfin_messaging import Messaging
        from tinkerfin_notifications import Notifications

        class Model(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        runtime = TinkerFin(checkpointer=InMemorySaver()).with_namespace("installed").build(
            Model(responses=[AIMessage(content="installed conversation")])
        )
        async with Messaging() as messaging, Notifications() as notifications:
            gateway = Gateway(messaging=messaging, notifications=notifications)
            automation = Automation(namespace="wheel-matrix")
            automation.target("conversation", TinkerFinTarget(runtime, gateway=gateway))
            try:
                async with automation.worker() as worker:
                    run = await automation.for_owner("wheel-owner").run(
                        "conversation", input={"messages": [{"role": "user", "content": "hello"}]}
                    )
                    await worker.wait_until_idle()
                    await run.refresh()
                    assert run.status == ExecutionStatus.SUCCEEDED, run.snapshot.failure_code
            finally:
                await automation.aclose()
    elif case["smoke"] == "automation_sqlite":
        from sqlalchemy.ext.asyncio import create_async_engine
        from tinkerfin_automation import SqlAlchemyAutomationStore
        from tinkerfin_automation.service import AutomationService
        from sqlalchemy.pool import AsyncAdaptedQueuePool
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", poolclass=AsyncAdaptedQueuePool, pool_size=1, max_overflow=0
        )
        store = SqlAlchemyAutomationStore(engine)
        service = AutomationService(namespace="wheel-matrix", store=store)
        try:
            execution = await service.execute_once(
                owner_id="wheel-owner",
                target="summary",
            )
            assert execution.task_id is None
        finally:
            await service.close()
            await store.close()
            await engine.dispose()

asyncio.run(smoke())
for root in case["forbidden_modules"]:
    if any(name == root or name.startswith(root + ".") for name in sys.modules):
        raise AssertionError(f"forbidden module loaded: {root}")
"""


@pytest.mark.packaging_e2e
@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_isolated_wheel_installation(
    case: _InstallCase,
    wheel_directory: Path,
    dependency_wheels: Path,
    locked_requirements: Path,
    tmp_path: Path,
) -> None:
    """Resolve, import, and smoke-test one public installation combination."""

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["TINKERFIN_WHEEL_CASE"] = json.dumps(
        {
            "imports": case.imports,
            "required_distributions": case.required_distributions,
            "forbidden_distributions": case.forbidden_distributions,
            "forbidden_modules": case.forbidden_modules,
            "smoke": case.smoke,
        }
    )
    venv = tmp_path / "venv"
    _run(["uv", "venv", "--python", sys.executable, str(venv)])
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--offline",
            "--no-index",
            "--find-links",
            str(dependency_wheels),
            "--constraint",
            str(locked_requirements),
            "--find-links",
            str(wheel_directory),
            case.spec,
        ],
        environment=environment,
    )
    _run(["uv", "pip", "check", "--python", str(python)], environment=environment)
    _run(
        [str(python), "-I", "-c", _verification_script()],
        environment=environment,
    )


@pytest.mark.packaging_e2e
def test_studio_candidate_wheel_set_is_self_contained(
    wheel_directory: Path,
    dependency_wheels: Path,
    tmp_path: Path,
) -> None:
    """Install the candidate source wheels after external locked dependencies."""

    expected_paths = tuple(
        str(project.path.relative_to(_ROOT)) for project in _PROJECTS
    )
    assert PROJECT_PATHS == expected_paths
    release_paths = project_paths_for("tinkerfin-studio")
    assert "packages/tinkerfin-remote" in release_paths
    assert "apps/studio-server" in release_paths
    assert "apps/desktop" not in release_paths
    assert "packages/tinkerfin-sandbox" in release_paths

    requirements = tmp_path / "requirements.txt"
    _run(
        [
            "uv",
            "export",
            "--quiet",
            "--frozen",
            "--package",
            "tinkerfin-studio",
            "--no-dev",
            "--no-emit-workspace",
            "--no-header",
            "--format",
            "requirements.txt",
            "--output-file",
            str(requirements),
        ]
    )

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    venv = tmp_path / "deploy-venv"
    _run(["uv", "venv", "--python", sys.executable, str(venv)])
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--offline",
            "--no-index",
            "--find-links",
            str(dependency_wheels),
            "--require-hashes",
            "-r",
            str(requirements),
        ],
        environment=environment,
    )

    projects_by_path = {
        str(project.path.relative_to(_ROOT)): project for project in _PROJECTS
    }
    release_wheels: list[str] = []
    for project_path in release_paths:
        project = projects_by_path[project_path]
        prefix = project.name.replace("-", "_") + "-"
        wheel = next(
            path
            for path in wheel_directory.glob("*.whl")
            if path.name.startswith(prefix)
        )
        release_wheels.append(str(wheel))
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            *release_wheels,
        ],
        environment=environment,
    )
    _run(["uv", "pip", "check", "--python", str(python)], environment=environment)
    _run(
        [str(python), "-I", "-c", "import tinkerfin_studio.application"],
        environment=environment,
    )
