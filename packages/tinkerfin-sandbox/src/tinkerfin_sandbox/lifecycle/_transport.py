"""Own SDK child requests without taking ownership of borrowed transports.

OpenSandbox 0.1.16 uses ``asyncio.gather`` for endpoint discovery. A failed
endpoint can leave its sibling running after the SDK call returns. Operation
scopes retain the actual request tasks through response-body consumption and
settle them before lifecycle ownership may move to another caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

import httpx
from opensandbox.transport import RetryAsyncTransport, RetryPolicy

from ..errors import OpenSandboxBackendError
from ._sql_tasks import select_failure


async def _join_owned_task(
    task: asyncio.Task[None],
    *,
    failure_label: str,
) -> None:
    """Settle one owned task before propagating cancellation of its waiter."""
    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            next_count = current.cancelling() if current is not None else 0
            if next_count > cancel_count:
                cancellation = cancellation or error
                cancel_count = next_count
                continue
            if task.done():
                break
            raise
        except BaseException:  # noqa: BLE001 - inspect the retained task below
            break
    task_error: BaseException | None = None
    try:
        task.result()
    except BaseException as error:  # noqa: BLE001 - preserve exact task outcome
        task_error = error
    if cancellation is not None:
        if task_error is not None:
            cancellation.add_note(
                f"{failure_label} also failed: "
                f"{type(task_error).__name__}: {task_error}"
            )
            raise select_failure(cancellation, task_error)
        raise cancellation.with_traceback(cancellation.__traceback__)
    if task_error is not None:
        raise task_error.with_traceback(task_error.__traceback__)


class _SDKOperation:
    """Retain one call's SDK children until cancellation and body reads settle."""

    def __init__(self) -> None:
        self.owner: asyncio.Task[object] | None = asyncio.current_task()
        self.requests: set[asyncio.Task[object]] = set()
        self.exited = asyncio.Event()
        self.settling = False
        self.settlement: asyncio.Task[None] | None = None

    def register(self) -> None:
        task: asyncio.Task[object] | None = asyncio.current_task()
        if task is not None and task is not self.owner:
            self.requests.add(task)
        if self.settling:
            # A child scheduled before SDK failure may reach its first request
            # after cleanup starts. It must not dispatch new remote work.
            raise asyncio.CancelledError

    async def _settle_requests(self) -> None:
        self.settling = True
        while self.requests:
            requests = tuple(self.requests)
            for task in requests:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            self.requests.difference_update(requests)

    async def settle(self) -> None:
        self.settling = True
        if not self.requests:
            return
        settlement = self.settlement
        if settlement is None or settlement.done():
            settlement = asyncio.create_task(
                self._settle_requests(), name="tinkerfin-sandbox-sdk-settlement"
            )
            self.settlement = settlement
        await _join_owned_task(settlement, failure_label="SDK request settlement")


class _SDKRequestTracker:
    """Associate HTTP children with a lifecycle call and drain them on close.

    Scopes are local to this client and inherited through ``ContextVar``. No
    event-loop task discovery or global task factory is used. The SDK's current
    endpoint operations reach the transport before awaiting network I/O; tracking
    their enclosing task also covers body reads after transport dispatch returns.
    """

    def __init__(self) -> None:
        self._current: ContextVar[_SDKOperation | None] = ContextVar(
            "tinkerfin_sandbox_sdk_operation", default=None
        )
        self._operations: set[_SDKOperation] = set()
        self._calls: set[asyncio.Event] = set()
        self._closing = False

    @contextmanager
    def owned_call(self) -> Generator[None]:
        """Keep resource handoff and cancellation reclamation ahead of close.

        A create/connect SDK task can finish before its public caller receives the
        result or assigns cancelled-result cleanup. Tracking the public call closes
        that handoff gap without treating its cleanup tasks as SDK-owned children.
        """
        if self._closing:
            raise OpenSandboxBackendError("OpenSandbox client is closed")
        finished = asyncio.Event()
        self._calls.add(finished)
        try:
            yield
        finally:
            finished.set()
            self._calls.discard(finished)

    def register_request(self) -> None:
        operation = self._current.get()
        if operation is not None:
            operation.register()

    @contextmanager
    def caller_work(self) -> Generator[None]:
        """Keep caller-owned initializer tasks outside SDK child ownership.

        The surrounding lifecycle operation still waits for the initializer itself.
        Tasks the initializer explicitly gives to an external owner inherit no SDK
        request scope and must not be reclaimed as SDK endpoint-discovery children.
        """
        token = self._current.set(None)
        try:
            yield
        finally:
            self._current.reset(token)

    @asynccontextmanager
    async def operation(self) -> AsyncGenerator[None]:
        """Keep failed SDK children inside the call that owns their resources."""
        if self._current.get() is not None:
            yield
            return
        if self._closing:
            raise OpenSandboxBackendError("OpenSandbox client is closed")
        operation = _SDKOperation()
        self._operations.add(operation)
        token = self._current.set(operation)
        try:
            yield
        finally:
            self._current.reset(token)
            try:
                await operation.settle()
            finally:
                operation.exited.set()
                if not operation.requests:
                    self._operations.discard(operation)

    async def aclose(self) -> None:
        """Wait for issued calls before their owned transport can be closed."""
        self._closing = True
        while self._calls:
            for finished in tuple(self._calls):
                await finished.wait()
        while self._operations:
            for operation in tuple(self._operations):
                await operation.exited.wait()
                await operation.settle()
                self._operations.discard(operation)

    def borrow(self, source: httpx.AsyncBaseTransport) -> httpx.AsyncBaseTransport:
        """Forward through a borrowed source while preserving SDK SSE unwrapping."""
        if isinstance(source, RetryAsyncTransport):
            return _TrackedRetryTransport(source, self)
        return _TrackedTransport(source, self)


class _TrackedTransport(httpx.AsyncBaseTransport):
    """Record SDK request ownership while leaving source lifetime with its owner."""

    def __init__(
        self, source: httpx.AsyncBaseTransport, tracker: _SDKRequestTracker
    ) -> None:
        self._source = source
        self._tracker = tracker

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._tracker.register_request()
        return await self._source.handle_async_request(request)

    async def aclose(self) -> None:
        # SDK adapters borrow this view; only the original owner closes source.
        return


class _TrackedRetryTransport(RetryAsyncTransport):
    """Keep the SDK's public retry/streaming distinction through tracking.

    ``opensandbox.transport.unwrap_retry_transport`` recognizes its public retry
    type. A generic outer transport would hide that type and replay SSE commands
    under an explicit retry policy. Normal requests delegate the original policy;
    ``inner`` exposes a tracked view of the original streaming transport. Neither
    view closes caller resources or reads private SDK policy attributes.
    """

    def __init__(
        self, source: RetryAsyncTransport, tracker: _SDKRequestTracker
    ) -> None:
        super().__init__(source, RetryPolicy.disabled(), owns_inner=False)
        self._source = source
        self._tracker = tracker
        self._stream_transport = _TrackedTransport(source.inner, tracker)

    @property
    def inner(self) -> httpx.AsyncBaseTransport:
        """Expose the same retry-free transport selected by the native SDK."""
        return self._stream_transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._tracker.register_request()
        return await self._source.handle_async_request(request)

    async def aclose(self) -> None:
        return
