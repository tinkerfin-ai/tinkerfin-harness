"""Test-only driver for framework-owned Messaging coordinator behavior."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging._messaging_ledger import (
    BackendRunHandle,
    PreparedRun,
    _MessagingLedger,
)
from tinkerfin_messaging.backend import FinalRunStatus, RunStatus
from tinkerfin_messaging.backend_contract import (
    CommittedMessagePage,
    CommittedMessageQuery,
    MessagingBackend,
    MessagingBackendSettings,
    MessagingChangeWait,
    MessagingStateQuery,
    MessagingStateSnapshot,
    MessagingTransition,
    MessagingTransitionResult,
    StreamGenerationPurge,
    StreamGenerationPurgeResult,
)
from tinkerfin_messaging.models import MessageEnvelope, RecoveryCheckpoint
from tinkerfin_messaging.redis import RedisBackend


class MessagingBackendHarness:
    """Exercise ledger behavior without defining a Backend or compatibility contract."""

    def __init__(self, storage_backend: MessagingBackend) -> None:
        self.storage_backend = storage_backend
        self._ledger = _MessagingLedger(storage_backend)

    @property
    def messaging_settings(self) -> MessagingBackendSettings:
        return self.storage_backend.messaging_settings

    @property
    def limits(self):
        return self._ledger.limits

    @property
    def retention_policy(self):
        return self._ledger.retention_policy

    @property
    def lease_renew_interval(self) -> float | None:
        return self._ledger.lease_renew_interval

    @property
    def lease_timeout(self) -> float | None:
        return self._ledger.lease_timeout

    async def prepare_messaging_storage(self) -> None:
        await self.storage_backend.prepare_messaging_storage()

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        return await self.storage_backend.commit_messaging_transition(transition)

    async def load_messaging_state(
        self,
        query: MessagingStateQuery,
    ) -> MessagingStateSnapshot:
        return await self.storage_backend.load_messaging_state(query)

    async def read_committed_messages(
        self,
        query: CommittedMessageQuery,
    ) -> CommittedMessagePage:
        return await self.storage_backend.read_committed_messages(query)

    async def wait_for_messaging_change(self, wait: MessagingChangeWait) -> None:
        await self.storage_backend.wait_for_messaging_change(wait)

    async def purge_stream_generation(
        self,
        purge: StreamGenerationPurge,
    ) -> StreamGenerationPurgeResult:
        return await self.storage_backend.purge_stream_generation(purge)

    async def prepare(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        codec: str,
        after: int | None,
        cancellable: bool,
        recoverable: bool,
        request_digest: str | None = None,
    ) -> PreparedRun:
        return await self._ledger.prepare(
            channel=channel,
            identity=identity,
            codec=codec,
            after=after,
            cancellable=cancellable,
            recoverable=recoverable,
            request_digest=request_digest,
        )

    async def append(
        self,
        handle: BackendRunHandle,
        *,
        message_id: str,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None = None,
    ) -> MessageEnvelope:
        return await self._ledger.append(
            handle,
            message_id=message_id,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
        )

    async def begin_settlement(self, handle: BackendRunHandle) -> bool:
        return await self._ledger.begin_settlement(handle)

    async def finish(
        self,
        handle: BackendRunHandle,
        *,
        status: FinalRunStatus,
        error: BaseException | None = None,
    ) -> None:
        await self._ledger.finish(handle, status=status, error=error)

    async def latest_seq(self, *, channel: str, identity: RunIdentity) -> int:
        return await self._ledger.latest_seq(channel=channel, identity=identity)

    async def get_run_status(
        self,
        *,
        channel: str,
        identity: RunIdentity,
    ) -> RunStatus:
        return await self._ledger.get_run_status(channel=channel, identity=identity)

    async def read(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]:
        return await self._ledger.read(
            channel=channel,
            identity=identity,
            after=after,
            limit=limit,
        )

    async def bind_follow(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        after: int,
    ) -> BackendRunHandle:
        return await self._ledger.bind_follow(
            channel=channel,
            identity=identity,
            after=after,
        )

    def follow(
        self,
        handle: BackendRunHandle,
        *,
        after: int,
    ) -> AsyncGenerator[MessageEnvelope, None]:
        return self._ledger.follow(handle, after=after)

    async def request_cancel(self, handle: BackendRunHandle) -> bool:
        return await self._ledger.request_cancel(handle)

    async def wait_for_cancel(self, handle: BackendRunHandle) -> bool:
        return await self._ledger.wait_for_cancel(handle)

    async def wait_finished(self, handle: BackendRunHandle) -> RunStatus:
        return await self._ledger.wait_finished(handle)

    async def failure(self, handle: BackendRunHandle) -> BaseException | None:
        return await self._ledger.failure(handle)

    async def renew(self, handle: BackendRunHandle) -> bool:
        return await self._ledger.renew(handle)

    async def delete_stream(self, *, channel: str, identity: RunIdentity) -> None:
        await self._ledger.delete_stream(channel=channel, identity=identity)


class RedisBackendHarness(MessagingBackendHarness):
    """Expose the test lifecycle facade over one concrete Redis Backend."""

    storage_backend: RedisBackend

    def __init__(self, storage_backend: RedisBackend) -> None:
        super().__init__(storage_backend)


__all__ = ["MessagingBackendHarness", "RedisBackendHarness"]
