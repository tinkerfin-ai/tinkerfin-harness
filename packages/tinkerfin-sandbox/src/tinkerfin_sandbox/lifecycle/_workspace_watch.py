"""Own project change subscriptions independently of workspace execution leases."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, Literal, Self, TypeVar

from pydantic import JsonValue

from tinkerfin_notifications import (
    Notification,
    NotificationCapacityExceeded,
    NotificationError,
    Notifications,
    NotificationScope,
    NotificationSubscription,
    ResyncRequired,
)

from ..backends._isolated import _workspace_call, _WorkspaceConnection
from ..backends._workspace_changes import (
    _WorkspaceEvent,
    _WorkspaceReady,
    _WorkspaceResync,
)
from ..errors import (
    OpenSandboxBackendUnavailableError,
    OpenSandboxBusyError,
    OpenSandboxError,
    OpenSandboxManagerClosedError,
)
from ._purpose import require_binding_purpose
from ._sql_tasks import (
    TaskOutcome,
    capture,
    join_owned_task,
    run_owned_operation,
    select_failure,
)
from .availability import OpenSandboxAvailability
from .state import OpenSandboxBinding

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

_KeyT = TypeVar("_KeyT")
_Reason = Literal["overflow", "disconnected", "reconnected"]


class WorkspaceChange(StrEnum):
    """Advise that a project's file tree should be read again.

    Hints cover the whole project file root, can be coalesced, and contain no
    file content or operation history. They do not certify an atomic snapshot.
    """

    FILES_CHANGED = "files_changed"


@dataclass(frozen=True, slots=True)
class _WatchIdentity:
    """Fence delayed publications from previous parents, collectors, or projects."""

    binding: str
    source: str
    incarnation: str

    def details(self) -> dict[str, JsonValue]:
        return {
            "binding": self.binding,
            "source": self.source,
            "incarnation": self.incarnation,
        }

    @property
    def key(self) -> str:
        return hashlib.sha256(
            f"{self.binding}:{self.source}:{self.incarnation}".encode()
        ).hexdigest()


class _WatchReader(AsyncIterator[WorkspaceChange | ResyncRequired]):
    """Keep one coalesced root hint and one sticky resync for a single consumer."""

    def __init__(self) -> None:
        self._changed = False
        self._resync: ResyncRequired | None = None
        self._ended = False
        self._pulling = False
        self._ready = asyncio.Event()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> WorkspaceChange | ResyncRequired:
        if self._pulling:
            raise RuntimeError("A workspace watch supports one reader")
        self._pulling = True
        try:
            while True:
                if self._resync is not None:
                    resync, self._resync = self._resync, None
                    return resync
                if self._ended:
                    raise StopAsyncIteration
                if self._changed:
                    self._changed = False
                    return WorkspaceChange.FILES_CHANGED
                self._ready.clear()
                await self._ready.wait()
        finally:
            self._pulling = False

    def accept(self, change: WorkspaceChange | ResyncRequired) -> None:
        if self._ended:
            return
        if isinstance(change, ResyncRequired):
            self._resync = change
            self._changed = False
        else:
            self._changed = True
        self._ready.set()

    def finish(self) -> None:
        if not self._ended:
            self.accept(ResyncRequired("disconnected"))
            self._ended = True

    def close(self) -> None:
        self._resync = None
        self._changed = False
        self._ended = True
        self._ready.set()


class _WorkspaceSource(Generic[_KeyT]):
    """Own one worker's collector, subscription, connection, and child tasks.

    Notification registration precedes the ready handshake; buffered publications
    are inspected only after that handshake fixes all three source identities.
    Distinct topics preserve resync against change coalescing, and immutable keys
    prevent a delayed old source from overwriting a current source's pending hint.
    The fixed availability sequence also detects a pause and resume that both
    finish between heartbeats, without following the parent's next running period.
    """

    def __init__(
        self,
        watches: _WorkspaceWatches[_KeyT],
        owner_key: str,
        project: str,
        binding: OpenSandboxBinding,
        availability: OpenSandboxAvailability | None,
    ) -> None:
        self.watches = watches
        self.owner_key = owner_key
        self.project = project
        self.binding = binding
        self.availability = availability
        self.readers: set[_WatchReader] = set()
        self.identity: _WatchIdentity | None = None
        self.scope = NotificationScope(
            "tinkerfin.sandbox.workspace",
            hashlib.sha256(owner_key.encode()).hexdigest(),
        )
        self.changed_topic = f"workspace.{project}.changed"
        self.resync_topic = f"workspace.{project}.resync"
        self.ready = asyncio.Event()
        self.failure: OpenSandboxError | None = None
        self.cleanup_failure: BaseException | None = None
        self.closing = False
        self._cancel_requested = False
        self.task = asyncio.create_task(
            capture(self._run()), name="tinkerfin-workspace-watch"
        )
        self._settlement: asyncio.Task[TaskOutcome[None]] | None = None

    def broadcast(self, change: WorkspaceChange | ResyncRequired) -> None:
        for reader in self.readers:
            reader.accept(change)

    def terminate(self) -> None:
        """Wake consumers before cancellation; no consumer acknowledgement is needed."""
        if self.closing:
            return
        self.closing = True
        for reader in self.readers:
            reader.finish()
        self.ready.set()
        if not self.task.done():
            self._cancel_requested = True
            self.task.cancel()

    async def _join(self) -> None:
        outcomes = await asyncio.gather(self.task, return_exceptions=True)
        failure = outcomes[0]
        if isinstance(failure, BaseException) and not (
            isinstance(failure, asyncio.CancelledError) and self._cancel_requested
        ):
            raise failure
        if self.cleanup_failure is not None:
            raise self.cleanup_failure

    async def stop(self) -> None:
        self.terminate()
        if self._settlement is None:
            self._settlement = asyncio.create_task(
                capture(self._join()), name="tinkerfin-workspace-watch-settlement"
            )
        await join_owned_task(self._settlement)

    async def _run(self) -> None:
        """Separate advisory source loss from failure to release owned resources."""
        resources = AsyncExitStack()
        failure: BaseException | None = None
        try:
            outcome = await capture(_workspace_call(self._connected(resources)))
            if isinstance(outcome, OpenSandboxError):
                self.failure = outcome
            elif isinstance(outcome, BaseException):
                failure = outcome
        finally:
            self.closing = True
            for reader in self.readers:
                reader.finish()
            self.ready.set()
            cleanup = await capture(
                run_owned_operation(
                    _workspace_call(resources.aclose()),
                    task_name="tinkerfin-workspace-watch-resource-close",
                )
            )
            if isinstance(cleanup, BaseException):
                self.cleanup_failure = cleanup
                primary = failure if failure is not None else self.failure
                failure = (
                    cleanup if primary is None else select_failure(primary, cleanup)
                )
        if failure is not None:
            raise failure

    async def _connected(self, resources: AsyncExitStack) -> None:
        manager = self.watches.manager
        await self.watches.start()
        async with asyncio.timeout(
            manager._client.config.connect_timeout.total_seconds()
        ):
            try:
                subscription = await resources.enter_async_context(
                    self.watches.notifications.subscribe(
                        scope=self.scope,
                        topics={self.changed_topic, self.resync_topic},
                    )
                )
            except NotificationError as error:
                raise OpenSandboxBackendUnavailableError(
                    "Workspace notifications are unavailable", cause=error
                ) from error
            parent = await manager._client._connect_observer(self.binding.sandbox_id)
            resources.push_async_callback(parent.aclose)
            connection = await _WorkspaceConnection.connect(parent)
            resources.push_async_callback(connection.aclose)
            events = await resources.enter_async_context(
                connection.changes(self.project)
            )
            ready = await anext(events)
            assert isinstance(ready, _WorkspaceReady)
            self.identity = _WatchIdentity(
                hashlib.sha256(
                    f"{self.binding.sandbox_id}:{self.binding.generation}".encode()
                ).hexdigest(),
                ready.source,
                ready.incarnation,
            )
        readers = (
            asyncio.create_task(
                capture(self._collect(events)), name="tinkerfin-workspace-change-source"
            ),
            asyncio.create_task(
                capture(self._receive(subscription)),
                name="tinkerfin-workspace-change-delivery",
            ),
        )
        self.ready.set()
        failure: BaseException | None = None
        try:
            outcome = await capture(
                asyncio.wait(readers, return_when=asyncio.FIRST_COMPLETED)
            )
            if isinstance(outcome, BaseException):
                failure = outcome
        finally:
            cancelled: set[asyncio.Task[TaskOutcome[None]]] = set()
            for task in readers:
                if not task.done():
                    cancelled.add(task)
                    task.cancel()
            outcomes = await asyncio.gather(*readers, return_exceptions=True)
            for task, outcome in zip(readers, outcomes, strict=True):
                if not isinstance(outcome, BaseException) or (
                    isinstance(outcome, asyncio.CancelledError) and task in cancelled
                ):
                    continue
                failure = (
                    outcome if failure is None else select_failure(failure, outcome)
                )
        if failure is not None:
            raise failure

    async def _publish(self, reason: _Reason | None = None) -> None:
        identity = self.identity
        assert identity is not None
        details = identity.details()
        if reason is not None:
            details["reason"] = reason
        try:
            await self.watches.notifications.publish(
                Notification(
                    scope=self.scope,
                    topic=self.changed_topic if reason is None else self.resync_topic,
                    key=identity.key,
                    details=details,
                )
            )
        except NotificationError as error:
            self.broadcast(
                ResyncRequired(
                    "overflow"
                    if isinstance(error, NotificationCapacityExceeded)
                    else "disconnected"
                )
            )

    async def _collect(self, events: AsyncIterator[_WorkspaceEvent]) -> None:
        async for event in events:
            if event.type == "changed":
                await self._publish()
            elif isinstance(event, _WorkspaceResync):
                reason: _Reason = (
                    "reconnected"
                    if event.reason == "topology"
                    else "overflow"
                    if event.reason == "overflow"
                    else "disconnected"
                )
                if reason == "disconnected":
                    for reader in self.readers:
                        reader.finish()
                    await self._publish(reason)
                    return
                self.broadcast(ResyncRequired(reason))
                await self._publish(reason)
            elif event.type == "closed":
                return
            elif event.type == "heartbeat":
                manager = self.watches.manager
                async with asyncio.timeout(
                    manager._client.config.connect_timeout.total_seconds()
                ):
                    binding = await manager._state.read_binding(self.owner_key)
                    availability = await manager._state.read_availability(
                        self.owner_key
                    )
                if binding != self.binding or availability != self.availability:
                    return

    async def _receive(self, subscription: NotificationSubscription) -> None:
        identity = self.identity
        assert identity is not None
        async for notification in subscription:
            if isinstance(notification, ResyncRequired):
                self.broadcast(notification)
                continue
            if any(
                notification.details.get(key) != value
                for key, value in identity.details().items()
            ):
                continue
            if notification.topic == self.changed_topic:
                self.broadcast(WorkspaceChange.FILES_CHANGED)
            else:
                reason = notification.details.get("reason")
                if isinstance(reason, str) and (
                    reason == "overflow"
                    or reason == "disconnected"
                    or reason == "reconnected"
                ):
                    self.broadcast(ResyncRequired(reason))


class _WorkspaceWatches(Generic[_KeyT]):
    """Keep observation outside execution leases and bound all local consumers.

    Failed resource closure remains owned until manager shutdown reports its
    outcome. Retained sources consume the same capacity as active sources, so
    repeated failed cleanup cannot accumulate unbounded resources or failures.
    """

    def __init__(
        self, manager: OpenSandboxManager[_KeyT], notifications: Notifications | None
    ) -> None:
        self.manager = manager
        self.notifications = (
            notifications if notifications is not None else Notifications()
        )
        self._owned_notifications = notifications is None
        self._startup: asyncio.Task[Notifications] | None = None
        self._sources: dict[tuple[str, str], _WorkspaceSource[_KeyT]] = {}
        self._all_sources: set[_WorkspaceSource[_KeyT]] = set()
        self._reader_count = 0
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise OpenSandboxManagerClosedError("Workspace watching is closed")
        if self._owned_notifications:
            if self._startup is None:
                self._startup = asyncio.create_task(
                    self.notifications.__aenter__(),
                    name="tinkerfin-workspace-notifications",
                )
            await asyncio.shield(self._startup)

    @asynccontextmanager
    async def watch(
        self, owner_key: str, project: str
    ) -> AsyncGenerator[AsyncIterator[WorkspaceChange | ResyncRequired], None]:
        """Register under the owner fence, then observe without holding that fence."""
        reader = _WatchReader()
        source: _WorkspaceSource[_KeyT] | None = None
        failure: BaseException | None = None
        try:
            async with (
                self.manager._operation(),
                self.manager._claim_owner(owner_key) as claim,
            ):
                if self._closed:
                    raise OpenSandboxManagerClosedError("Workspace watching is closed")
                binding = claim.binding
                require_binding_purpose(binding, "workspaces")
                if binding is None:
                    raise OpenSandboxBackendUnavailableError(
                        "No Sandbox is bound to this owner",
                        context={"reason": "not_bound"},
                    )
                availability = await self.manager._availability.require_running(
                    owner_key
                )
                if self._reader_count >= self.notifications.limits.max_subscriptions:
                    raise OpenSandboxBusyError("Workspace watch capacity is exhausted")
                key = owner_key, project
                current = self._sources.get(key)
                if current is not None and (
                    current.closing
                    or current.binding != binding
                    or current.availability != availability
                ):
                    current.terminate()
                    current = None
                if current is None:
                    if (
                        len(self._all_sources)
                        >= self.notifications.limits.max_subscriptions
                    ):
                        raise OpenSandboxBusyError(
                            "Workspace watch resource capacity is exhausted"
                        )
                    current = _WorkspaceSource(
                        self, owner_key, project, binding, availability
                    )
                    self._sources[key] = current
                    self._all_sources.add(current)
                source = current
                source.readers.add(reader)
                self._reader_count += 1
            await source.ready.wait()
            if source.identity is None:
                if source.failure is not None:
                    raise source.failure
                raise OpenSandboxBackendUnavailableError(
                    "Workspace watching stopped before becoming ready"
                )
            yield reader
        except BaseException as error:
            failure = error
            raise
        finally:
            reader.close()
            if source is not None:
                source.readers.discard(reader)
                self._reader_count -= 1
                if not source.readers:
                    outcome = await capture(source.stop())
                    if isinstance(outcome, BaseException):
                        failure = (
                            outcome
                            if failure is None
                            else select_failure(failure, outcome)
                        )
                    if source.cleanup_failure is None and source.task.done():
                        self._all_sources.discard(source)
                    key = source.owner_key, source.project
                    if self._sources.get(key) is source:
                        del self._sources[key]
            if failure is not None:
                raise failure

    def stop(self, owner_key: str, project: str | None = None) -> None:
        """End matching streams after the caller has sealed new resource admission."""
        for source in self._all_sources:
            if source.owner_key == owner_key and (
                project is None or source.project == project
            ):
                source.terminate()

    async def aclose(self) -> None:
        """Wake every reader and settle observation before the manager waits for Runs."""
        self._closed = True
        failure: BaseException | None = None
        sources = tuple(self._all_sources)
        for source in sources:
            source.terminate()
        for source in sources:
            outcome = await capture(source.stop())
            if isinstance(outcome, BaseException):
                failure = (
                    outcome if failure is None else select_failure(failure, outcome)
                )
        self._sources.clear()
        self._all_sources.clear()
        if self._startup is not None:
            outcome = await capture(_workspace_call(asyncio.shield(self._startup)))
            if isinstance(outcome, BaseException):
                failure = (
                    outcome if failure is None else select_failure(failure, outcome)
                )
        if self._owned_notifications:
            outcome = await capture(_workspace_call(self.notifications.aclose()))
            if isinstance(outcome, BaseException):
                failure = (
                    outcome if failure is None else select_failure(failure, outcome)
                )
        if failure is not None:
            raise failure
