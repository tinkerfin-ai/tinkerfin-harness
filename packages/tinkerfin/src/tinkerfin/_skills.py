"""Discover current skill instructions before each model step."""

from codecs import BOM_UTF8
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from deepagents.backends.protocol import BackendProtocol, FileDownloadResponse, LsResult
from deepagents.middleware.skills import (
    SKILLS_SYSTEM_PROMPT,
    SkillMetadata,
    SkillsMiddleware,
    SkillsState,
)
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from ._workspace_operations import WorkspaceOperations
from .skills import SkillSource, WorkspaceSkills


class _SkillDiscoveryBackend(BackendProtocol):
    """Decode UTF-8 signatures for discovery while preserving stored file bytes.

    Deep Agents 0.7.19's ``_skill_metadata_from_response`` decodes plain UTF-8,
    leaving a BOM in front of its required frontmatter delimiter. Only metadata
    discovery receives this view; file tools retain the original backend. The
    borrowed backend keeps ownership of reads, errors, and cancellation.
    """

    def __init__(self, backend: BackendProtocol) -> None:
        self._backend = backend

    async def als(self, path: str) -> LsResult:
        """List the original directories with the same access restrictions."""
        return await self._backend.als(path)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Remove only a leading UTF-8 signature from successful metadata reads."""
        responses = await self._backend.adownload_files(paths)
        return [
            replace(response, content=response.content[len(BOM_UTF8) :])
            if response.error is None
            and response.content is not None
            and response.content.startswith(BOM_UTF8)
            else response
            for response in responses
        ]


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
        operations: WorkspaceOperations | None = None,
    ) -> None:
        self._operations = operations
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
        discovery_backend = _SkillDiscoveryBackend(backend)
        self._readers = tuple(
            (
                source,
                SkillsMiddleware(backend=discovery_backend, sources=[source.directory]),
            )
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
            with (
                nullcontext()
                if self._operations is None
                else self._operations.framework(
                    "skills", "discover:" + source.directory, state
                )
            ):
                update = await reader.abefore_agent(
                    {**state, "skills_metadata": None}, runtime, {}
                )
            assert update is not None
            for skill in update["skills_metadata"]:
                if source.names is None or skill["name"] in source.names:
                    skills[skill["name"]] = skill
            errors.extend(update.get("skills_load_errors", []))
        return {"skills_metadata": list(skills.values()), "skills_load_errors": errors}


def resolve_skill_sources(
    declarations: Sequence[SkillSource | WorkspaceSkills] | None,
    prepared: Sequence[SkillSource],
    *,
    inherit: bool,
) -> list[SkillSource]:
    """Apply role selections to real prepared roots without widening host policy."""
    result: list[SkillSource] = []
    selected = False
    for source in declarations or ():
        if isinstance(source, SkillSource):
            result.append(source)
            continue
        selected = True
        for provider in prepared:
            names = (
                provider.names
                if source.names is None
                else source.names
                if provider.names is None
                else tuple(name for name in source.names if name in provider.names)
            )
            result.append(SkillSource(provider.directory, names=names))
    if inherit and not selected:
        result.extend(prepared)
    return result
