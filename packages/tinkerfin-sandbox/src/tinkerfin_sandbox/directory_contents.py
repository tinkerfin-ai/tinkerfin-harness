"""Declare complete managed inputs without opening a workspace."""

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class WorkspaceDirectoryContents:
    """Publish managed files while preserving unrelated project files.

    Args:
        path: Virtual absolute directory, never the workspace root itself.
        files: Unique relative paths and immutable file bytes. Reusing the same
            content preserves local edits; changed managed files conflict with
            local modifications instead of silently overwriting them.

    Raises:
        TypeError: Files are mutable or their content is not bytes.
        ValueError: Paths are invalid, duplicated, or exceed the capacity limits.
    """

    path: str
    files: tuple[tuple[str, bytes], ...]

    def __post_init__(self) -> None:
        """Reject file declarations that cannot be safely published."""
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError("directory must be a virtual absolute path")
        _relative(self.path[1:])
        if not isinstance(self.files, tuple):
            raise TypeError("managed files must be an immutable tuple")
        names: set[str] = set()
        nodes: set[str] = set()
        size = 0
        for entry in self.files:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise TypeError(
                    "managed file entries must be immutable path-content pairs"
                )
            path, content = entry
            _relative(path)
            if path in names:
                raise ValueError("managed file paths must be unique")
            if not isinstance(content, bytes):
                raise TypeError("managed file content must be bytes")
            names.add(path)
            nodes.add(path)
            nodes.update(
                str(parent)
                for parent in PurePosixPath(path).parents
                if parent != PurePosixPath(".")
            )
            if len(nodes) > 16384:
                raise ValueError("managed directory exceeds its entry capacity")
            size += len(content)
        if len(names) > 8192 or size > 512 * 1024 * 1024:
            raise ValueError("managed directory exceeds its capacity")


def _relative(path: str) -> None:
    if not isinstance(path, str) or not path or len(path) > 4096:
        raise ValueError("file path must be nonempty and bounded")
    if (
        PurePosixPath(path).is_absolute()
        or any(part in ("", ".", "..") for part in path.split("/"))
        or "\\" in path
        or "\x00" in path
    ):
        raise ValueError("file path must remain within its directory")
