"""Public OpenSandbox error-family contracts."""

from __future__ import annotations

import pytest

from tinkerfin_sandbox import (
    OpenSandboxBackendError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBusyError,
    OpenSandboxError,
    OpenSandboxErrorCode,
    OpenSandboxFileChangedError,
    OpenSandboxFileTooLargeError,
    OpenSandboxInitializationError,
    OpenSandboxLifecycleUncertainError,
    OpenSandboxNotTextError,
    OpenSandboxPausedError,
    OpenSandboxSettlementTimeoutError,
    OpenSandboxStateUnavailableError,
    OpenSandboxWarmPoolUnavailableError,
    OpenSandboxWorkspaceNotInitializedError,
)


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (OpenSandboxFileTooLargeError, OpenSandboxErrorCode.FILE_TOO_LARGE),
        (OpenSandboxPausedError, OpenSandboxErrorCode.PAUSED),
        (OpenSandboxBusyError, OpenSandboxErrorCode.BUSY),
        (OpenSandboxLifecycleUncertainError, OpenSandboxErrorCode.LIFECYCLE_UNCERTAIN),
        (OpenSandboxFileChangedError, OpenSandboxErrorCode.FILE_CHANGED),
        (OpenSandboxNotTextError, OpenSandboxErrorCode.NOT_TEXT),
        (
            OpenSandboxWorkspaceNotInitializedError,
            OpenSandboxErrorCode.WORKSPACE_NOT_INITIALIZED,
        ),
    ],
)
def test_admission_errors_preserve_the_public_backend_error_contract(
    error_type: type[OpenSandboxBackendError], code: OpenSandboxErrorCode
) -> None:
    cause = RuntimeError("private provider response")
    error = error_type("Sandbox is not ready for use", cause=cause)
    assert isinstance(error, OpenSandboxError)
    assert error.code is code
    assert error.cause is error.__cause__ is cause
    assert dict(error.context) == {}
    assert "private" not in str(error)


def test_error_codes_are_unique_and_namespaced() -> None:
    values = [code.value for code in OpenSandboxErrorCode]

    assert len(values) == len(set(values))
    assert all(value.startswith("sandbox.") for value in values)


def test_error_contexts_are_separated_read_only_and_copied() -> None:
    cause = ConnectionError("database internals")
    context = {"retryable": True}
    diagnostic_context = {
        "implementation": "sqlalchemy",
        "dialect": "mysql",
        "operation": "read_binding",
    }
    error = OpenSandboxStateUnavailableError(
        "State is unavailable",
        context=context,
        diagnostic_context=diagnostic_context,
        cause=cause,
    )
    context["retryable"] = False
    diagnostic_context["operation"] = "mutated"

    assert isinstance(error, OpenSandboxError)
    assert error.code is OpenSandboxErrorCode.STATE_UNAVAILABLE
    assert error.cause is cause
    assert error.__cause__ is cause
    assert str(error) == "State is unavailable"
    assert dict(error.context) == {"retryable": True}
    assert dict(error.diagnostic_context) == {
        "implementation": "sqlalchemy",
        "dialect": "mysql",
        "operation": "read_binding",
    }
    assert not hasattr(error.context, "__setitem__")
    assert not hasattr(error.diagnostic_context, "__setitem__")


def test_timeout_errors_remain_machine_classifiable() -> None:
    state_timeout = OpenSandboxBackendTimeoutError("command timed out")
    settlement_timeout = OpenSandboxSettlementTimeoutError(timeout=1)

    assert state_timeout.code is OpenSandboxErrorCode.BACKEND_TIMEOUT
    assert isinstance(settlement_timeout, TimeoutError)
    assert settlement_timeout.code is OpenSandboxErrorCode.SETTLEMENT_TIMEOUT


def test_warm_pool_failure_is_public_and_machine_classifiable() -> None:
    error = OpenSandboxWarmPoolUnavailableError(
        "Warm capacity is unavailable",
        context={"target_capacity": 1},
    )

    assert isinstance(error, RuntimeError)
    assert error.code is OpenSandboxErrorCode.WARM_POOL_UNAVAILABLE
    assert dict(error.context) == {"target_capacity": 1}


def test_initialization_error_is_distinct_and_preserves_its_cause() -> None:
    cause = TimeoutError("private initializer details")
    error = OpenSandboxInitializationError("Sandbox initialization failed", cause=cause)
    assert isinstance(error, OpenSandboxError)
    assert error.code is OpenSandboxErrorCode.INITIALIZATION_FAILED
    assert error.cause is cause
    assert error.__cause__ is cause
    assert dict(error.context) == {}
    assert not hasattr(error.context, "__setitem__")
    assert "private" not in str(error)


def test_actionable_workspace_failures_expose_only_their_safe_category() -> None:
    from tinkerfin_contracts import WorkspacePreparationFailure
    from tinkerfin_sandbox import OpenSandboxBusyError, OpenSandboxFileChangedError

    for failure, reason in [
        (OpenSandboxBusyError("busy"), "busy"),
        (OpenSandboxFileChangedError("changed"), "file_conflict"),
    ]:
        assert isinstance(failure, WorkspacePreparationFailure)
        assert failure.workspace_failure == reason
