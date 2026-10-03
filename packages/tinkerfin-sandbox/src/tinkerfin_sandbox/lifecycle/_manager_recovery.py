"""Recover one authoritative user binding without replaying workspace operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, TypeVar

from ..backends.handle import OpenSandboxHandle
from ..backends.sdk import OpenSandboxBackend
from ..errors import (
    OpenSandboxBackendError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxStateError,
    OpenSandboxStateOwnershipError,
    UnexpectedOpenSandboxBackendError,
)
from ..models import OpenSandboxPurpose
from ._notifications import failure_reason
from ._purpose import require_binding_purpose
from .client import _connection_deadline
from .notifications import OpenSandboxLifecycleReason
from .state import OpenSandboxOwnerClaim

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

KeyT = TypeVar("KeyT")
_RecoveryReason = Literal["not_found", "unreachable", "unhealthy", "timeout"]


def _recovery_reason(error: OpenSandboxBackendError) -> _RecoveryReason | None:
    """Only declared connection or health failures permit recovery attempts."""
    if isinstance(error, OpenSandboxBackendTimeoutError):
        return "timeout"
    if isinstance(error, OpenSandboxBackendUnavailableError):
        reason = error.context.get("reason")
        if reason in {"not_found", "unreachable", "unhealthy", "timeout"}:
            if reason == "timeout":
                return "timeout"
            if reason == "not_found":
                return "not_found"
            if reason == "unhealthy":
                return "unhealthy"
            return "unreachable"
    return None


async def _check_health(
    self: OpenSandboxManager[KeyT], backend: OpenSandboxBackend | OpenSandboxHandle
) -> None:
    try:
        response = await backend.aexecute(self._client.config.health_command)
    except OpenSandboxBackendError:
        raise
    except TimeoutError as error:
        raise OpenSandboxBackendTimeoutError(
            "OpenSandbox health check timed out", cause=error
        ) from error
    except Exception as error:
        raise UnexpectedOpenSandboxBackendError(
            "OpenSandbox health check failed", cause=error
        ) from error
    if response.exit_code != 0:
        raise OpenSandboxBackendUnavailableError(
            "OpenSandbox health check did not succeed", context={"reason": "unhealthy"}
        )


async def _connect_existing(
    self: OpenSandboxManager[KeyT],
    owner_key: str,
    sandbox_id: str,
    handle: OpenSandboxHandle | None,
    claim: OpenSandboxOwnerClaim,
    *,
    purpose: OpenSandboxPurpose,
) -> OpenSandboxHandle:
    """Publish a verified local connection without ever deleting a remote instance."""
    backend = await self._client.connect(sandbox_id, purpose=purpose)
    try:
        if backend.id != sandbox_id:
            raise OpenSandboxStateOwnershipError(
                "OpenSandbox reconnect returned a different instance"
            )
        await _check_health(self, backend)
    except BaseException:
        await self._cleanup_owned_backend(backend, destroy=False)
        raise
    previous_id = handle.id if handle is not None else None
    if handle is None or handle.is_closed:
        handle = OpenSandboxHandle(backend)
        old_backend = None
    else:
        old_backend = handle._replace_backend(backend)
    self._handles[owner_key] = handle
    self._notifications.connected(owner_key, sandbox_id, previous_id)
    if old_backend is not None and old_backend is not backend:
        cleanup = asyncio.create_task(self._close_replaced_backend(handle, old_backend))
        self._track_cleanup_task(cleanup)
    # The handle owns the new connection; old cleanup must survive a failed
    # holder-registration response or cancellation.
    await self._availability.register(owner_key, claim, handle)
    return handle


async def recover_binding(
    self: OpenSandboxManager[KeyT],
    owner_key: str,
    claim: OpenSandboxOwnerClaim,
    *,
    purpose: OpenSandboxPurpose,
    reconnect: bool = False,
    allow_recreate: bool = True,
) -> OpenSandboxHandle:
    """Bound retries to one authoritative ID and preserve it on uncertain failures."""
    require_binding_purpose(claim.binding, purpose)
    binding = claim.binding
    if binding is None:
        raise OpenSandboxBackendUnavailableError(
            "No Sandbox is bound to this owner", context={"reason": "not_bound"}
        )
    sandbox_id = binding.sandbox_id
    handle = self._handles.get(owner_key)
    policy = self._recovery_policy
    attempts = 0
    failure: OpenSandboxBackendError | None = None
    reason: _RecoveryReason | None = None
    delay = policy.initial_delay
    connecting = False
    uncertain_connection = False
    deadline = asyncio.get_running_loop().time() + policy.timeout
    inherited_deadline = _connection_deadline.get()
    if inherited_deadline is not None:
        deadline = min(deadline, inherited_deadline)
    deadline_token = _connection_deadline.set(deadline)
    try:
        async with asyncio.timeout_at(deadline):
            for attempts in range(1, policy.max_attempts + 1):
                try:
                    if (
                        attempts == 1
                        and not reconnect
                        and handle is not None
                        and not handle.is_closed
                        and handle.id == sandbox_id
                        and handle._accepts_calls()
                        and self._availability.confirms_binding(owner_key, binding)
                    ):
                        await _check_health(self, handle)
                    else:
                        # A running binding may retain a closed local gate after
                        # failed registration or resume refresh. Reconnect that
                        # same instance; the gate is not a remote health failure
                        # and must never select recreation as its recovery action.
                        self._notifications.recovery_started(owner_key, sandbox_id)
                        connecting = True
                        handle = await _connect_existing(
                            self, owner_key, sandbox_id, handle, claim, purpose=purpose
                        )
                        connecting = False
                    reason = None
                    failure = None
                    break
                except OpenSandboxBackendError as error:
                    reason = _recovery_reason(error)
                    observed_reason = failure_reason(error)
                    self._notifications.unavailable(
                        owner_key,
                        sandbox_id,
                        observed_reason,
                        workspace_may_have_changed=connecting,
                    )
                    connecting = False
                    if reason is None:
                        self._notifications.failed(
                            owner_key, sandbox_id, observed_reason
                        )
                        raise
                    failure = error
                    if reason == "not_found" or attempts == policy.max_attempts:
                        break
                    self._notifications.recovering(
                        owner_key, sandbox_id, observed_reason
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, policy.max_delay)
                except OpenSandboxStateError as error:
                    self._notifications.failed(
                        owner_key, sandbox_id, failure_reason(error)
                    )
                    raise
            else:
                raise AssertionError("Recovery requires at least one attempt")
    except TimeoutError as error:
        # Client.connect owns late connection/initializer settlement after caller
        # cancellation. Its phase is uncertain here, so it cannot authorize remote
        # deletion. A typed client timeout completed before this deadline remains
        # eligible for the configured failure action.
        uncertain_connection = connecting
        reason = "timeout"
        failure = OpenSandboxBackendTimeoutError(
            "OpenSandbox recovery exceeded its time budget", cause=error
        )
        self._notifications.unavailable(
            owner_key,
            sandbox_id,
            OpenSandboxLifecycleReason.TIMEOUT,
            workspace_may_have_changed=connecting,
        )
    else:
        # The last attempt succeeded only when it cleared the previous failure.
        # A healthy cached handle and a verified reconnect share the same identity.
        if handle is not None and reason is None:
            self._notifications.available(owner_key, sandbox_id)
            await self._renew_backend(handle)
            return handle
    finally:
        _connection_deadline.reset(deadline_token)
    assert failure is not None
    if allow_recreate and not uncertain_connection and policy.on_failure == "recreate":
        self._notifications.recovering(owner_key, sandbox_id, failure_reason(failure))
        try:
            return await self._replace(
                owner_key, claim, handle, old_id=sandbox_id, purpose=purpose
            )
        except Exception as error:
            self._notifications.failed(owner_key, sandbox_id, failure_reason(error))
            raise
    self._notifications.failed(owner_key, sandbox_id, failure_reason(failure))
    raise OpenSandboxBackendUnavailableError(
        "OpenSandbox recovery failed; the existing binding was preserved",
        context={"reason": reason, "attempts": attempts},
        cause=failure,
    ) from failure
