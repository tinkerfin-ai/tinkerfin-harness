"""Own native execution scopes for applications and Runtime runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import sysconfig
import tempfile
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, TypeVar
from uuid import uuid4

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import BackendProtocol
from pydantic import BaseModel, JsonValue

from tinkerfin_contracts import PreparedWorkspace, RunIdentity

from ._environment import PAYLOAD_KEY
from ._process import (
    ProcessOwner,
    communicate,
    finish_owned,
    raise_cleanup_failures,
    safe_environment,
    start_process,
)
from ._seatbelt import FilePolicy, IsolatedTree, profile
from .errors import LocalError, LocalErrorCode
from .policy import LocalAccess, LocalLimits
from .session import LocalSession

_Value = TypeVar("_Value")


class _SandboxPackage(BaseModel):
    name: Literal["@anthropic-ai/sandbox-runtime"]
    version: Literal["0.0.79"]


def _sandbox_module(executable: str) -> Path:
    selected = Path(executable)
    module = selected.with_name("index.js")
    if (
        selected.name != "cli.js"
        or selected.parent.name != "dist"
        or not module.is_file()
    ):
        raise ValueError("sandbox_executable must identify the installed SRT CLI entry")
    with (selected.parent.parent / "package.json").open("rb") as stream:
        metadata = stream.read(64 * 1024 + 1)
    if len(metadata) > 64 * 1024:
        raise ValueError("sandbox package metadata exceeds its limit")
    _SandboxPackage.model_validate_json(metadata)
    return module


def _paths(paths: tuple[Path, ...]) -> list[str]:
    return [str(path.expanduser().resolve(strict=True)) for path in paths]


def _isolated_trees(directory: Path, access: LocalAccess) -> tuple[IsolatedTree, ...]:
    explicit_reads = tuple(Path(path) for path in _paths(access.read_directories))
    trees: list[IsolatedTree] = []
    for selected in access.isolated_roots:
        root = selected.expanduser().resolve(strict=True)
        if root == Path("/") or root == directory or not root.is_dir():
            raise ValueError(
                "isolated roots must be non-root directories distinct from the working directory"
            )
        working = directory if directory.is_relative_to(root) else None
        # Broad grants and implicit runtime/write grants must not expose a
        # whole isolated tree. Only explicit, strict descendant reads qualify.
        readable = {
            path
            for path in explicit_reads
            if path != root and path.is_relative_to(root)
        }
        if any(not path.is_dir() for path in readable):
            raise ValueError("isolated read grants must identify directories")
        if working is not None:
            readable.add(working)
        trees.append(IsolatedTree(root, tuple(sorted(readable)), working))
    return tuple(trees)


def _trusted_installation(executable: str) -> tuple[str, tuple[Path, ...]]:
    selected = Path(executable).resolve(strict=True)
    protected = {
        Path(executable).parent,
        selected.parent,
        Path(sys.executable).parent,
        *(
            Path(path)
            for path in safe_environment(str(selected))["PATH"].split(os.pathsep)
        ),
    }
    protected = {path for path in protected if path.exists()}
    # Workloads cannot modify the launcher, its npm dependency tree, or an
    # enclosing application bundle before the next trusted sandbox activation.
    for program in (selected, Path(sys.executable).resolve(strict=True)):
        for parent in program.parents:
            if parent.name == "node_modules" or (
                parent.suffix == ".app" and parent / "Contents" in program.parents
            ):
                protected.add(parent)
    return str(selected), tuple(sorted(protected))


def _file_policy(
    directory: Path,
    home: Path,
    metadata: Path,
    access: LocalAccess,
    installation: tuple[Path, ...] = (),
    commands: tuple[Path, ...] = (),
) -> FilePolicy:
    runtime_paths = [sys.prefix, sys.base_prefix]
    runtime_paths.extend(
        path
        for path in sys.path
        if path and (path.endswith("/src") or "site-packages" in path)
    )
    # Native loaders resolve libraries beside the canonical interpreter. An
    # interpreter-manager alias is not a sufficient grant for those real paths.
    runtime_paths = [str(Path(path).resolve(strict=True)) for path in runtime_paths]
    # System libraries load locale and language data from /usr/share as well
    # as /System; these OS-owned resources are readable, never writable grants.
    reads = [
        str(directory),
        str(home),
        "/System",
        "/usr/lib",
        "/usr/share",
        "/usr/bin",
        "/bin",
        "/sbin",
        "/usr/sbin",
        "/Library/Apple",
        "/Library/Frameworks",
        "/private/etc",
        "/private/var/select",
        "/dev/null",
        "/dev/urandom",
        "/dev/random",
        *runtime_paths,
        *_paths(access.read_directories),
        *_paths(access.write_directories),
        *_paths(commands),
    ]
    protected = [str(path.expanduser().resolve()) for path in access.protected_paths]
    if access.network == "restricted":
        protected.extend(
            (
                str(Path.home() / ".ssh"),
                str(Path.home() / ".aws"),
                str(Path.home() / "Library/Keychains"),
            )
        )
    protected.append(str(metadata))
    return FilePolicy(
        restrict_reads=access.filesystem == "workspace",
        readable=tuple(Path(path) for path in sorted(set(reads))),
        writable=tuple(
            Path(path)
            for path in (
                "/" if access.filesystem == "user" else str(directory),
                str(home),
                *_paths(access.write_directories),
            )
        ),
        protected=tuple(Path(path) for path in protected),
        read_only=tuple(
            Path(path)
            for path in (
                *runtime_paths,
                *_paths(access.read_only_paths),
                *_paths(installation),
                str(home / "bin"),
                str(home / "lib"),
                str(home / "pyvenv.cfg"),
            )
        ),
    )


def _restricted_policy(
    files: FilePolicy, home: Path, access: LocalAccess
) -> dict[str, JsonValue]:
    unix_paths = [path.expanduser().resolve() for path in access.unix_socket_paths]
    if Path("/") in unix_paths:
        raise ValueError("unix_socket_paths cannot grant the filesystem root")
    return {
        "filesystem": {
            "denyRead": [
                {"path": str(path), "literal": True}
                for path in [
                    *([Path("/")] if files.restrict_reads else []),
                    *files.protected,
                ]
            ],
            "allowRead": [
                {"path": str(path), "literal": True} for path in files.readable
            ],
            "allowWrite": [
                {"path": str(path), "literal": True} for path in files.writable
            ],
            "denyWrite": [
                {"path": str(path), "literal": True}
                for path in (*files.protected, *files.read_only)
            ],
        },
        "network": {
            "allowedDomains": list(access.allowed_domains),
            "strictAllowlist": True,
            "allowLocalBinding": False,
            "deniedDomains": [],
            "allowAllUnixSockets": False,
            "allowUnixSockets": [
                *([str(home)] if access.graphics else []),
                *(str(path) for path in unix_paths),
            ],
            "allowMachLookup": ["com.apple.windowserver.active"]
            if access.graphics
            else [],
            "deniedResolvedAddresses": [
                "0.0.0.0/8",
                "10.0.0.0/8",
                "100.64.0.0/10",
                "127.0.0.0/8",
                "169.254.0.0/16",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "::1/128",
                "fc00::/7",
                "fe80::/10",
            ],
        },
    }


class _LocalBorrow:
    """Own admission before its first disk await, through final temporary cleanup."""

    def __init__(self, workspace: LocalWorkspace) -> None:
        self.workspace = workspace
        self.allocated: list[Path] = []
        self.session: LocalSession | None = None
        self.opening = asyncio.create_task(self._open())
        self._closing: asyncio.Task[None] | None = None

    async def _open(self) -> LocalSession:
        try:
            return await self.workspace._prepare_session(self)
        except OSError as error:
            raise LocalError(
                LocalErrorCode.UNAVAILABLE,
                "Local execution resources could not be prepared",
                cause=error,
            ) from error

    async def aclose(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await finish_owned(self._closing)

    async def _close(self) -> None:
        # Preparation errors belong to the open caller. Waiting here also settles
        # non-cancellable thread work before inspecting allocated directories.
        await asyncio.gather(self.opening, return_exceptions=True)
        failures: list[BaseException] = []
        if self.session is not None:
            try:
                await self.session.aclose()
            except BaseException as error:  # noqa: BLE001 - release every owned resource
                failures.append(error)
        for path in self.allocated:
            try:
                await self.workspace._disk(lambda path=path: shutil.rmtree(path))
            except FileNotFoundError:
                pass
            except OSError as error:
                failures.append(error)
        raise_cleanup_failures(failures)


class LocalWorkspace:
    """Provide native file and command access with a default working directory.

    Args:
        directory: Existing default directory for relative paths and commands.
        access: Filesystem reach, protected paths, and outbound-domain grants.
        limits: Positive execution and transfer bounds.
        sandbox_executable: SRT 0.0.79 CLI for restricted network access, supplied
            by the application or discovered as srt on PATH. Normal access uses
            macOS sandbox-exec and does not inspect this selection or need Node.
        environment: Explicit workload variables. Host environment and credentials
            are not inherited. HOME and TMPDIR are managed by the workspace.
            Do not grant provider keys to untrusted commands.
        commands: Additional command names bound to trusted executables, such as
            node and npm. Grant required libraries with access.read_directories.
        on_command_abort: Optional bounded cleanup for external services owned by
            the host. aexecute and aexecute_stream await it after their process
            group stops exceptionally, including cancellation. Normal results,
            nonzero exits, file operations and open_process do not invoke it.
            Cleanup may run concurrently for overlapping command failures; use
            an independent service owner rather than the same callback-bound
            session for cleanup commands. Ownership of that service stays with
            the host, including ordinary workspace closure.

    Open this object for the application lifetime and pass it as Runtime backend.
    Each admitted run gets an independent LocalSession. Its Python commands use
    the host's installed packages through a private, read-only environment;
    install task dependencies in a virtual environment inside the workspace.
    The workspace owns only its process groups and private temporary directories, never the
    authorized directory or user files. Closing is permanent and idempotent.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        access: LocalAccess | None = None,
        limits: LocalLimits | None = None,
        sandbox_executable: str | Path = "srt",
        environment: Mapping[str, str] | None = None,
        commands: Mapping[str, str | Path] | None = None,
        on_command_abort: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Capture host authorization without filesystem or process I/O."""
        self._directory = Path(directory)
        self._user_home = Path.home()
        self._access = access or LocalAccess()
        self._limits = limits or LocalLimits()
        self._sandbox_selection = str(sandbox_executable)
        self._environment = dict(environment or {})
        self._on_command_abort = on_command_abort
        managed = {
            "HOME",
            "TMPDIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "FTP_PROXY",
            "NO_PROXY",
            PAYLOAD_KEY,
        }
        if managed.intersection(name.upper() for name in self._environment):
            raise ValueError(
                "home, temporary, proxy and launcher variables are owned by the local workspace"
            )
        self._command_selection = dict(commands or {})
        reserved = {
            "python",
            "python3",
            f"python{sys.version_info.major}.{sys.version_info.minor}",
        }
        if len(self._command_selection) > 32 or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", name) is None
            or name in reserved
            for name in self._command_selection
        ):
            raise ValueError(
                "command names must be distinct from the managed Python commands"
            )
        self._commands: dict[str, Path] = {}
        self._command_prefix: tuple[str, ...] | None = None
        self._root: Path | None = None
        self._python: Path | None = None
        self._installation: tuple[Path, ...] = ()
        self._borrows: set[_LocalBorrow] = set()
        self._opening: asyncio.Task[Self] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._opened = False
        self._closed = False
        self._io_slots = asyncio.Semaphore(8)

    async def _disk(self, operation: Callable[[], _Value]) -> _Value:
        # Filesystem metadata has no native async API. Each workspace permits
        # eight executor operations; cancellation waits for their settlement.
        async with self._io_slots:
            return await finish_owned(asyncio.to_thread(operation))

    async def __aenter__(self) -> Self:
        """Validate the platform and grants while retaining startup cleanup ownership."""
        if self._opening is not None or self._closed:
            raise LocalError(LocalErrorCode.CLOSED, "LocalWorkspace cannot be reopened")
        self._opening = asyncio.create_task(self._initialize())
        try:
            return await finish_owned(self._opening)
        except BaseException:
            await self.aclose()
            raise

    async def _initialize(self) -> Self:

        if self._opened or self._closed:
            raise LocalError(LocalErrorCode.CLOSED, "LocalWorkspace cannot be reopened")
        if sys.platform != "darwin":
            raise LocalError(
                LocalErrorCode.UNAVAILABLE, "This local runtime requires macOS"
            )
        root = await self._disk(
            lambda: self._directory.expanduser().resolve(strict=True)
        )
        if not await self._disk(root.is_dir) or root == Path("/"):
            raise ValueError("directory must identify an existing non-root directory")
        selection = (
            self._sandbox_selection
            if self._access.network == "restricted"
            else "/usr/bin/sandbox-exec"
        )
        executable = await self._disk(lambda: shutil.which(selection))
        if executable is None:
            raise LocalError(
                LocalErrorCode.UNAVAILABLE,
                "The native sandbox runtime is not installed",
            )
        executable, self._installation = await self._disk(
            lambda executable=executable: _trusted_installation(executable)
        )
        for name, selected in self._command_selection.items():
            command = await self._disk(
                lambda selected=selected: (
                    Path(selected).expanduser().resolve(strict=True)
                )
            )
            if not await self._disk(
                lambda command=command: (
                    command.is_file() and os.access(command, os.X_OK)
                )
            ):
                raise ValueError("bound commands must be executable files")
            self._commands[name] = command
            _path, protected = await self._disk(
                lambda command=command: _trusted_installation(str(command))
            )
            self._installation = tuple(sorted(set((*self._installation, *protected))))
        if self._access.network == "restricted":
            module = await self._disk(lambda: _sandbox_module(executable))
            node = self._commands.get("node")
            if node is None:
                selected_node = await self._disk(
                    lambda: shutil.which(
                        "node", path=safe_environment(executable)["PATH"]
                    )
                )
                if selected_node is None:
                    raise LocalError(
                        LocalErrorCode.UNAVAILABLE,
                        "The restricted sandbox requires an installed Node runtime",
                    )
                node = await self._disk(
                    lambda: Path(selected_node).resolve(strict=True)
                )
            _path, protected = await self._disk(
                lambda: _trusted_installation(str(node))
            )
            self._installation = tuple(sorted(set((*self._installation, *protected))))
            opening = asyncio.create_task(
                start_process(
                    (str(node), executable, "--version"),
                    env=safe_environment(executable),
                    output_limit=4096,
                )
            )
            owner = ProcessOwner(opening)
            try:
                await asyncio.wait({opening})
                check = opening.result()
                async with asyncio.timeout(10):
                    result = await communicate(check, content=None, output_limit=4096)
                stdout = result.stdout
            finally:
                await finish_owned(owner.close())
            if check.returncode != 0 or stdout.strip() != b"0.0.79":
                raise LocalError(
                    LocalErrorCode.UNAVAILABLE,
                    "The installed sandbox runtime does not match the application",
                )
            prefix = (
                str(node),
                str(Path(__file__).with_name("_sandbox_runner.mjs")),
                "--module",
                str(module),
                "--policy",
            )
        else:
            prefix = (executable, "-f")
        self._python = await self._disk(
            lambda: Path(sys.executable).resolve(strict=True)
        )
        if self._closed:
            raise LocalError(
                LocalErrorCode.CLOSED, "LocalWorkspace closed during startup"
            )
        self._root, self._command_prefix = root, prefix
        self._opened = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Revoke active borrows and retain cleanup through cancellation."""
        await self.aclose()

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[LocalSession, None]:
        """Borrow native files and commands independently of a Runtime Run.

        The manager owns preparation, private directories and all processes. Its
        close also revokes active borrows and joins preparations already admitted.
        Exiting this context releases only its own resources, never user files.

        Yields:
            A revocable session bound to this workspace's grants.

        Raises:
            LocalError: The manager is closed, startup fails or cleanup fails.
        """
        if not self._opened or self._closed:
            raise LocalError(LocalErrorCode.CLOSED, "LocalWorkspace is not open")
        borrow = _LocalBorrow(self)
        self._borrows.add(borrow)
        try:
            await asyncio.wait({borrow.opening})
            session = borrow.opening.result()
            if self._closed:
                raise LocalError(
                    LocalErrorCode.CLOSED, "LocalWorkspace closed during admission"
                )
            yield session
        finally:
            try:
                await borrow.aclose()
            finally:
                self._borrows.discard(borrow)

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncGenerator[PreparedWorkspace[LocalSession, BackendProtocol]]:
        """Prepare native tools and a thread-scoped artifact root for one Run.

        Args:
            identity: Actual Runtime identity; it does not grant filesystem access.

        Yields:
            Prepared tools whose session expires on exit or manager close.

        Raises:
            TypeError: The identity is not a RunIdentity.
            LocalError: Preparation or cleanup fails, or the manager is closed.
        """
        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        async with self.open() as session:
            # Deep Agents 0.7.19 CompositeBackend.artifacts_root is shared by
            # offloaded tool output and history. It belongs to the thread's files,
            # while the independent session owns processes and temporary resources.
            artifact_key = hashlib.sha256(
                json.dumps([identity.namespace, identity.thread_id]).encode()
            ).hexdigest()
            yield PreparedWorkspace(
                workspace=session,
                backend=CompositeBackend(
                    default=session,
                    routes={},
                    artifacts_root=str(
                        session.directory / ".tinkerfin/artifacts" / artifact_key
                    ),
                ),
                file_path_resolver=session.resolve_path,
                filesystem_instructions=(
                    f"The default working directory is {json.dumps(str(session.directory))}. "
                    "Relative file paths and commands start there. Absolute paths address the native filesystem; "
                    f"~/ refers to the user's home {json.dumps(str(self._user_home))}. "
                    "Filesystem, network, and process restrictions are enforced by the native sandbox. "
                    "A tool approval does not expand those permissions."
                ),
            )

    async def _prepare_session(self, borrow: _LocalBorrow) -> LocalSession:
        if (
            not self._opened
            or self._closed
            or self._root is None
            or self._command_prefix is None
            or self._python is None
        ):
            raise LocalError(LocalErrorCode.CLOSED, "LocalWorkspace is not open")
        directory, python = self._root, self._python
        isolated = await self._disk(lambda: _isolated_trees(directory, self._access))
        allocated = borrow.allocated

        def allocate() -> tuple[Path, Path]:
            metadata = Path(
                tempfile.mkdtemp(prefix="tinkerfin-native-policy-")
            ).resolve()
            allocated.append(metadata)
            home = Path(tempfile.mkdtemp(prefix="tinkerfin-native-home-")).resolve()
            allocated.append(home)
            commands = home / "bin"
            commands.mkdir(mode=0o700)
            # CPython discovers this private environment beside its command
            # links. Canonical home avoids interpreter-manager aliases, while
            # the site directory retains installed packages and editable .pth
            # entries without contaminating task-created virtual environments.
            (home / "pyvenv.cfg").write_text(
                f"home = {python.parent}\ninclude-system-site-packages = false\n",
                encoding="utf-8",
            )
            site = Path(
                sysconfig.get_path(
                    "purelib", vars={"base": str(home), "platbase": str(home)}
                )
            )
            site.parent.mkdir(parents=True)
            site.symlink_to(Path(sysconfig.get_path("purelib")).resolve(strict=True))
            for name in (
                "python",
                "python3",
                f"python{sys.version_info.major}.{sys.version_info.minor}",
            ):
                (commands / name).symlink_to(python)
            for name, command in self._commands.items():
                (commands / name).symlink_to(command)
            return metadata, home

        metadata, home = await self._disk(allocate)
        files = await self._disk(
            lambda: _file_policy(
                directory,
                home,
                metadata,
                self._access,
                self._installation,
                tuple(self._commands.values()),
            )
        )
        if self._access.network == "restricted":
            policy_path = metadata / "sandbox.json"
            sandbox_policy = await self._disk(
                lambda: _restricted_policy(files, home, self._access)
            )
            content = json.dumps(
                {
                    "sandbox": sandbox_policy,
                    "protected": [str(path) for path in files.protected],
                    "isolation": [
                        {
                            "root": str(tree.root),
                            "read": [str(path) for path in tree.readable],
                            "write": str(tree.working_directory)
                            if tree.working_directory is not None
                            else None,
                        }
                        for tree in isolated
                    ],
                    "graphics": self._access.graphics,
                }
            )
            prefix = (*self._command_prefix, str(policy_path), "--")
        else:
            policy_path = metadata / "sandbox.sb"
            content = profile(files, isolated)
            prefix = (*self._command_prefix, str(policy_path))
        await self._disk(lambda: policy_path.write_text(content, encoding="utf-8"))
        if self._closed:
            raise LocalError(
                LocalErrorCode.CLOSED, "LocalWorkspace closed during preparation"
            )
        session = LocalSession._create(
            directory=directory,
            command_prefix=prefix,
            python=self._python,
            home=home,
            user_home=self._user_home,
            identity=uuid4().hex,
            limits=self._limits,
            environment=self._environment,
            on_command_abort=self._on_command_abort,
        )
        borrow.session = session
        return session

    async def aclose(self) -> None:
        """Join admitted preparation and release all borrows without deleting user files.

        Closing permanently rejects new borrows. Concurrent callers join the same
        cleanup; cancellation is propagated only after accepted cleanup settles.
        LocalError retains any cleanup failures as private causes.
        """
        self._closed = True
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await finish_owned(self._closing)

    async def _close(self) -> None:
        if self._opening is not None:
            await asyncio.gather(self._opening, return_exceptions=True)
        results = await asyncio.gather(
            *(borrow.aclose() for borrow in tuple(self._borrows)),
            return_exceptions=True,
        )
        raise_cleanup_failures(
            [result for result in results if isinstance(result, BaseException)]
        )
