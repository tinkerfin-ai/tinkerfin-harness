"""Canonical checkpoint ownership issued by managed Graph invocation boundaries."""

from __future__ import annotations

import json
from typing import Literal, TypeAlias, cast

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tinkerfin_contracts import RunIdentity

LINEAGE_CONFIG_KEY = "__tinkerfin_checkpoint_lineage"
LINEAGE_METADATA_KEY = "_tinkerfin_lineage"
RESUME_METADATA_KEY = "_tinkerfin_resume"
RESUME_CONFIG_KEY = "__tinkerfin_resume_intent"
RESUME_WRITE_OWNER = "tinkerfin-resume-intent"
PLANNING_CHECKPOINT_RUN_ID = "tinkerfin-plan"
RUN_ID_METADATA_KEY = "_tinkerfin_run_id"
NAMESPACE_METADATA_KEY = "_tinkerfin_namespace"
CHECKPOINT_ROLE_METADATA_KEY = "_tinkerfin_checkpoint_role"
PARENT_RUN_ID_METADATA_KEY = "_tinkerfin_parent_run_id"
RUNTIME_PROFILE_METADATA_KEY = "_tinkerfin_runtime_profile"
NATIVE_CHECKPOINT_ROLE = "native"
PLANNING_CHECKPOINT_ROLE = "planning"

LineageRole: TypeAlias = Literal["native", "planning"]


class LineageMarker(RunIdentity):
    """Persist the full managed run identity and role without exposing Graph state."""

    parent_run_id: str | None = Field(default=None, min_length=1)
    runtime_profile: str = Field(min_length=1)
    role: LineageRole

    @field_validator("parent_run_id", "runtime_profile")
    @classmethod
    def lineage_identifiers_are_canonical(cls, value: str | None) -> str | None:
        """Reject identifiers that cannot produce stable lineage evidence."""

        if value is not None and value != value.strip():
            raise ValueError("lineage marker identifiers must be canonical")
        return value

    def canonical_json(self) -> str:
        """Return stable JSON for exact checkpoint ownership comparisons."""

        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )


class ResumeAnchor(BaseModel):
    """Keep one approval's original task, payload, and Tool-call correlation.

    Dynamic children can advance while their dispatch checkpoint stays paused.
    Retrying a request must therefore read these immutable facts instead of the
    child's latest interrupt or messages. Only matched Tool calls are retained.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: LineageMarker
    graph_namespace: str
    checkpoint_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    interrupt_id: str = Field(min_length=1)
    interrupt_json: str
    tool_calls_json: str
    cancellation_supported: bool


class ResumeIntent(LineageMarker):
    """Validated private checkpoint evidence for one saver-readable resume intent."""

    digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    role: LineageRole = Field(
        description="Graph role of the interrupted source checkpoint"
    )
    source_checkpoint_id: str = Field(min_length=1)
    source_checkpoint_ns: str
    native_interrupt_ids: tuple[str, ...] = Field(min_length=1)
    anchors: tuple[ResumeAnchor, ...] = Field(min_length=1)
    storage_checkpoint_id: str = Field(min_length=1)
    storage_checkpoint_ns: str
    decisions_json: str

    @field_validator("native_interrupt_ids", "anchors", mode="before")
    @classmethod
    def sequences_are_json_arrays(cls, value: object) -> object:
        """Normalize JSON arrays before strict tuple validation."""

        if isinstance(value, list):
            return tuple(cast(list[object], value))
        return value

    @model_validator(mode="after")
    def interrupt_ids_are_canonical(self) -> ResumeIntent:
        """Require a stable unique native interrupt set."""

        values = self.native_interrupt_ids
        if any(not value or value != value.strip() for value in values):
            raise ValueError("native interrupt IDs must be canonical strings")
        if len(set(values)) != len(values):
            raise ValueError("native interrupt IDs must be unique")
        if tuple(anchor.interrupt_id for anchor in self.anchors) != values:
            raise ValueError("resume anchors must cover the complete ordered batch")
        if any(
            anchor.source.thread != self.thread
            or anchor.source.runtime_profile != self.runtime_profile
            for anchor in self.anchors
        ):
            raise ValueError("resume anchors have conflicting thread or Profile")
        if not any(
            anchor.checkpoint_id == self.storage_checkpoint_id
            and anchor.graph_namespace == self.storage_checkpoint_ns
            for anchor in self.anchors
        ):
            raise ValueError("resume storage must be an actual approval checkpoint")
        return self


def bind_checkpoint_run(
    config: RunnableConfig,
    *,
    identity: RunIdentity,
    parent_run_id: str | None,
    runtime_profile: str,
) -> RunnableConfig:
    """Issue typed run ownership at the managed execution boundary.

    Ordinary metadata and model state never authorize checkpoint ownership. LangGraph
    preserves this Python value in execution config, while get_checkpoint_metadata
    excludes its private key. The scoped saver serializes the authoritative evidence.

    Args:
        config: Execution configuration whose containers remain unchanged.
        identity: Complete logical identity of this managed invocation.
        parent_run_id: Optional source Run in the same thread.
        runtime_profile: Integration that owns the invocation's checkpoint semantics.

    Returns:
        Copied configuration containing this invocation's typed ownership.
    """

    options = dict(config.get("configurable", {}))
    options.pop(RESUME_CONFIG_KEY, None)
    return {
        **config,
        "configurable": {
            **options,
            NAMESPACE_METADATA_KEY: identity.namespace,
            RUN_ID_METADATA_KEY: identity.run_id,
            RUNTIME_PROFILE_METADATA_KEY: runtime_profile,
            LINEAGE_CONFIG_KEY: LineageMarker(
                namespace=identity.namespace,
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                parent_run_id=parent_run_id,
                runtime_profile=runtime_profile,
                role="native",
            ),
        },
    }
