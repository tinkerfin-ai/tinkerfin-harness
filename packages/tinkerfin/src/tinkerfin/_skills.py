"""Keep skill discovery bound to the configured source directories."""

from typing import Annotated, Any, NotRequired, cast

from deepagents.middleware.skills import (
    SkillsMiddleware,
    SkillsState,
    SkillsStateUpdate,
)
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime


class _SourceState(SkillsState):
    _tinkerfin_skill_sources: NotRequired[Annotated[list[str], PrivateStateAttr]]


class _SourceUpdate(SkillsStateUpdate):
    _tinkerfin_skill_sources: list[str]


class SourceSkillsMiddleware(SkillsMiddleware):
    """Reload discovery when a rebuilt agent uses different source paths.

    Checkpoints retain the source list alongside the SDK metadata. Unchanged
    paths retain the SDK cache, including an empty result. Hosts can use immutable
    content directories without reading or resetting private checkpoint fields.
    """

    state_schema = _SourceState

    @property
    def name(self) -> str:
        """Retain the SDK middleware replacement slot."""
        return "SkillsMiddleware"

    async def abefore_agent(
        self, state: SkillsState, runtime: Runtime[Any], config: RunnableConfig
    ) -> _SourceUpdate | None:
        """Refresh changed sources before exposing their metadata to the model."""
        current = cast(_SourceState, state)
        if current.get("_tinkerfin_skill_sources") != self.sources:
            state = {**state, "skills_metadata": None}
        update = await super().abefore_agent(state, runtime, config)
        if update is None:
            return None
        return {**update, "_tinkerfin_skill_sources": list(self.sources)}
