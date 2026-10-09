"""Declare directories whose current skills an agent may discover."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SkillSource:
    """Select current skill packages from one backend directory.

    Args:
        directory: Backend directory containing named skill directories.
        names: Exact names to expose, or ``None`` for all discovered skills.
            An empty tuple exposes none. This selects instructions, not filesystem
            permissions; file and execution access remain the backend's concern.

    Raises:
        ValueError: The directory is empty, or names are invalid or repeated.
    """

    directory: str
    names: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """Reject empty paths and invalid or mutable name selections."""
        if not isinstance(self.directory, str) or not self.directory.strip():
            raise ValueError("skill source directory must be nonempty")
        if self.names is not None:
            if not isinstance(self.names, tuple) or any(
                not isinstance(name, str) or not name or "/" in name or "\\" in name
                for name in self.names
            ):
                raise ValueError("skill names must be a tuple of nonempty names")
            if len(set(self.names)) != len(self.names):
                raise ValueError("skill names must be unique within a source")
