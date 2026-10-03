"""Internal structural contracts used by the OpenSandbox lifecycle manager."""

from collections.abc import Awaitable, Mapping
from typing import Protocol, TypeVar

from ..backends.sdk import OpenSandboxBackend
from ..errors import OpenSandboxError, UnexpectedOpenSandboxBackendError
from ..models import (
    OpenSandboxConfig,
    OpenSandboxDiagnosticContent,
    OpenSandboxPurpose,
    OpenSandboxRuntimeInfo,
)

__all__ = ["_SandboxClientBoundary"]


class _SandboxClient(Protocol):
    """Create, reconnect, inspect, destroy, and close remote sandboxes."""

    config: OpenSandboxConfig

    async def create(
        self,
        *,
        purpose: OpenSandboxPurpose = "commands",
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend: ...

    async def connect(
        self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
    ) -> OpenSandboxBackend: ...

    async def _connect_observer(self, sandbox_id: str) -> OpenSandboxBackend: ...

    async def inspect(
        self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
    ) -> OpenSandboxRuntimeInfo: ...

    async def get_runtime_info(self, sandbox_id: str) -> OpenSandboxRuntimeInfo: ...

    async def pause(self, sandbox_id: str) -> None: ...

    async def resume(self, sandbox_id: str) -> None: ...

    async def get_diagnostic_logs(
        self, sandbox_id: str, *, scope: str = "container"
    ) -> OpenSandboxDiagnosticContent: ...

    async def get_diagnostic_events(
        self, sandbox_id: str, *, scope: str = "runtime"
    ) -> OpenSandboxDiagnosticContent: ...

    async def destroy(self, sandbox_id: str) -> None: ...

    async def aclose(self) -> None: ...


_ResultT = TypeVar("_ResultT")


async def _call_client(
    client: _SandboxClient,
    operation: str,
    awaitable: Awaitable[_ResultT],
) -> _ResultT:
    if getattr(client, "_tinkerfin_error_boundary", False):
        return await awaitable
    try:
        return await awaitable
    except OpenSandboxError:
        raise
    except Exception as error:
        translated = UnexpectedOpenSandboxBackendError(
            f"OpenSandbox client {operation} failed",
            diagnostic_context={"operation": operation},
            cause=error,
        )
        raise translated from error


class _SandboxClientBoundary(_SandboxClient):
    """Enforce the client failure contract for replaceable implementations."""

    def __init__(self, client: _SandboxClient) -> None:
        self._client = client
        try:
            self.config = client.config
        except Exception as error:
            translated = UnexpectedOpenSandboxBackendError(
                "OpenSandbox client config lookup failed",
                diagnostic_context={"operation": "config"},
                cause=error,
            )
            raise translated from error

    async def create(
        self,
        *,
        purpose: OpenSandboxPurpose = "commands",
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        return await _call_client(
            self._client,
            "create",
            self._client.create(purpose=purpose, metadata=metadata),
        )

    async def connect(
        self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
    ) -> OpenSandboxBackend:
        return await _call_client(
            self._client,
            "connect",
            self._client.connect(sandbox_id, purpose=purpose),
        )

    async def inspect(
        self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
    ) -> OpenSandboxRuntimeInfo:
        return await _call_client(
            self._client,
            "inspect",
            self._client.inspect(sandbox_id, purpose=purpose),
        )

    async def _connect_observer(self, sandbox_id: str) -> OpenSandboxBackend:
        return await _call_client(
            self._client,
            "observe workspace",
            self._client._connect_observer(sandbox_id),
        )

    async def destroy(self, sandbox_id: str) -> None:
        await _call_client(
            self._client,
            "destroy",
            self._client.destroy(sandbox_id),
        )

    async def get_runtime_info(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        return await _call_client(
            self._client, "runtime info", self._client.get_runtime_info(sandbox_id)
        )

    async def pause(self, sandbox_id: str) -> None:
        await _call_client(self._client, "pause", self._client.pause(sandbox_id))

    async def resume(self, sandbox_id: str) -> None:
        await _call_client(self._client, "resume", self._client.resume(sandbox_id))

    async def get_diagnostic_logs(
        self, sandbox_id: str, *, scope: str = "container"
    ) -> OpenSandboxDiagnosticContent:
        return await _call_client(
            self._client,
            "diagnostic logs",
            self._client.get_diagnostic_logs(sandbox_id, scope=scope),
        )

    async def get_diagnostic_events(
        self, sandbox_id: str, *, scope: str = "runtime"
    ) -> OpenSandboxDiagnosticContent:
        return await _call_client(
            self._client,
            "diagnostic events",
            self._client.get_diagnostic_events(sandbox_id, scope=scope),
        )

    async def aclose(self) -> None:
        await _call_client(self._client, "close", self._client.aclose())
