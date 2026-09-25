"""Validated Runtime and Native observations consumed by framework observers."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, model_validator

from ._json import FiniteJsonValue
from ._models import ContractModel, ObservationModel
from .identity import RunIdentity

RunInputKind: TypeAlias = Literal[
    "ordinary", "branch", "continuation", "resume", "abandon", "compaction"
]
RunMode: TypeAlias = Literal["default", "plan"]
RunTerminalOutcome: TypeAlias = Literal[
    "succeeded",
    "interrupted",
    "failed",
    "cancelled",
    "abandoned",
]
NativeExtraMode: TypeAlias = Literal["updates", "checkpoints", "debug", "custom"]
NativeMessageType: TypeAlias = Literal[
    "human",
    "assistant",
    "assistant_chunk",
    "tool",
    "system",
    "chat",
    "remove",
    "other",
]
ContextKind: TypeAlias = Literal[
    "memory", "guardrail", "retrieval", "custom", "compaction"
]


class ObservationBoundary(StrEnum):
    """Durability boundaries that a managed observation session must settle."""

    CALL_STARTED = "call_started"
    RESUME_CHECKPOINTED = "resume_checkpointed"
    INTERRUPT = "interrupt"
    TERMINAL = "terminal"
    CLOSE = "close"


class RunResumeSummary(ContractModel):
    """Describe one public interaction decision without transport-specific models."""

    interrupt_id: str = Field(min_length=1, max_length=1024)
    status: Literal["resolved", "cancelled"]
    decision: Literal["approve", "edit", "reject", "respond"] | None = None

    @model_validator(mode="after")
    def cancellation_has_no_fabricated_decision(self) -> RunResumeSummary:
        """Keep abandonment distinct from a resolved rejection decision."""

        if self.status == "cancelled" and self.decision is not None:
            raise ValueError("cancelled interactions cannot contain a decision")
        return self


class RunSourceContext(ContractModel):
    """Describe the real input and lineage used to open one Runtime request.

    Input and configuration values are finite JSON snapshots produced by the Runtime.
    A continuation operates on existing graph state without resolving an interaction;
    a resume supplies an interaction decision.
    Nested dictionaries and lists remain mutable. Observer implementations must treat
    received evidence as read-only and apply their retention policy before storing it.
    """

    identity: RunIdentity
    runtime_profile: str = Field(min_length=1, max_length=1024)
    input_kind: RunInputKind
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=1024)
    mode: RunMode = "default"
    input: FiniteJsonValue
    config: FiniteJsonValue
    resume: tuple[RunResumeSummary, ...] = ()
    private_state_keys: tuple[str, ...] = ()
    call_tracking_enabled: bool = Field(
        default=False,
        description="Whether the managed request installed provider and Tool callbacks",
    )

    @model_validator(mode="after")
    def identifiers_and_collections_are_canonical(self) -> RunSourceContext:
        """Reject an ambiguous Profile, resume group, or private state set."""

        if self.runtime_profile != self.runtime_profile.strip():
            raise ValueError("runtime_profile must be canonical text")
        resume_ids = tuple(item.interrupt_id for item in self.resume)
        if len(set(resume_ids)) != len(resume_ids):
            raise ValueError("resume interrupt IDs must be unique")
        if len(set(self.private_state_keys)) != len(self.private_state_keys):
            raise ValueError("private state keys must be unique")
        return self


class NativeToolCall(ContractModel):
    """Describe one complete Tool proposal carried by a Native message."""

    id: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=1024)
    arguments: dict[str, FiniteJsonValue]


class NativeToolCallChunk(ContractModel):
    """Describe Tool arguments identified by a provider index or an explicit ID.

    LangChain ToolCallChunk permits a missing index, including complete calls from
    langchain-ollama. Such calls must carry their own ID and name; they cannot use
    another call's positional binding. Indexed continuations may omit both fields.
    """

    index: int | None = Field(
        ge=0,
        description="Provider fragment index; None when the call identifies itself",
    )
    id: str | None = Field(default=None, min_length=1, max_length=1024)
    name: str | None = Field(default=None, min_length=1, max_length=1024)
    arguments: str = ""

    @model_validator(mode="after")
    def unindexed_calls_identify_themselves(self) -> NativeToolCallChunk:
        """Reject argument fragments that have no reliable correlation identity."""

        if self.index is None and (self.id is None or self.name is None):
            raise ValueError("unindexed tool calls require both id and name")
        return self


class NativeMessageRecord(ContractModel):
    """Represent a LangChain message without exposing its concrete Python class."""

    message_type: NativeMessageType
    id: str | None = Field(default=None, min_length=1, max_length=1024)
    name: str | None = Field(default=None, min_length=1, max_length=1024)
    content: FiniteJsonValue
    tool_calls: tuple[NativeToolCall, ...] = ()
    tool_call_chunks: tuple[NativeToolCallChunk, ...] = ()
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    tool_status: Literal["success", "error"] | None = None
    response_metadata: dict[str, FiniteJsonValue] = Field(default_factory=dict)
    usage_metadata: dict[str, FiniteJsonValue] | None = None


class NativeInterruptRecord(ContractModel):
    """Preserve one native interrupt ID and its finite public value."""

    id: str = Field(min_length=1, max_length=1024)
    value: FiniteJsonValue


class RunStartedObservation(ObservationModel):
    """Record admission of one Runtime request after coordination succeeds."""

    kind: Literal["run.started"] = "run.started"
    identity: RunIdentity


class RunInputObservation(ObservationModel):
    """Record the real ordinary, branch, resume, or abandonment input."""

    kind: Literal["run.input"] = "run.input"
    identity: RunIdentity
    source: RunSourceContext

    @model_validator(mode="after")
    def source_identity_matches_observation(self) -> RunInputObservation:
        """Prevent one observation from binding input owned by another Run."""

        if self.identity != self.source.identity:
            raise ValueError("Run input source identity must match the observation")
        return self


class RunResumeCheckpointedObservation(ObservationModel):
    """Record durable resume evidence before continuation output."""

    kind: Literal["run.resume_checkpointed"] = "run.resume_checkpointed"
    identity: RunIdentity
    marker_id: str = Field(min_length=1, max_length=1024)
    native_interrupt_ids: tuple[str, ...] = Field(min_length=1)


class RunObserverFailedObservation(ObservationModel):
    """Notify healthy observers that another observer terminated the Runtime."""

    kind: Literal["run.observer_failed"] = "run.observer_failed"
    identity: RunIdentity
    observer_name: str = Field(min_length=1, max_length=1024)
    error_type: str = Field(min_length=1, max_length=1024)


class RunTerminalObservation(ObservationModel):
    """Record the exactly-once semantic terminal selected by the Runtime."""

    kind: Literal["run.terminal"] = "run.terminal"
    identity: RunIdentity
    outcome: RunTerminalOutcome
    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description=(
            "Client-safe terminal code; runtime_initialization_error identifies "
            "a failure before Agent execution"
        ),
    )
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    interrupt_ids: tuple[str, ...] = ()


class RunClosedObservation(ObservationModel):
    """Record completion of Runtime-owned cleanup after the terminal boundary."""

    kind: Literal["run.closed"] = "run.closed"
    identity: RunIdentity
    outcome: RunTerminalOutcome


class ModelCallObservation(ObservationModel):
    """Record one provider call independently of Native stream delivery."""

    kind: Literal["call.model"] = "call.model"
    identity: RunIdentity
    phase: Literal[
        "started",
        "first_output",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "abandoned",
    ]
    call_id: str = Field(min_length=1, max_length=1024)
    parent_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    contribution_id: str | None = Field(
        default=None, description="Explicit context action owning this provider call"
    )
    graph_namespace: tuple[str, ...] = ()
    agent_name: str | None = Field(default=None, min_length=1, max_length=1024)
    provider: str | None = Field(default=None, min_length=1, max_length=1024)
    model: str | None = Field(default=None, min_length=1, max_length=1024)
    messages: tuple[NativeMessageRecord, ...] = Field(
        default=(),
        description="Final middleware-processed messages sent to the provider",
    )
    invocation: FiniteJsonValue | None = Field(
        default=None,
        description="Provider invocation parameters exposed by the locked callback",
    )
    options: FiniteJsonValue | None = Field(
        default=None,
        description="Bound model options exposed by the locked callback",
    )
    usage: dict[str, FiniteJsonValue] | None = None
    response_metadata: dict[str, FiniteJsonValue] | None = None
    output_message_ids: tuple[str, ...] = Field(
        default=(),
        description="Stable provider output message identities when exposed",
    )
    tool_call_ids: tuple[str, ...] = ()
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)
    failure_origin: bool = False

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> ModelCallObservation:
        """Require final request evidence only on start and errors only on failures."""

        has_request = (
            bool(self.messages)
            or self.invocation is not None
            or self.options is not None
        )
        if self.phase == "started" and not self.messages:
            raise ValueError("started model calls require final messages")
        if self.phase != "started" and has_request:
            raise ValueError("only started model calls may carry final request values")
        if self.phase == "failed" and self.error_type is None:
            raise ValueError("failed model calls require an error type")
        if self.phase != "failed" and (
            self.error_type is not None or self.error_message is not None
        ):
            raise ValueError("non-failure model call phases cannot carry an error")
        if self.failure_origin and self.phase != "failed":
            raise ValueError("only failed model calls can own a failure")
        if self.phase != "completed" and self.tool_call_ids:
            raise ValueError("only completed model calls may carry Tool call IDs")
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
        if any(
            not tool_call_id or tool_call_id != tool_call_id.strip()
            for tool_call_id in self.tool_call_ids
        ):
            raise ValueError("model Tool call IDs must be canonical text")
        if len(set(self.tool_call_ids)) != len(self.tool_call_ids):
            raise ValueError("model Tool call IDs must be unique")
        return self


class ToolExecutionObservation(ObservationModel):
    """Record actual Tool execution separately from a model's Tool proposal.

    ``graph_task_id`` identifies the Graph task that owns the execution and stays
    unchanged across its phases. Calls outside a Graph leave it unset; callback
    ``execution_id`` and model ``tool_call_id`` remain separate identities.
    """

    kind: Literal["call.tool"] = "call.tool"
    identity: RunIdentity
    phase: Literal[
        "started",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "abandoned",
    ]
    execution_id: str = Field(min_length=1, max_length=1024)
    parent_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    graph_namespace: tuple[str, ...] = ()
    graph_task_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description=(
            "Identity of the Graph task executing this Tool, distinct from the "
            "callback execution and model Tool call IDs; absent outside a Graph"
        ),
    )
    agent_name: str | None = Field(default=None, min_length=1, max_length=1024)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    tool_name: str = Field(min_length=1, max_length=1024)
    input: FiniteJsonValue | None = Field(
        default=None,
        description="Actual post-review Tool input carried only by the start phase",
    )
    output: FiniteJsonValue | None = Field(
        default=None,
        description="Actual Tool result carried only by a terminal phase",
    )
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)
    failure_origin: bool = False

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> ToolExecutionObservation:
        """Keep attempted input, successful output, and failures unambiguous."""

        if self.phase == "started" and self.input is None:
            raise ValueError("started Tool executions require actual input")
        if self.phase != "started" and self.input is not None:
            raise ValueError("only started Tool executions may carry input")
        if self.phase == "completed" and self.output is None:
            raise ValueError("completed Tool executions require output")
        if self.phase not in {"completed", "failed"} and self.output is not None:
            raise ValueError("only completed or handled-failure Tools may carry output")
        if self.phase == "failed" and self.error_type is None and self.output is None:
            raise ValueError("failed Tool executions require error or output evidence")
        if self.phase != "failed" and (
            self.error_type is not None or self.error_message is not None
        ):
            raise ValueError("non-failure Tool execution phases cannot carry an error")
        if self.failure_origin and self.phase != "failed":
            raise ValueError("only failed Tool executions can own a failure")
        return self


class ContextContributionObservation(ObservationModel):
    """Record one explicitly named Memory, Guardrail, retrieval, or custom action."""

    kind: Literal["call.context"] = "call.context"
    identity: RunIdentity
    phase: Literal[
        "started",
        "generated",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "abandoned",
    ]
    contribution_id: str = Field(min_length=1, max_length=1024)
    parent_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    model_call_ids: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        description="Provider calls explicitly owned by this action",
    )
    compaction_origin: Literal["manual", "automatic", "tool"] | None = None
    parent_tool_call_id: str | None = None
    graph_namespace: tuple[str, ...] = ()
    context_kind: ContextKind
    name: str = Field(min_length=1, max_length=1024)
    input: FiniteJsonValue | None = Field(
        default=None,
        description="Public contribution input carried only by the start phase",
    )
    output: FiniteJsonValue | None = Field(
        default=None,
        description="Public contribution result carried only by successful completion",
    )
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    failure_origin: bool = False

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> ContextContributionObservation:
        """Keep contribution input, output, and terminal errors phase-owned."""

        if self.phase != "started" and self.input is not None:
            raise ValueError("only started context contributions may carry input")
        if self.phase not in {"completed", "generated"} and self.output is not None:
            raise ValueError("only completed context contributions may carry output")
        if self.phase == "failed" and self.error_type is None:
            raise ValueError("failed contributions require an error type")
        if self.phase != "failed" and self.error_type is not None:
            raise ValueError(
                "non-failure contribution phases cannot carry an error type"
            )
        if self.failure_origin and self.phase != "failed":
            raise ValueError("only failed contributions can own a failure")
        return self


class NativeMessageObservation(ObservationModel):
    """Record one validated Native message part with complete graph scope."""

    kind: Literal["native.message"] = "native.message"
    identity: RunIdentity
    graph_namespace: tuple[str, ...]
    message: NativeMessageRecord
    metadata: dict[str, FiniteJsonValue] = Field(default_factory=dict)


class NativeReasoningObservation(ObservationModel):
    """Record explicitly enabled provider reasoning for one scoped message.

    The Runtime emits this observation only when a caller injects a verified
    provider extractor. Observers must apply an independent retention policy;
    enabling live reasoning does not by itself authorize persistence.
    """

    kind: Literal["native.reasoning"] = "native.reasoning"
    identity: RunIdentity
    graph_namespace: tuple[str, ...]
    message_id: str = Field(min_length=1, max_length=1024)
    extractor: str = Field(min_length=1, max_length=1024)
    content: FiniteJsonValue
    snapshot: bool = Field(
        description="Whether content is a complete message snapshot rather than a delta"
    )


class NativeTaskObservation(ObservationModel):
    """Record one validated LangGraph runtime task phase."""

    kind: Literal["native.task"] = "native.task"
    identity: RunIdentity
    graph_namespace: tuple[str, ...]
    phase: Literal["start", "result"]
    task_id: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=1024)
    triggers: tuple[str, ...] = ()
    input: FiniteJsonValue | None = None
    result: FiniteJsonValue | None = None
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    interrupts: tuple[NativeInterruptRecord, ...] = ()
    metadata: dict[str, FiniteJsonValue] = Field(default_factory=dict)


class NativeStateObservation(ObservationModel):
    """Record one validated root or subgraph state snapshot and interrupts."""

    kind: Literal["native.state"] = "native.state"
    identity: RunIdentity
    graph_namespace: tuple[str, ...]
    state: dict[str, FiniteJsonValue]
    messages: tuple[NativeMessageRecord, ...] = ()
    interrupts: tuple[NativeInterruptRecord, ...] = ()


class NativeExtraObservation(ObservationModel):
    """Record only safe structural metadata for a declared extra stream mode."""

    kind: Literal["native.extra"] = "native.extra"
    identity: RunIdentity
    graph_namespace: tuple[str, ...]
    mode: NativeExtraMode
    data_type: str = Field(min_length=1, max_length=1024)
    safe_size_bytes: int = Field(ge=0)
    top_level_keys: tuple[str, ...] = ()


NativeObservation: TypeAlias = (
    NativeMessageObservation
    | NativeReasoningObservation
    | NativeTaskObservation
    | NativeStateObservation
    | NativeExtraObservation
)


RuntimeObservation: TypeAlias = Annotated[
    RunStartedObservation
    | RunInputObservation
    | RunResumeCheckpointedObservation
    | RunObserverFailedObservation
    | RunTerminalObservation
    | RunClosedObservation
    | ModelCallObservation
    | ToolExecutionObservation
    | ContextContributionObservation
    | NativeMessageObservation
    | NativeReasoningObservation
    | NativeTaskObservation
    | NativeStateObservation
    | NativeExtraObservation,
    Field(discriminator="kind"),
]

RUNTIME_OBSERVATION_ADAPTER: TypeAdapter[RuntimeObservation] = TypeAdapter(
    RuntimeObservation
)


__all__ = [
    "RUNTIME_OBSERVATION_ADAPTER",
    "ContextContributionObservation",
    "ContextKind",
    "ModelCallObservation",
    "NativeExtraMode",
    "NativeExtraObservation",
    "NativeInterruptRecord",
    "NativeMessageObservation",
    "NativeMessageRecord",
    "NativeMessageType",
    "NativeObservation",
    "NativeReasoningObservation",
    "NativeStateObservation",
    "NativeTaskObservation",
    "NativeToolCall",
    "NativeToolCallChunk",
    "ObservationBoundary",
    "RunClosedObservation",
    "RunInputKind",
    "RunInputObservation",
    "RunMode",
    "RunObserverFailedObservation",
    "RunResumeCheckpointedObservation",
    "RunResumeSummary",
    "RunSourceContext",
    "RunStartedObservation",
    "RunTerminalObservation",
    "RunTerminalOutcome",
    "RuntimeObservation",
    "ToolExecutionObservation",
]
