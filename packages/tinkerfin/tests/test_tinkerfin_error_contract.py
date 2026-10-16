"""Public TinkerFin error-family contracts."""

from __future__ import annotations

import pytest

from tinkerfin import (
    AgUiResumeBindingError,
    AgUiSettlementTimeoutError,
    DelegationFailedError,
    DelegationReplayError,
    TinkerFinError,
    TinkerFinErrorCode,
    TinkerFinLifecycleError,
)
from tinkerfin.coordination import RunCoordinationError
from tinkerfin.files import FileConflict
from tinkerfin.redis import RedisLeaseError, RedisLeaseUnavailableError


def test_error_codes_are_unique_and_namespaced() -> None:
    values = [code.value for code in TinkerFinErrorCode]

    assert len(values) == len(set(values))
    assert all(value.startswith("tinkerfin.") for value in values)


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (RedisLeaseUnavailableError, TinkerFinErrorCode.REDIS_LEASE_UNAVAILABLE),
        (FileConflict, TinkerFinErrorCode.FILE_CONFLICT),
    ],
)
def test_error_contexts_are_separated_read_only_and_copied(
    error_type: type[TinkerFinError],
    code: TinkerFinErrorCode,
) -> None:
    cause = ConnectionError("redis internals")
    context = {"retryable": True}
    diagnostic_context = {"implementation": "redis", "operation": "acquire"}
    error = error_type(
        "Run coordination is unavailable",
        context=context,
        diagnostic_context=diagnostic_context,
        cause=cause,
    )
    context["retryable"] = False
    diagnostic_context["operation"] = "mutated"

    assert isinstance(error, TinkerFinError)
    assert error.code is code
    assert error.cause is cause
    assert error.__cause__ is cause
    assert str(error) == "Run coordination is unavailable"
    assert dict(error.context) == {"retryable": True}
    assert dict(error.diagnostic_context) == {
        "implementation": "redis",
        "operation": "acquire",
    }
    assert not hasattr(error.context, "__setitem__")
    assert not hasattr(error.diagnostic_context, "__setitem__")


def test_semantic_errors_keep_python_catch_contracts() -> None:
    assert isinstance(AgUiResumeBindingError("invalid resume"), ValueError)
    assert isinstance(AgUiSettlementTimeoutError(timeout=1), TimeoutError)
    assert isinstance(TinkerFinLifecycleError("closed"), RuntimeError)
    assert issubclass(RedisLeaseUnavailableError, RedisLeaseError)
    assert issubclass(RunCoordinationError, TinkerFinError)
    assert issubclass(DelegationReplayError, TinkerFinLifecycleError)
    assert issubclass(DelegationFailedError, TinkerFinError)


def test_delegation_failure_details_remain_in_the_trusted_error_context() -> None:
    cause = ValueError("private provider detail")
    error = DelegationReplayError(
        "Delegation cannot continue",
        cause=cause,
        diagnostic_context={"original": str(cause)},
    )
    assert error.code is TinkerFinErrorCode.DELEGATION_REPLAY_INVALID
    assert error.cause is cause and error.__cause__ is cause
    assert str(error) == "Delegation cannot continue" and not error.context
    assert not hasattr(error.diagnostic_context, "__setitem__")
    assert (
        DelegationFailedError("Delegated task failed").code
        is TinkerFinErrorCode.DELEGATION_FAILED
    )
