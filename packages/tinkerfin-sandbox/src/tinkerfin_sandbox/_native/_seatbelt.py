"""Compile native path boundaries while preserving ordinary macOS services."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FilePolicy:
    """Canonical file grants shared by native and restricted process launches."""

    restrict_reads: bool
    readable: tuple[Path, ...]
    writable: tuple[Path, ...]
    protected: tuple[Path, ...]
    read_only: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class IsolatedTree:
    """Canonical read exceptions and the sole writable, connectable subtree."""

    root: Path
    readable: tuple[Path, ...]
    working_directory: Path | None


def _path(value: Path) -> str:
    if not value.is_absolute():
        raise ValueError("sandbox paths must be absolute")
    return json.dumps(str(value), ensure_ascii=False)


def _outside(root: Path, exceptions: tuple[Path, ...]) -> str:
    subtree = f"(subpath {_path(root)})"
    if not exceptions:
        return subtree
    allowed = " ".join(f"(subpath {_path(path)})" for path in exceptions)
    if len(exceptions) > 1:
        allowed = f"(require-any {allowed})"
    return f"(require-all {subtree} (require-not {allowed}))"


def profile(files: FilePolicy, isolated: tuple[IsolatedTree, ...]) -> str:
    """Enforce file and Unix-socket access and preserve the process supervisor.

    Allowing ordinary macOS services does not grant a workload control over the
    host or the group anchor. Signal and task-port operations remain confined
    to processes sharing its sandbox, as required by _process_supervisor.main.
    Final file denies override broad grants. Literal ancestor pins prevent
    rename from moving protected or read-only contents outside their rules.
    File rules do not govern Unix connections. Matching network rules protect
    those endpoints through aliases, including short aliases for long paths,
    as verified by test_unix_socket_protection.
    """
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny signal)",
        "(allow signal (target same-sandbox))",
        "(deny mach-priv-task-port)",
        "(allow mach-priv-task-port (target same-sandbox))",
    ]
    if files.restrict_reads:
        rules.extend(
            (
                "(deny file-read*)",
                '(allow file-read* (literal "/"))',
                "(allow file-read-metadata (vnode-type DIRECTORY))",
            )
        )
        rules.extend(
            f"(allow file-read* (subpath {_path(path)}))" for path in files.readable
        )
    if Path("/") not in files.writable:
        rules.append("(deny file-write*)")
        rules.extend(
            f"(allow file-write* (subpath {_path(path)}))" for path in files.writable
        )
        rules.append('(allow file-write* (literal "/dev/null") (literal "/dev/zero"))')

    pinned: set[Path] = set()
    traversable: set[Path] = set()
    for tree in isolated:
        writable = (tree.working_directory,) if tree.working_directory else ()
        # Read grants cannot release writes or Unix connections. Every nested
        # root contributes its own deny, including roots with no exceptions.
        rules.append(
            f"(deny file-read* file-read-metadata {_outside(tree.root, tree.readable)})"
        )
        rules.append(
            "(deny file-write* file-write-create file-write-unlink "
            f"{_outside(tree.root, writable)})"
        )
        rules.append(
            "(deny network-outbound (remote unix-socket "
            f"{_outside(tree.root, writable)}))"
        )
        for allowed in tree.readable:
            traversable.update(
                parent for parent in allowed.parents if parent.is_relative_to(tree.root)
            )
            pinned.update((allowed, *allowed.parents))
        pinned.update((tree.root, *tree.root.parents))

    rules.extend(
        f"(allow file-read-metadata (literal {_path(path)}))"
        for path in sorted(traversable)
        if not any(path.is_relative_to(protected) for protected in files.protected)
    )

    for path in files.protected:
        rules.append(
            "(deny file-read* file-read-metadata file-write* file-write-create "
            f"file-write-unlink (subpath {_path(path)}))"
        )
        rules.append(
            f"(deny network-outbound (remote unix-socket (subpath {_path(path)})))"
        )
        pinned.update(path.parents)
    for path in files.read_only:
        rules.append(
            "(deny file-write* file-write-create file-write-unlink "
            f"(subpath {_path(path)}))"
        )
        pinned.update(path.parents)
    pinned.discard(Path("/"))
    rules.extend(
        f"(deny file-write-create file-write-unlink (literal {_path(path)}))"
        for path in sorted(pinned)
    )
    return "\n".join(rules) + "\n"
