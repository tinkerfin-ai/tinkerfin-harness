"""Public contracts for the request-scoped Deep Agents runtime package."""

from __future__ import annotations

import inspect

import tinkerfin
from tinkerfin import (
    AgUiSettlementTimeoutError,
    TinkerFin,
)


def test_top_level_exposes_the_stateless_runtime_contract() -> None:
    expected = {
        "CompactionResult",
        "AgUiResumeBinding",
        "AgUiResumeBindingError",
        "AgUiResumeReceipt",
        "AgUiResumeReceiptObserver",
        "AgUiResumeNotSavedObserver",
        "AgUiResumeRequest",
        "AgUiResumeResponse",
        "AgUiRunStream",
        "AgUiSettlementTimeoutError",
        "AgUiUserInput",
        "AgentMode",
        "AgentRuntime",
        "AttachmentContent",
        "AttachmentSupport",
        "ContextKind",
        "EventObserver",
        "RunIdentity",
        "RunObservationError",
        "NativeRunStream",
        "NativeStreamPart",
        "PartObserver",
        "SseBody",
        "SseEventIdResolver",
        "SseMapper",
        "SsePayload",
        "SsePreflight",
        "TinkerFin",
        "TinkerFinError",
        "TinkerFinErrorCode",
        "TinkerFinLifecycleError",
        "TinkerFinStreamProtocolError",
        "TraceContribution",
        "trace_contribution",
    }

    assert set(tinkerfin.__all__) == expected
    assert all(getattr(tinkerfin, name) is not None for name in expected)
    assert issubclass(AgUiSettlementTimeoutError, TimeoutError)


def test_extension_contracts_are_not_exposed_from_the_root() -> None:
    extension_names = {
        "DeepAgentsRuntimeProfile",
        "DeepAgentsV2RuntimeProfile",
        "DeepAgentsV3RuntimeProfile",
        "InMemoryRunCoordinator",
        "NativeStreamDriver",
        "NativeStreamFrame",
        "ReasoningExtractor",
        "RedisLeaseError",
        "RedisLeaseLifecycleError",
        "RedisLeaseProtocolError",
        "RedisLeaseTimeoutError",
        "RedisLeaseUnavailableError",
        "RunCoordinator",
        "RunCoordinationError",
        "RunCoordinationOwnershipLostError",
        "RunCoordinationTimeoutError",
        "RunCoordinationUnavailableError",
        "join_task",
    }

    assert extension_names.isdisjoint(tinkerfin.__all__)
    assert all(not hasattr(tinkerfin, name) for name in extension_names)


def test_factory_has_no_application_resource_lifecycle() -> None:
    constructor = inspect.signature(TinkerFin).parameters

    assert tuple(constructor) == (
        "checkpointer",
        "run_coordinator",
        "store",
        "runtime_profile",
    )
    assert not hasattr(TinkerFin, "__aenter__")
    assert not hasattr(TinkerFin, "__aexit__")


def test_low_level_source_contract_is_not_public() -> None:
    removed = {
        "AgUiNativeStreamConfig",
        "AgUiNativeStreamInvocation",
        "GraphRunStream",
        "NativeTinkerFinRun",
        "TinkerFinRun",
    }

    assert not hasattr(TinkerFin, "run")
    assert removed.isdisjoint(tinkerfin.__all__)
    assert all(not hasattr(tinkerfin, name) for name in removed)
