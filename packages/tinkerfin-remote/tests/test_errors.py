"""Verify the supported remote failure and diagnostics boundary."""

from collections.abc import MutableMapping

import pytest

from tinkerfin_remote import RemoteError, RemoteErrorCode, WorkspaceRejected


def test_remote_error_preserves_safe_readonly_context_and_private_cause() -> None:
    context: dict[str, str | int | float | bool | None] = {"operation": "close"}
    diagnostic: dict[str, str | int | float | bool | None] = {"detail": "private"}
    cause = RuntimeError("private provider data")
    error = RemoteError(
        RemoteErrorCode.STORAGE,
        "Storage is unavailable",
        context=context,
        diagnostic_context=diagnostic,
        cause=cause,
    )
    context["operation"] = "changed"
    diagnostic["detail"] = "changed"
    assert error.context == {"operation": "close"}
    assert error.diagnostic_context == {"detail": "private"}
    assert not isinstance(error.context, MutableMapping)
    assert not isinstance(error.diagnostic_context, MutableMapping)
    assert error.cause is cause and error.__cause__ is cause
    assert error.message == str(error) == "Storage is unavailable"
    assert "private" not in repr(error)
    assert error.code == "remote.storage"
    assert len({code.value for code in RemoteErrorCode}) == len(RemoteErrorCode)
    assert all(code.value.startswith("remote.") for code in RemoteErrorCode)
    assert isinstance(WorkspaceRejected(), RemoteError)
    assert WorkspaceRejected().code == "remote.rejected"
    rejected = WorkspaceRejected(cause=cause, context={"permission": "files"})
    assert rejected.cause is cause and rejected.__cause__ is cause
    assert rejected.context == {"permission": "files"}


@pytest.mark.parametrize(
    ("code", "failure"),
    [
        (RemoteErrorCode.CANCELLED, "stopped"),
        (RemoteErrorCode.CLOSED, "stopped"),
        (RemoteErrorCode.CONFLICT, "busy"),
        (RemoteErrorCode.CAPACITY, "busy"),
        (RemoteErrorCode.TIMEOUT, "timeout"),
        (RemoteErrorCode.CLEANUP, "unavailable"),
    ],
)
def test_workspace_failure_keeps_runtime_status_contract(
    code: RemoteErrorCode, failure: str
) -> None:
    assert RemoteError(code, "Unavailable").workspace_failure == failure
