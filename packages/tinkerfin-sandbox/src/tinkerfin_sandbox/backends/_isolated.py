"""Own bounded HTTP exchanges with one fixed execd isolation endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import Literal, TypeVar
from uuid import UUID

import httpx
from deepagents.backends.protocol import ExecuteResponse
from opensandbox.adapters.converter.event_node import EventNode
from opensandbox.adapters.sse import aiter_sse_events
from opensandbox.models.isolated import CreateIsolatedSessionRequest
from opensandbox.transport import unwrap_retry_transport
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError, field_validator

from ..errors import (
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxError,
    OpenSandboxFileTooLargeError,
    UnexpectedOpenSandboxBackendError,
)
from ._bounded_read import _BorrowedTransport, _close_response
from ._workspace_changes import _open_changes, _WorkspaceEvent
from .sdk import OpenSandboxBackend, _backend_call

_CONTROL_LIMIT = 64 * 1024
_EVENT_LIMIT = 32 * 1024 * 1024
_OUTPUT_LIMIT = 32 * 1024 * 1024
_FILE_LIMIT = 64 * 1024 * 1024
_SESSION_PATH = "/v1/isolated/session"
_ResultT = TypeVar("_ResultT")


async def _workspace_call(operation: Awaitable[_ResultT]) -> _ResultT:
    """Preserve declared package failures and translate untyped HTTP boundaries."""
    try:
        return await operation
    except (OpenSandboxError, FileNotFoundError, PermissionError, IsADirectoryError):
        raise
    except (TimeoutError, httpx.TimeoutException) as error:
        raise OpenSandboxBackendTimeoutError(
            "Workspace request timed out", cause=error
        ) from error
    except httpx.HTTPError as error:
        raise OpenSandboxBackendUnavailableError(
            "Workspace connection failed", cause=error
        ) from error
    except (TypeError, ValueError) as error:
        raise OpenSandboxBackendProtocolError(
            "Workspace returned an invalid response", cause=error
        ) from error
    except Exception as error:
        raise UnexpectedOpenSandboxBackendError(
            "Workspace operation failed", cause=error
        ) from error


def _uuid_text(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("isolation identities must be canonical UUID strings")
    return value


def _required_guarantee(value: object) -> bool:
    if value is not True:
        raise ValueError("workspace isolation requires an explicit true capability")
    return True


class _Capabilities(BaseModel):
    """Require the patched runtime guarantees before admitting a workspace."""

    model_config = ConfigDict(strict=True)

    available: Literal[True]
    client_session_ownership: Literal[True]
    namespace_exit_confirmation: Literal[True]
    rooted_filesystem: Literal[True]
    session_namespace: str

    _namespace_uuid = field_validator("session_namespace")(_uuid_text)
    _required_flags = field_validator(
        "available",
        "client_session_ownership",
        "namespace_exit_confirmation",
        "rooted_filesystem",
        mode="before",
    )(_required_guarantee)


class _CreateRequest(CreateIsolatedSessionRequest):
    """Preserve client ownership fields absent from OpenSandbox 0.1.16 models."""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str
    session_namespace: str

    _identity_uuid = field_validator("session_id", "session_namespace")(_uuid_text)


class _Created(BaseModel):
    model_config = ConfigDict(strict=True)

    session_id: str


class _Rejection(BaseModel):
    model_config = ConfigDict(strict=True)

    code: str


async def _read_body(response: httpx.Response, *, limit: int) -> bytes:
    """Retain at most the declared limit and a single transport chunk."""
    if response.headers.get("Content-Encoding", "identity") != "identity":
        raise OpenSandboxBackendProtocolError(
            "Workspace responses require identity encoding"
        )
    if not isinstance(response.stream, httpx.AsyncByteStream):
        raise OpenSandboxBackendProtocolError("Workspace returned a synchronous body")
    content = bytearray()
    async for chunk in response.stream:
        if len(chunk) > limit - len(content):
            raise OpenSandboxFileTooLargeError(
                "Workspace response exceeds the byte limit",
                context={"max_bytes": limit},
            )
        content.extend(chunk)
    return bytes(content)


class _BoundedEventStream(httpx.AsyncByteStream):
    """Bound incomplete SSE frames before the SDK parser can retain them."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if not isinstance(self._response.stream, httpx.AsyncByteStream):
            raise OpenSandboxBackendProtocolError(
                "Workspace returned a synchronous body"
            )
        frame_bytes = 0
        line_bytes = 0
        async for chunk in self._response.stream:
            for line in chunk.splitlines(keepends=True):
                frame_bytes += len(line)
                line_bytes += len(line)
                if frame_bytes > _EVENT_LIMIT:
                    raise OpenSandboxBackendProtocolError(
                        "Workspace command event exceeds the byte limit"
                    )
                if line.endswith(b"\n"):
                    if line_bytes in {1, 2} and line in {b"\n", b"\r\n"}:
                        frame_bytes = 0
                    line_bytes = 0
            yield chunk


