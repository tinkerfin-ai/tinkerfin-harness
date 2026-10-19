"""Select skill directories supplied by a backend or prepared workspace."""

from dataclasses import dataclass

from tinkerfin_contracts.skills import SkillSource as SkillSource


@dataclass(frozen=True, slots=True)
class WorkspaceSkills:
    """Select skills from directories supplied by the prepared workspace.

    Args:
        names: Exact skill names to expose, or ``None`` for every skill allowed
            by the provider. An empty tuple exposes none. Provider restrictions
            still apply; this declaration never widens filesystem permissions.

    Use ``skills=[WorkspaceSkills(names=("research",))]`` on a declared subagent
    whose workspace directories are known only after preparation. The main and
    automatic general-purpose agents already discover prepared workspace sources.
    """

    names: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """Reject duplicate names and paths in an explicit skill selection."""
        if self.names is not None:
            if not isinstance(self.names, tuple) or any(
                not isinstance(name, str) or not name or "/" in name or "\\" in name
                for name in self.names
            ):
                raise ValueError("skill names must be a tuple of nonempty names")
            if len(set(self.names)) != len(self.names):
                raise ValueError("skill names must be unique within a selection")


__all__ = ["SkillSource", "WorkspaceSkills"]
