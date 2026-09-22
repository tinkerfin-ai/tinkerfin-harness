"""Current semantic facts stored in the Trace Ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, model_validator

from tinkerfin_contracts import RunIdentity, RunInputKind, RunTerminalOutcome

from ._models import TimedTraceModel, TraceModel
from .capture import CapturedValue


def _omit_false(value: bool) -> bool:
    return not value


class TraceFactBase(TimedTraceModel, frozen=True):
    """Attach stable source identity and graph scope to one semantic fact."""

    source_observation_id: str = Field(min_length=1, max_length=1024)
    identity: RunIdentity
    graph_namespace: tuple[str, ...] = ()
    in_subagent_scope: bool = Field(
        default=False,
        exclude_if=_omit_false,
        description="Whether the graph namespace is a proven Subagent execution scope",
    )


class TurnFact(TraceFactBase, frozen=True):
    """Start one user task that can span multiple resumed Run segments."""

    kind: Literal["turn.started"] = "turn.started"
    turn_id: str = Field(min_length=1, max_length=2048)
    user_message_id: str | None = Field(default=None, min_length=1, max_length=1024)
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=1024)


class RunFact(TraceFactBase, frozen=True):
    """Describe the root Runtime lifecycle without transport or Subagent state.

    Run observations describe the whole invocation. Their graph namespace is always empty
    and ``in_subagent_scope`` is false; child execution belongs to ``SubagentFact``.
    """

    kind: Literal["run"] = "run"
    phase: Literal[
        "started",
        "input",
        "resumed",
        "resume_checkpointed",
        "observer_failed",
        "terminal",
        "closed",
    ]
    input_kind: RunInputKind | None = None
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=1024)
    outcome: RunTerminalOutcome | None = None
    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description=(
            "Client-safe terminal code; runtime_initialization_error proves "
            "that Agent execution did not start"
        ),
    )
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    observer_name: str | None = Field(default=None, min_length=1, max_length=1024)
    interrupt_ids: tuple[str, ...] = ()
    input: CapturedValue | None = None
    config: CapturedValue | None = None
    failure_origin: bool = Field(default=False, exclude_if=_omit_false)

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> RunFact:
        """Require the evidence needed to interpret each lifecycle phase."""

        if self.graph_namespace != () or self.in_subagent_scope is not False:
            raise ValueError("Run lifecycle facts require the root scope")
        if self.phase == "started" and self.input_kind is None:
            raise ValueError("started Run facts require input_kind")
        if self.phase == "input" and self.input_kind not in {
            "ordinary",
            "branch",
            "compaction",
        }:
            raise ValueError(
                "input Run facts require ordinary, branch or compaction input_kind"
            )
        if self.phase == "resumed" and self.input_kind not in {
            "continuation",
            "resume",
            "abandon",
        }:
            raise ValueError(
                "resumed Run facts require continuation, resume or abandon input_kind"
            )
        if self.phase in {"input", "resumed"} and (
            self.input is None or self.config is None
        ):
            raise ValueError("Run input facts require input and config capture")
        if self.input_kind == "branch" and self.parent_run_id is None:
            raise ValueError("branch Run facts require parent_run_id")
        if self.phase == "resume_checkpointed" and not self.interrupt_ids:
            raise ValueError("resume checkpoint facts require interrupt_ids")
        if self.interrupt_ids and self.phase not in {"resumed", "resume_checkpointed"}:
            raise ValueError(
                "interrupt_ids belong only to resumed and resume checkpoint facts"
            )
        if self.phase == "observer_failed" and (
            self.observer_name is None or self.error_type is None
        ):
            raise ValueError(
                "observer failure facts require observer_name and error_type"
            )
        if self.phase in {"terminal", "closed"} and self.outcome is None:
            raise ValueError("terminal and closed Run facts require outcome")
        if self.failure_origin and not (
            self.phase == "terminal"
            and self.outcome == "failed"
            and self.error_type is not None
        ):
            raise ValueError("only failed terminal Run facts can own a failure")
        return self


class MessageFact(TraceFactBase, frozen=True):
    """Record content and delivery state for one scoped conversation message.

    A cancelled, interrupted, or abandoned Assistant retains only the content actually
    observed before Run settlement. Those phases never assert a complete response.
    """

    kind: Literal["message"] = "message"
    phase: Literal[
        "started",
        "content",
        "completed",
        "reconciled",
        "removed",
        "cancelled",
        "interrupted",
        "abandoned",
    ]
    message_id: str = Field(min_length=1, max_length=2048)
    source_message_id: str | None = Field(default=None, min_length=1, max_length=1024)
    role: Literal["user", "assistant", "tool", "system", "other"]
    content: CapturedValue | None = None
    fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    from_state_snapshot: bool = Field(
        default=False,
        description="Whether this message revision came from complete state values",
    )
    name: str | None = Field(default=None, min_length=1, max_length=1024)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def content_phase_is_explicit(self) -> MessageFact:
        """Require content ownership or an explicit Tool-result reference."""

        if self.phase in {"cancelled", "interrupted", "abandoned"} and (
            self.role != "assistant" or self.from_state_snapshot
        ):
            raise ValueError("settled message phases require an Assistant delivery")
        tool_result_reference = (
            self.phase == "reconciled"
            and self.role == "tool"
            and self.tool_call_id is not None
        )
        if (
            self.phase in {"content", "reconciled"}
            and self.content is None
            and not tool_result_reference
        ):
            raise ValueError("message content facts require a capture envelope")
        return self


class ReasoningFact(TraceFactBase, frozen=True):
    """Append, reconcile, or complete explicitly enabled provider reasoning."""

    kind: Literal["reasoning"] = "reasoning"
    phase: Literal["content", "reconciled", "completed"]
    reasoning_id: str = Field(min_length=1, max_length=2048)
    message_id: str = Field(min_length=1, max_length=2048)
    source_message_id: str = Field(min_length=1, max_length=1024)
    extractor: str = Field(min_length=1, max_length=1024)
    content: CapturedValue | None = None

    @model_validator(mode="after")
    def content_phase_is_explicit(self) -> ReasoningFact:
        """Require retained or explicitly omitted content for content phases."""

        if self.phase in {"content", "reconciled"} and self.content is None:
            raise ValueError("reasoning content phases require a capture envelope")
        if self.phase == "completed" and self.content is not None:
            raise ValueError("completed reasoning facts cannot repeat content")
        return self


class ToolFact(TraceFactBase, frozen=True):
    """Describe one scoped Tool proposal, argument stream, end, or result."""

    kind: Literal["tool"] = "tool"
    phase: Literal[
        "started",
        "arguments",
        "completed",
        "result",
        "cancelled",
        "abandoned",
    ]
    tool_call_id: str = Field(min_length=1, max_length=2048)
    source_tool_call_id: str = Field(min_length=1, max_length=1024)
    parent_call_id: str | None = Field(default=None, min_length=1, max_length=2048)
    tool_name: str = Field(min_length=1, max_length=1024)
    content: CapturedValue | None = None
    result_status: Literal["success", "error"] | None = None
    failure_origin: bool = Field(default=False, exclude_if=_omit_false)

    @model_validator(mode="after")
    def result_phase_is_explicit(self) -> ToolFact:
        """Keep Tool execution results distinct from proposal lifecycle facts."""

        if self.phase == "result" and (
            self.content is None or self.result_status is None
        ):
            raise ValueError("Tool result facts require content and result_status")
        if self.phase != "result" and self.result_status is not None:
            raise ValueError("Only Tool result facts can set result_status")
        if self.failure_origin and not (
            self.phase == "result" and self.result_status == "error"
        ):
            raise ValueError("only failed Tool results can own a failure")
        return self


class StateRevisionFact(TraceFactBase, frozen=True):
    """Store changed top-level state keys without duplicating message snapshots."""

    kind: Literal["state.revision"] = "state.revision"
    revision_id: str = Field(min_length=1, max_length=2048)
    changes: CapturedValue
    removed_keys: tuple[str, ...] = ()


class InteractionFact(TraceFactBase, frozen=True):
    """Open or resolve one native approval, clarification, or review interaction."""

    kind: Literal["interaction"] = "interaction"
    phase: Literal["opened", "resolved"]
    interaction_id: str = Field(min_length=1, max_length=2048)
    source_interaction_id: str = Field(min_length=1, max_length=1024)
    interaction_kind: str = Field(min_length=1, max_length=1024)
    tool_call_ids: tuple[str, ...] = Field(
        default=(),
        description="Exact Native Tool calls correlated to review actions by position",
    )
    status: Literal["pending", "resolved", "cancelled"]
    payload: CapturedValue | None = None

    @model_validator(mode="after")
    def status_matches_phase(self) -> InteractionFact:
        """Enforce the pending-to-resolved interaction state machine."""

        if self.phase == "opened" and self.status != "pending":
            raise ValueError("opened interactions must be pending")
        if self.phase == "resolved" and self.status == "pending":
            raise ValueError("resolved interactions cannot remain pending")
        if any(not value or value != value.strip() for value in self.tool_call_ids):
            raise ValueError("interaction Tool call IDs must be canonical strings")
        if len(set(self.tool_call_ids)) != len(self.tool_call_ids):
            raise ValueError("interaction Tool call IDs must be unique")
        return self


class SubagentFact(TraceFactBase, frozen=True):
    """Record one validated child Agent execution or its waiting/terminal state.

    ``started`` requires ``running``; ``updated`` requires ``waiting``; ``completed``
    accepts ``succeeded``, ``failed``, ``cancelled``, or ``abandoned``. Only started
    facts carry ``input``, ``parent_tool_call_id``, ``parent_execution_id``, and
    ``model_call_id``. Subsequent facts retain the same ``subagent_id`` and graph namespace
    while inherited opening evidence supplies their request and relationships.
    """

    kind: Literal["subagent"] = "subagent"
    phase: Literal["started", "updated", "completed"]
    subagent_id: str = Field(min_length=1, max_length=2048)
    agent_name: str | None = Field(default=None, min_length=1, max_length=1024)
    parent_tool_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Raw parent task Tool call ID when Deep Agents provenance is proven",
    )
    parent_execution_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description="Actual task Tool execution that opened this subagent",
    )
    model_call_id: str | None = Field(default=None, min_length=1, max_length=2048)
    input: CapturedValue | None = None
    status: Literal[
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
        "abandoned",
    ]

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> SubagentFact:
        """Keep lifecycle state and immutable opening evidence unambiguous."""

        expected_statuses = {
            "started": {"running"},
            "updated": {"waiting"},
            "completed": {"succeeded", "failed", "cancelled", "abandoned"},
        }
        if self.status not in expected_statuses[self.phase]:
            raise ValueError("Subagent status does not match its lifecycle phase")
        if self.phase != "started" and any(
            value is not None
            for value in (
                self.parent_tool_call_id,
                self.parent_execution_id,
                self.model_call_id,
                self.input,
            )
        ):
            raise ValueError("Subagent opening evidence belongs only to started facts")
        return self


class PlanRevisionFact(TraceFactBase, frozen=True):
    """Record one public TinkerFin Plan state revision."""

    kind: Literal["plan.revision"] = "plan.revision"
    revision_id: str = Field(min_length=1, max_length=2048)
    revision: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, min_length=1, max_length=1024)
    plan: CapturedValue


class NativeExtraFact(TraceFactBase, frozen=True):
    """Retain structural metadata for an allowed extra Native stream mode."""

    kind: Literal["native.extra"] = "native.extra"
    mode: Literal["updates", "checkpoints", "debug", "custom"]
    data_type: str = Field(min_length=1, max_length=1024)
    top_level_keys: tuple[str, ...] = ()


class ModelCallFact(TraceFactBase, frozen=True):
    """Describe one provider call independently of Native response delivery."""

    kind: Literal["model.call"] = "model.call"
    phase: Literal[
        "started",
        "first_output",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "abandoned",
    ]
    call_id: str = Field(min_length=1, max_length=2048)
    parent_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    contribution_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Explicit context action owning this provider call",
    )
    agent_name: str | None = Field(default=None, min_length=1, max_length=1024)
    provider: str | None = Field(default=None, min_length=1, max_length=1024)
    model: str | None = Field(default=None, min_length=1, max_length=1024)
    context_started_at: datetime | None = Field(
        default=None,
        description=(
            "UTC time of the preceding visible execution boundary that opened "
            "context preparation for this provider attempt"
        ),
    )
    request: CapturedValue | None = None
    usage: CapturedValue | None = None
    response_metadata: CapturedValue | None = None
    system_message_positions: tuple[int, ...] = Field(
        description="Zero-based SystemMessage positions in the final provider request",
    )
    output_message_ids: tuple[str, ...] = Field(
        description="Stable provider output message identities when exposed",
    )
    tool_call_ids: tuple[str, ...] = ()
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    error_message: CapturedValue | None = None
    failure_origin: bool = Field(default=False, exclude_if=_omit_false)

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> ModelCallFact:
        """Keep request, response metadata, and errors owned by their phases."""

        if self.phase == "started" and self.request is None:
            raise ValueError("started model call facts require a request")
        if self.phase != "started" and self.request is not None:
            raise ValueError("only started model call facts may carry a request")
        if self.phase == "started" and self.context_started_at is None:
            raise ValueError("started model call facts require context_started_at")
        if self.phase != "started" and self.context_started_at is not None:
            raise ValueError(
                "only started model call facts may carry context_started_at"
            )
        if self.context_started_at is not None:
            if (
                self.context_started_at.tzinfo is None
                or self.context_started_at.utcoffset()
                != UTC.utcoffset(self.context_started_at)
            ):
                raise ValueError("context_started_at must be an aware UTC timestamp")
            if self.context_started_at > self.occurred_at:
                raise ValueError(
                    "context preparation cannot start after the model call"
                )
        if self.phase != "started" and self.system_message_positions:
            raise ValueError(
                "only started model call facts may carry SystemMessage positions"
            )
        if tuple(sorted(set(self.system_message_positions))) != (
            self.system_message_positions
        ):
            raise ValueError("SystemMessage positions must be unique and ordered")
        if any(position < 0 for position in self.system_message_positions):
            raise ValueError("SystemMessage positions must be non-negative")
        if self.phase not in {"first_output", "completed"} and self.output_message_ids:
            raise ValueError(
                "only first output or completed model calls may carry output message IDs"
            )
        if any(
            not message_id or message_id != message_id.strip()
            for message_id in self.output_message_ids
        ):
            raise ValueError("model output message IDs must be canonical text")
        if len(set(self.output_message_ids)) != len(self.output_message_ids):
            raise ValueError("model output message IDs must be unique")
        if self.phase not in {"completed"} and (
            self.usage is not None or self.response_metadata is not None
        ):
            raise ValueError("only completed model calls may carry response metadata")
        if self.phase == "failed" and self.error_type is None:
            raise ValueError("failed model calls require an error type")
        if self.phase != "failed" and (
            self.error_type is not None or self.error_message is not None
        ):
            raise ValueError("non-failure model call facts cannot carry an error")
        if self.phase != "completed" and self.tool_call_ids:
            raise ValueError("only completed model call facts may carry Tool call IDs")
        if len(set(self.tool_call_ids)) != len(self.tool_call_ids):
            raise ValueError("model call Tool IDs must be unique")
        if self.failure_origin and self.phase != "failed":
            raise ValueError("only failed model calls can own a failure")
        return self


class CallTrackingFact(TraceFactBase, frozen=True):
    """Mark one Run as observed through provider and Tool callback boundaries."""

    kind: Literal["call.tracking"] = "call.tracking"


class ToolExecutionFact(TraceFactBase, frozen=True):
    """Describe actual Tool execution separately from a model Tool proposal."""

    kind: Literal["tool.execution"] = "tool.execution"
    phase: Literal[
        "started",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "abandoned",
    ]
    execution_id: str = Field(min_length=1, max_length=2048)
    parent_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    agent_name: str | None = Field(default=None, min_length=1, max_length=1024)
    source_tool_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    tool_name: str = Field(min_length=1, max_length=1024)
    input: CapturedValue | None = None
    output: CapturedValue | None = None
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    error_message: CapturedValue | None = None
    failure_origin: bool = Field(default=False, exclude_if=_omit_false)

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> ToolExecutionFact:
        """Keep actual input, output, and failures attached to their lifecycle phase."""

        if self.phase == "started" and self.input is None:
            raise ValueError("started Tool execution facts require input")
        if self.phase != "started" and self.input is not None:
            raise ValueError("only started Tool execution facts may carry input")
        if self.phase not in {"completed", "failed"} and self.output is not None:
            raise ValueError("only completed or handled-failure Tools may carry output")
        if self.phase == "completed" and self.output is None:
            raise ValueError("completed Tool execution facts require output")
        if self.phase == "failed" and self.error_type is None and self.output is None:
            raise ValueError("failed Tool executions require error or output evidence")
        if self.phase != "failed" and (
            self.error_type is not None or self.error_message is not None
        ):
            raise ValueError("non-failure Tool execution facts cannot carry an error")
        if self.failure_origin and self.phase != "failed":
            raise ValueError("only failed Tool executions can own a failure")
        return self


class ContextContributionFact(TraceFactBase, frozen=True):
    """Describe one explicit Memory, Guardrail, retrieval, or custom contribution."""

    kind: Literal["context.contribution"] = "context.contribution"
    phase: Literal[
        "started",
        "generated",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "abandoned",
    ]
    contribution_id: str = Field(min_length=1, max_length=2048)
    parent_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    model_call_ids: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        description="Provider calls explicitly owned by this action",
    )
    compaction_origin: Literal["manual", "automatic", "tool"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    parent_tool_call_id: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    context_kind: Literal["memory", "guardrail", "retrieval", "custom", "compaction"]
    name: str = Field(min_length=1, max_length=1024)
    input: CapturedValue | None = None
    output: CapturedValue | None = None
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    failure_origin: bool = Field(default=False, exclude_if=_omit_false)

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> ContextContributionFact:
        """Keep contribution input, output, and errors attached to their phases."""

        if self.phase != "started" and self.input is not None:
            raise ValueError("only started contribution facts may carry input")
        if self.phase not in {"completed", "generated"} and self.output is not None:
            raise ValueError("only completed contribution facts may carry output")
        if self.phase == "failed" and self.error_type is None:
            raise ValueError("failed contribution facts require an error type")
        if self.phase != "failed" and self.error_type is not None:
            raise ValueError(
                "non-failure contribution facts cannot carry an error type"
            )
        if self.failure_origin and self.phase != "failed":
            raise ValueError("only failed contributions can own a failure")
        return self


TraceSemanticFact: TypeAlias = Annotated[
    TurnFact
    | RunFact
    | MessageFact
    | ReasoningFact
    | ToolFact
    | StateRevisionFact
    | InteractionFact
    | SubagentFact
    | PlanRevisionFact
    | NativeExtraFact
    | CallTrackingFact
    | ModelCallFact
    | ToolExecutionFact
    | ContextContributionFact,
    Field(discriminator="kind"),
]

TRACE_FACT_ADAPTER: TypeAdapter[TraceSemanticFact] = TypeAdapter(TraceSemanticFact)


class TraceEvent(TraceModel, frozen=True):
    """Wrap one committed semantic fact with generation and sequence identity."""

    event_id: str = Field(min_length=1, max_length=1024)
    trace_seq: int = Field(ge=1)
    generation: str = Field(min_length=1, max_length=1024)
    fact: TraceSemanticFact
    persisted_bytes: int = Field(ge=1)


__all__ = [
    "TRACE_FACT_ADAPTER",
    "CallTrackingFact",
    "ContextContributionFact",
    "InteractionFact",
    "MessageFact",
    "ModelCallFact",
    "NativeExtraFact",
    "PlanRevisionFact",
    "ReasoningFact",
    "RunFact",
    "StateRevisionFact",
    "SubagentFact",
    "ToolExecutionFact",
    "ToolFact",
    "TraceEvent",
    "TraceFactBase",
    "TraceSemanticFact",
    "TurnFact",
]
