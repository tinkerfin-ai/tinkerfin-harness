"""Validated commands bound to one durable run identity."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tinkerfin import AgentMode, AgUiResumeRequest


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)


class StartRun(_Command):
    """Start a conversation using complete host-authorized user messages.

    Parameters are JSON execution settings passed to the Runtime's configurable
    options. Include choices that affect this command; never include credentials.
    Model and tool authorization remain host-owned. The Gateway freezes this
    command before admission and rejects conflicting retries of the same run.
    """

    kind: Literal["start"] = "start"
    messages: tuple[dict[str, JsonValue], ...] = Field(min_length=1)
    parent_run_id: str | None = None
    mode: AgentMode = "default"
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class ResumeRun(_Command):
    """Apply one complete, Runtime-validated batch of interrupt decisions.

    Admission does not confirm that decisions were saved. The separate resume
    settlement reports only confirmed saved or not-saved outcomes.
    """

    kind: Literal["resume"] = "resume"
    resume: AgUiResumeRequest
    parent_run_id: str | None = None
    mode: AgentMode = "default"
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class CompactRun(_Command):
    """Compress a saved, idle conversation with the supplied Runtime."""

    kind: Literal["compact"] = "compact"


RunCommand: TypeAlias = StartRun | ResumeRun | CompactRun


def _freeze(command: RunCommand, namespace: str) -> tuple[RunCommand, str]:
    # Validate Python values before JSON encoding; encoding must never normalize
    # an invalid nonfinite number to null. Execution and hashing use one snapshot.
    frozen = type(command).model_validate(command.model_dump(mode="python"))
    encoded = json.dumps(
        {"namespace": namespace, "command": frozen.model_dump(mode="python")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return frozen, hashlib.sha256(encoded).hexdigest()


__all__ = ["CompactRun", "ResumeRun", "RunCommand", "StartRun", "_freeze"]
