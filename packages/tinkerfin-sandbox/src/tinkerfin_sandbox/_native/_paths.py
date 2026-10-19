"""Resolve local tool paths without performing filesystem I/O."""

from pathlib import Path


def local_path(value: str, *, directory: Path, user_home: Path) -> Path:
    """Resolve relative paths from the workspace and tilde from the user's home.

    Native sandbox rules enforce permissions after the operating system resolves
    symlinks. Keep parent components intact so a symlink followed by ``..`` has
    the same target as a native command. Per-run storage does not alter ``~/``.
    """
    if not value or "\0" in value:
        raise ValueError("file path must be non-empty and contain no null bytes")
    if value == "~":
        path = user_home
    elif value.startswith("~/"):
        path = user_home / value[2:]
    elif value.startswith("~"):
        raise ValueError("use an absolute path to another user's directory")
    else:
        path = Path(value)
        if not path.is_absolute():
            path = directory / path
    return path
