"""Semantic Runtime tracing with bounded memory and optional database persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .backend import TraceGraphQueryBackend as TraceGraphQueryBackend
from .backend import TraceGraphRebuildBackend as TraceGraphRebuildBackend
from .backend import TraceLedgerBackend as TraceLedgerBackend
from .backend import TraceStoreOptions as TraceStoreOptions
from .capture import CapturedValue as CapturedValue
from .capture import CapturePolicy as CapturePolicy
from .capture import ReasoningCapturePolicy as ReasoningCapturePolicy
from .capture import ToolCaptureRule as ToolCaptureRule
from .capture import ToolTraceCapture as ToolTraceCapture
from .codec import CanonicalTracePayloadCodec as CanonicalTracePayloadCodec
from .codec import EncodedTracePayload as EncodedTracePayload
from .durable_store import DurableTraceStore as DurableTraceStore
from .durable_store import InMemoryTraceStore as InMemoryTraceStore
from .errors import AmbiguousTraceHead as AmbiguousTraceHead
from .errors import InvalidTraceCursor as InvalidTraceCursor
from .errors import InvalidTraceReference as InvalidTraceReference
from .errors import TraceCaptureRejected as TraceCaptureRejected
from .errors import TraceCorruption as TraceCorruption
from .errors import TraceFollowLifecycleError as TraceFollowLifecycleError
from .errors import TraceObserverFailed as TraceObserverFailed
from .errors import (
    TraceProjectionCheckpointConflict as TraceProjectionCheckpointConflict,
)
from .errors import TraceProjectionFailed as TraceProjectionFailed
from .errors import TraceQuotaExceeded as TraceQuotaExceeded
from .errors import TraceRunConflict as TraceRunConflict
from .errors import TraceRunNotFound as TraceRunNotFound
from .errors import TraceStoreError as TraceStoreError
from .errors import TraceStoreProtocolError as TraceStoreProtocolError
from .errors import TraceStoreTimeout as TraceStoreTimeout
from .errors import TraceThreadNotFound as TraceThreadNotFound
from .errors import TracingError as TracingError
from .errors import TracingErrorCode as TracingErrorCode
from .facts import CallTrackingFact as CallTrackingFact
from .facts import ContextContributionFact as ContextContributionFact
from .facts import InteractionFact as InteractionFact
from .facts import MessageFact as MessageFact
from .facts import ModelCallFact as ModelCallFact
from .facts import NativeExtraFact as NativeExtraFact
from .facts import PlanRevisionFact as PlanRevisionFact
from .facts import ReasoningFact as ReasoningFact
from .facts import RunFact as RunFact
from .facts import StateRevisionFact as StateRevisionFact
from .facts import SubagentFact as SubagentFact
from .facts import ToolExecutionFact as ToolExecutionFact
from .facts import ToolFact as ToolFact
from .facts import TraceEvent as TraceEvent
from .facts import TraceSemanticFact as TraceSemanticFact
from .facts import TurnFact as TurnFact
from .follow import TraceFollow as TraceFollow
from .graph import TraceGraph as TraceGraph
from .graph import TraceGraphCompleteness as TraceGraphCompleteness
from .graph import TraceGraphDelta as TraceGraphDelta
from .graph import TraceGraphFailure as TraceGraphFailure
from .graph import TraceGraphFilter as TraceGraphFilter
from .graph import TraceGraphLinkIssue as TraceGraphLinkIssue
from .graph import TraceGraphNode as TraceGraphNode
from .graph import TraceGraphNodeKind as TraceGraphNodeKind
from .graph import TraceGraphNodeStatus as TraceGraphNodeStatus
from .graph import TraceGraphPage as TraceGraphPage
from .graph import TraceGraphQueryLimits as TraceGraphQueryLimits
from .graph import TraceGraphTurn as TraceGraphTurn
from .graph_query import TraceGraphQuery as TraceGraphQuery
from .limits import TraceLimits as TraceLimits
from .model_requests import TraceModelRequest as TraceModelRequest
from .projection import TraceProjection as TraceProjection
from .query import TraceThread as TraceThread
from .redaction import CompositeRedactor as CompositeRedactor
from .redaction import RedactionContentKind as RedactionContentKind
from .redaction import RedactionContext as RedactionContext
from .redaction import TraceRedactor as TraceRedactor
from .redaction import redact_json_paths as redact_json_paths
from .store import StoreThreadSnapshot as StoreThreadSnapshot
from .store import StoreWriterSnapshot as StoreWriterSnapshot
from .store import TraceGraphRebuildStore as TraceGraphRebuildStore
from .store import TraceGraphStore as TraceGraphStore
from .store import TraceProjectionCheckpoint as TraceProjectionCheckpoint
from .store import TraceStore as TraceStore
from .store import TraceStoreUpdate as TraceStoreUpdate
from .store import TraceThreadKey as TraceThreadKey
from .store import TraceWriter as TraceWriter
from .testing import verify_trace_ledger_backend as verify_trace_ledger_backend
from .tracer import Tracer as Tracer
from .views import TraceCompleteness as TraceCompleteness
from .views import TraceEntityDelta as TraceEntityDelta
from .views import TraceEventPage as TraceEventPage
from .views import TraceInteraction as TraceInteraction
from .views import TraceMessage as TraceMessage
from .views import TraceReasoning as TraceReasoning
from .views import TraceState as TraceState
from .views import TraceStatus as TraceStatus
from .views import TraceSummary as TraceSummary
from .views import TraceUpdate as TraceUpdate
from .writing import TraceWritePolicy as TraceWritePolicy

if TYPE_CHECKING:
    from .sql_schema import TraceStoreSchema as TraceStoreSchema
    from .sql_schema import get_trace_store_schema as get_trace_store_schema
    from .sql_store import SqlAlchemyTraceStore as SqlAlchemyTraceStore

__all__ = [
    "AmbiguousTraceHead",
    "CallTrackingFact",
    "CanonicalTracePayloadCodec",
    "CapturePolicy",
    "CapturedValue",
    "CompositeRedactor",
    "ContextContributionFact",
    "DurableTraceStore",
    "EncodedTracePayload",
    "InMemoryTraceStore",
    "InteractionFact",
    "InvalidTraceCursor",
    "InvalidTraceReference",
    "MessageFact",
    "ModelCallFact",
    "NativeExtraFact",
    "PlanRevisionFact",
    "ReasoningCapturePolicy",
    "ReasoningFact",
    "RedactionContentKind",
    "RedactionContext",
    "RunFact",
    "SqlAlchemyTraceStore",
    "StateRevisionFact",
    "StoreThreadSnapshot",
    "StoreWriterSnapshot",
    "SubagentFact",
    "ToolCaptureRule",
    "ToolExecutionFact",
    "ToolFact",
    "ToolTraceCapture",
    "TraceCaptureRejected",
    "TraceCompleteness",
    "TraceCorruption",
    "TraceEntityDelta",
    "TraceEvent",
    "TraceEventPage",
    "TraceFollow",
    "TraceFollowLifecycleError",
    "TraceGraph",
    "TraceGraphCompleteness",
    "TraceGraphDelta",
    "TraceGraphFailure",
    "TraceGraphFilter",
    "TraceGraphLinkIssue",
    "TraceGraphNode",
    "TraceGraphNodeKind",
    "TraceGraphNodeStatus",
    "TraceGraphPage",
    "TraceGraphQuery",
    "TraceGraphQueryBackend",
    "TraceGraphQueryLimits",
    "TraceGraphRebuildBackend",
    "TraceGraphRebuildStore",
    "TraceGraphStore",
    "TraceGraphTurn",
    "TraceInteraction",
    "TraceLedgerBackend",
    "TraceLimits",
    "TraceMessage",
    "TraceModelRequest",
    "TraceObserverFailed",
    "TraceProjection",
    "TraceProjectionCheckpoint",
    "TraceProjectionCheckpointConflict",
    "TraceProjectionFailed",
    "TraceQuotaExceeded",
    "TraceReasoning",
    "TraceRedactor",
    "TraceRunConflict",
    "TraceRunNotFound",
    "TraceSemanticFact",
    "TraceState",
    "TraceStatus",
    "TraceStore",
    "TraceStoreError",
    "TraceStoreOptions",
    "TraceStoreProtocolError",
    "TraceStoreSchema",
    "TraceStoreTimeout",
    "TraceStoreUpdate",
    "TraceSummary",
    "TraceThread",
    "TraceThreadKey",
    "TraceThreadNotFound",
    "TraceUpdate",
    "TraceWritePolicy",
    "TraceWriter",
    "Tracer",
    "TracingError",
    "TracingErrorCode",
    "TurnFact",
    "get_trace_store_schema",
    "redact_json_paths",
    "verify_trace_ledger_backend",
]


def __getattr__(name: str) -> object:
    """Load optional database integrations when their public symbol is requested."""

    if name == "SqlAlchemyTraceStore":
        try:
            from .sql_store import SqlAlchemyTraceStore
        except ModuleNotFoundError as error:
            if error.name not in {"sqlalchemy", "tinkerfin_sqlalchemy"} and not str(
                error.name
            ).startswith("sqlalchemy."):
                raise
            raise ImportError(
                f'{name} requires "tinkerfin-tracing[sqlalchemy]"'
            ) from error
        globals()["SqlAlchemyTraceStore"] = SqlAlchemyTraceStore
        return SqlAlchemyTraceStore
    if name in {"TraceStoreSchema", "get_trace_store_schema"}:
        try:
            from .sql_schema import TraceStoreSchema, get_trace_store_schema
        except ModuleNotFoundError as error:
            if error.name not in {"sqlalchemy", "tinkerfin_sqlalchemy"} and not str(
                error.name
            ).startswith("sqlalchemy."):
                raise
            raise ImportError(
                f'{name} requires "tinkerfin-tracing[sqlalchemy]"'
            ) from error
        globals()["TraceStoreSchema"] = TraceStoreSchema
        globals()["get_trace_store_schema"] = get_trace_store_schema
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