class _WorkspaceConnection:
    """Borrow the parent transport without borrowing its command or file routes.

    Execution contexts retain the parent Handle lease until namespaced DELETE has
    confirmed namespace exit and remote file-lease drainage. File-change observers
    instead own a separate parent connection until their stream closes. This object
    owns only its HTTP client. Every exchange bypasses SDK retries and closes its
    response; neither response text nor authentication headers enter public errors.
    """

    def __init__(
        self, client: httpx.AsyncClient, *, sandbox_id: str, request_timeout: float
    ) -> None:
        self._client = client
        self._sandbox_id = sandbox_id
        self._namespace: str | None = None
        self._request_timeout = request_timeout

    @property
    def sandbox_id(self) -> str:
        """Return the fixed physical sandbox identity without a parent reference."""
        return self._sandbox_id

    @classmethod
    async def connect(cls, parent: OpenSandboxBackend) -> _WorkspaceConnection:
        """Fix endpoint, headers, and transport while the caller owns the connection."""
        sandbox = parent._sandbox
        endpoint = await _backend_call(
            "workspace endpoint", sandbox.get_endpoint(44772)
        )
        if not httpx.Headers(endpoint.headers).get("X-EXECD-ACCESS-TOKEN"):
            raise OpenSandboxBackendProtocolError(
                "Isolated workspaces require an authenticated parent endpoint",
                context={"reason": "parent_authentication_missing"},
            )
        config = sandbox.connection_config
        transport = config.transport
        client = httpx.AsyncClient(
            base_url=f"{config.protocol}://{endpoint.endpoint}",
            headers={
                "User-Agent": config.user_agent,
                **config.headers,
                **endpoint.headers,
                "Accept-Encoding": "identity",
            },
            timeout=config.request_timeout.total_seconds(),
            transport=(
                _BorrowedTransport(unwrap_retry_transport(transport))
                if transport is not None
                else None
            ),
            follow_redirects=False,
            trust_env=False,
        )
        return cls(
            client,
            sandbox_id=parent.id,
            request_timeout=config.request_timeout.total_seconds(),
        )

    @asynccontextmanager
    async def _response(
        self, request: httpx.Request, *, command: bool = False
    ) -> AsyncGenerator[httpx.Response, None]:
        async with asyncio.timeout(None if command else self._request_timeout):
            response = await self._client.send(request, stream=True)
            try:
                yield response
            finally:
                await _close_response(response)

    async def _accepted(
        self,
        response: httpx.Response,
        *,
        expected: tuple[int, ...] = (200,),
        file_operation: bool = False,
    ) -> None:
        if response.status_code in expected:
            return
        status = response.status_code
        if status == 409:
            raise OpenSandboxBackendProtocolError(
                "Workspace isolation ownership was rejected",
                context={"reason": "namespace_or_session_conflict", "status": status},
            )
        if file_operation and status == 404:
            content = await _read_body(response, limit=_CONTROL_LIMIT)
            rejection = _Rejection.model_validate_json(content)
            if rejection.code == "FILE_NOT_FOUND":
                raise FileNotFoundError("Workspace file was not found")
        if file_operation and status == 403:
            raise PermissionError("Workspace file access was denied")
        raise OpenSandboxBackendUnavailableError(
            "Workspace isolation request was rejected",
            context={"reason": "request_rejected", "status": status},
        )

    async def _control(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, JsonValue] | None = None,
        params: Mapping[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> bytes:
        request = self._client.build_request(method, path, json=body, params=params)
        async with self._response(request) as response:
            await self._accepted(response, expected=expected)
            return await _read_body(response, limit=_CONTROL_LIMIT)

    async def capabilities(self) -> str:
        """Validate safety capabilities and pin the daemon-instance nonce."""

        async def read() -> str:
            content = await self._control("GET", "/v1/isolated/capabilities")
            try:
                capabilities = _Capabilities.model_validate_json(content)
            except ValidationError as error:
                raise OpenSandboxBackendProtocolError(
                    "Workspace isolation guarantees are unavailable",
                    context={"reason": "capabilities_missing"},
                    cause=error,
                ) from error
            namespace = capabilities.session_namespace
            self._check_namespace(namespace)
            self._namespace = namespace
            return namespace

        return await _workspace_call(read())

    def _check_namespace(self, namespace: str) -> None:
        _uuid_text(namespace)
        if self._namespace is not None and namespace != self._namespace:
            raise OpenSandboxBackendProtocolError(
                "Workspace isolation instance has changed",
                context={"reason": "namespace_changed"},
            )

    async def start(self, request: Mapping[str, JsonValue]) -> None:
        """Submit a client-known identity once; the orchestrator owns failed starts."""

        async def create() -> None:
            parsed = _CreateRequest.model_validate(request)
            if self._namespace is None:
                raise OpenSandboxBackendProtocolError(
                    "Workspace capabilities must be verified before creation"
                )
            self._check_namespace(parsed.session_namespace)
            content = await self._control(
                "POST",
                _SESSION_PATH,
                body=parsed.model_dump(mode="json", exclude_none=True),
                expected=(201,),
            )
            created = _Created.model_validate_json(content)
            if created.session_id != parsed.session_id:
                raise OpenSandboxBackendProtocolError(
                    "Workspace creation returned a different session identity",
                    context={"reason": "session_changed"},
                )

        await _workspace_call(create())

    async def stop(self, session_id: str, namespace: str) -> None:
        """Confirm PID namespace exit and file drainage using the recorded nonce.

        A missing response, 404, or nonce conflict never proves termination. The
        patched execd DELETE is idempotent for a known identity within its instance.
        """

        async def remove() -> None:
            _uuid_text(session_id)
            self._check_namespace(namespace)
            await self._control(
                "DELETE",
                f"{_SESSION_PATH}/{session_id}",
                params={"session_namespace": namespace},
            )

        await _workspace_call(remove())

    async def run(
        self,
        session_id: str,
        *,
        command: str,
        environment: Mapping[str, str],
        timeout: int,
    ) -> ExecuteResponse:
        """Read one foreground command, requiring an explicit terminal event.

        OpenSandbox 0.1.16 isolated runs expose stdout plus either completion or an
        ExitError event, without command-init identifiers. Other runtime errors and
        incomplete streams require whole-session settlement by the backend.
        """
        _uuid_text(session_id)
        request = self._client.build_request(
            "POST",
            f"{_SESSION_PATH}/{session_id}/run",
            json={
                "code": command,
                "envs": dict(environment),
                "timeout_seconds": timeout,
            },
            timeout=httpx.Timeout(
                connect=self._client.timeout.connect,
                read=None,
                write=self._client.timeout.write,
                pool=self._client.timeout.pool,
            ),
        )
        async with self._response(request, command=True) as response:
            await self._accepted(response)
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise OpenSandboxBackendProtocolError(
                    "Workspace command streams require identity encoding"
                )
            stream_response = httpx.Response(
                response.status_code,
                headers=response.headers,
                request=response.request,
                stream=_BoundedEventStream(response),
            )
            output: list[str] = []
            output_bytes = 0
            exit_code: int | None = None
            async for event in aiter_sse_events(stream_response):
                node = EventNode.model_validate_json(event.data)
                if exit_code is not None:
                    raise OpenSandboxBackendProtocolError(
                        "Workspace command emitted data after its terminal event"
                    )
                if node.type in {"stdout", "stderr"}:
                    content = node.text or ""
                    if not content:
                        continue
                    output_bytes += len(content.encode("utf-8")) + bool(output)
                    if output_bytes > _OUTPUT_LIMIT:
                        raise OpenSandboxBackendProtocolError(
                            "Workspace command output exceeds the byte limit"
                        )
                    output.append(content)
                elif node.type == "execution_complete":
                    exit_code = 0
                elif node.type == "error" and node.error is not None:
                    if node.error.name != "ExitError":
                        raise OpenSandboxBackendProtocolError(
                            "Workspace command did not complete normally",
                            context={"reason": "runtime_error"},
                        )
                    exit_code = int(node.error.value or "")
                else:
                    raise OpenSandboxBackendProtocolError(
                        "Workspace command returned an unsupported event"
                    )
            if exit_code is None:
                raise OpenSandboxBackendProtocolError(
                    "Workspace command ended without terminal evidence"
                )
            return ExecuteResponse(output="\n".join(output), exit_code=exit_code)

    async def upload(self, session_id: str, path: str, content: bytes) -> None:
        """Upload once through the runtime's fixed workspace directory descriptor."""
        _uuid_text(session_id)
        metadata = json.dumps({"path": path, "mode": 644})
        request = self._client.build_request(
            "POST",
            f"{_SESSION_PATH}/{session_id}/files/upload",
            files=[
                ("metadata", ("metadata", metadata, "application/json")),
                (
                    "file",
                    (PurePosixPath(path).name, content, "application/octet-stream"),
                ),
            ],
        )
        async with self._response(request) as response:
            await self._accepted(response, file_operation=True)
            await _read_body(response, limit=_CONTROL_LIMIT)

    async def download(
        self, session_id: str, path: str, *, max_bytes: int = _FILE_LIMIT
    ) -> bytes:
        """Download a complete file with bounded retention and response ownership."""
        _uuid_text(session_id)
        request = self._client.build_request(
            "GET",
            f"{_SESSION_PATH}/{session_id}/files/download",
            params={"path": path},
        )
        async with self._response(request) as response:
            await self._accepted(response, file_operation=True)
            return await _read_body(response, limit=max_bytes)

    async def aclose(self) -> None:
        """Close only this local HTTP client; the parent transport remains borrowed."""
        await self._client.aclose()

    @asynccontextmanager
    async def changes(
        self, project: str
    ) -> AsyncGenerator[AsyncIterator[_WorkspaceEvent], None]:
        """Close the fixed collector stream before its borrowed transport can close."""
        async with _open_changes(
            self._client, project, request_timeout=self._request_timeout
        ) as events:
            yield events
