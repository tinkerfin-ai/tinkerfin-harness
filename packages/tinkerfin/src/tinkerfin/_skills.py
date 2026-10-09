"""Discover current skill instructions before each model step."""

from collections.abc import Sequence
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.skills import (
    SKILLS_SYSTEM_PROMPT,
    SkillMetadata,
    SkillsMiddleware,
    SkillsState,
)
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from .skills import SkillSource


class SourceSkillsMiddleware(SkillsMiddleware):
    """Refresh mutable directories without reusing checkpointed discovery.

    Each source is filtered before later sources replace names. Upstream public
    middleware hooks retain parsing, warning and prompt semantics. Readers are
    immutable per declaration, so concurrent runs never change shared sources.
    """

    def __init__(
        self,
        *,
        backend: BackendProtocol,
        sources: Sequence[SkillSource],
        system_prompt: str | None = SKILLS_SYSTEM_PROMPT,
        labels: Sequence[str] | None = None,
    ) -> None:
        configured = tuple(sources)
        if any(not isinstance(source, SkillSource) for source in configured):
            raise TypeError("skills must contain SkillSource declarations")
        native = (
            [source.directory for source in configured]
            if labels is None
            else list(
                zip((source.directory for source in configured), labels, strict=True)
            )
        )
        super().__init__(backend=backend, sources=native, system_prompt=system_prompt)
        self._readers = tuple(
            (source, SkillsMiddleware(backend=backend, sources=[source.directory]))
            for source in configured
            if source.names != ()
        )

    @property
    def name(self) -> str:
        """Retain the SDK middleware replacement slot."""
        return "SkillsMiddleware"

    def before_agent(
        self, state: SkillsState, runtime: Runtime[Any], config: RunnableConfig
    ) -> None:
        """Leave discovery to the model step, including restored conversations."""

    async def abefore_agent(
        self, state: SkillsState, runtime: Runtime[Any], config: RunnableConfig
    ) -> None:
        """Leave discovery to the model step, including restored conversations."""

    async def abefore_model(
        self, state: SkillsState, runtime: Runtime[Any]
    ) -> dict[str, list[SkillMetadata] | list[str]]:
        """Read current metadata so preceding file tools can change instructions."""
        skills: dict[str, SkillMetadata] = {}
        errors: list[str] = []
        for source, reader in self._readers:
            update = await reader.abefore_agent(
                {**state, "skills_metadata": None}, runtime, {}
            )
            assert update is not None
            for skill in update["skills_metadata"]:
                if source.names is None or skill["name"] in source.names:
                    skills[skill["name"]] = skill
            errors.extend(update.get("skills_load_errors", []))
        return {"skills_metadata": list(skills.values()), "skills_load_errors": errors}
