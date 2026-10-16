"""Deep Agents v2 to AG-UI 0.1.19 conversion primitives."""

from ._json_schema import require_valid_schema as require_valid_schema
from ._json_schema import (
    validate_json_schema_instance as validate_json_schema_instance,
)
from .adapter import DeepAgentAgUiAdapter as DeepAgentAgUiAdapter
from .adapter import (
    ValidatedDeepAgentStreamPart as ValidatedDeepAgentStreamPart,
)
from .adapter import ValidatedExtraStreamPart as ValidatedExtraStreamPart
from .adapter import ValidatedMessageStreamPart as ValidatedMessageStreamPart
from .adapter import ValidatedTaskResultPayload as ValidatedTaskResultPayload
from .adapter import ValidatedTasksStreamPart as ValidatedTasksStreamPart
from .adapter import ValidatedTaskStartPayload as ValidatedTaskStartPayload
from .adapter import ValidatedUpdatesStreamPart as ValidatedUpdatesStreamPart
from .adapter import ValidatedValuesStreamPart as ValidatedValuesStreamPart
from .adapter import (
    validate_deep_agent_stream_part as validate_deep_agent_stream_part,
)
from .contracts import AgentRunOutcome as AgentRunOutcome
from .contracts import RunIdentity as RunIdentity
from .errors import AgUiAdapterError as AgUiAdapterError
from .errors import AgUiAdapterErrorCode as AgUiAdapterErrorCode
from .errors import AgUiConversionError as AgUiConversionError
from .errors import AgUiLifecycleError as AgUiLifecycleError
from .errors import AgUiSerializationError as AgUiSerializationError
from .errors import AgUiStreamContractError as AgUiStreamContractError
from .errors import HitlCorrelationError as HitlCorrelationError
from .errors import HitlNoMatchError as HitlNoMatchError
from .hitl import HitlActionRequest as HitlActionRequest
from .hitl import HitlRequest as HitlRequest
from .hitl import HitlReviewConfig as HitlReviewConfig
from .ids import ScopedIdCodec as ScopedIdCodec
from .interrupt_projection import project_interrupt as project_interrupt
from .lifecycle import AgUiLifecycleEventFactory as AgUiLifecycleEventFactory
from .media_events import (
    AttachmentAssistantMessage,
    AttachmentMessagesSnapshotEvent,
    AttachmentOutputEvent,
    AttachmentSnapshotMessage,
    AttachmentToolCallResultEvent,
    AttachmentToolMessage,
    parse_attachment_output_event,
)
from .microbatch import micro_batch as micro_batch
from .models import AgentRuntimeInterrupt as AgentRuntimeInterrupt
from .resume import ResumeMapper as ResumeMapper
from .resume import ResumeMappingError as ResumeMappingError
from .resume import ResumeTranslation as ResumeTranslation
from .runtime_interrupts import RuntimeInterruptEnvelope as RuntimeInterruptEnvelope
from .sse import SseEventId as SseEventId
from .sse import encode_sse as encode_sse
from .stream import astream_events as astream_events
from .subagent import SUBAGENT_PROVENANCE_SCHEMA as SUBAGENT_PROVENANCE_SCHEMA
from .subagent import SubagentProvenance as SubagentProvenance
from .subagent import create_subagent_provenance as create_subagent_provenance
from .subagent import subagent_invocation_id as subagent_invocation_id
from .tool_review import TOOL_REVIEW_SCHEMA as TOOL_REVIEW_SCHEMA
from .tool_review import ToolReviewContractError as ToolReviewContractError
from .tool_review import ToolReviewDecision as ToolReviewDecision
from .tool_review import ToolReviewInterruptMetadata as ToolReviewInterruptMetadata
from .tool_review import parse_tool_review_interrupt as parse_tool_review_interrupt

__all__ = [
    "SUBAGENT_PROVENANCE_SCHEMA",
    "TOOL_REVIEW_SCHEMA",
    "AgUiAdapterError",
    "AgUiAdapterErrorCode",
    "AgUiConversionError",
    "AgUiLifecycleError",
    "AgUiLifecycleEventFactory",
    "AgUiSerializationError",
    "AgUiStreamContractError",
    "AgentRunOutcome",
    "AgentRuntimeInterrupt",
    "AttachmentAssistantMessage",
    "AttachmentMessagesSnapshotEvent",
    "AttachmentOutputEvent",
    "AttachmentSnapshotMessage",
    "AttachmentToolCallResultEvent",
    "AttachmentToolMessage",
    "DeepAgentAgUiAdapter",
    "HitlActionRequest",
    "HitlCorrelationError",
    "HitlNoMatchError",
    "HitlRequest",
    "HitlReviewConfig",
    "ResumeMapper",
    "ResumeMappingError",
    "ResumeTranslation",
    "RunIdentity",
    "RuntimeInterruptEnvelope",
    "ScopedIdCodec",
    "SseEventId",
    "SubagentProvenance",
    "ToolReviewContractError",
    "ToolReviewDecision",
    "ToolReviewInterruptMetadata",
    "ValidatedDeepAgentStreamPart",
    "ValidatedExtraStreamPart",
    "ValidatedMessageStreamPart",
    "ValidatedTaskResultPayload",
    "ValidatedTaskStartPayload",
    "ValidatedTasksStreamPart",
    "ValidatedUpdatesStreamPart",
    "ValidatedValuesStreamPart",
    "astream_events",
    "create_subagent_provenance",
    "encode_sse",
    "micro_batch",
    "parse_attachment_output_event",
    "parse_tool_review_interrupt",
    "project_interrupt",
    "require_valid_schema",
    "subagent_invocation_id",
    "validate_deep_agent_stream_part",
    "validate_json_schema_instance",
]
