"""Real Redis persistence, cross-worker, cancellation, and fencing contracts."""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import ClassVar, TypeVar, cast
from uuid import uuid4

import pytest
from backend_harness import RedisBackendHarness
from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.typing import KeyT, StreamIdT

from tinkerfin import RunIdentity
from tinkerfin_contracts import ThreadIdentity
from tinkerfin_messaging import (
    CodecMismatch,
    MessageSubscription,
    Messaging,
    MessagingBackendProtocolError,
    MessagingLimits,
    MessagingQuotaExceeded,
    MessagingRetentionPolicy,
    RecoveryCheckpoint,
    RunAlreadyActive,
    StreamDeleteConflict,
    StreamDeleted,
    StreamExpired,
    _redis_journal,
    _redis_scripts,
)
from tinkerfin_messaging import (
    RedisBackend as RedisStorageBackend,
)
from tinkerfin_messaging._messaging_ledger import PreparedRun
from tinkerfin_messaging.backend_contract import (
    CommittedMessageQuery,
    MessagingChangeWait,
    MessagingStateQuery,
    MessagingTransition,
    StreamGenerationPurge,
)
from tinkerfin_messaging.testing import verify_messaging_backend

_RedisStreamEntry = tuple[bytes, dict[bytes, bytes]]
_XReadResponse = list[tuple[bytes, list[_RedisStreamEntry]]]
_RedisT = TypeVar("_RedisT", bound=Redis)


class RedisBackend(RedisBackendHarness):
    """Construct the public Redis storage backend behind the test lifecycle harness."""

    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str = "tinkerfin-messaging",
        lease_ttl: float = 15.0,
        poll_interval: float = 0.1,
        limits: MessagingLimits = MessagingLimits(),
        retention_policy: MessagingRetentionPolicy = MessagingRetentionPolicy(),
    ) -> None:
        super().__init__(
            RedisStorageBackend(
                client,
                key_prefix=key_prefix,
                producer_lease_seconds=lease_ttl,
                generation_cleanup_retry_seconds=poll_interval,
                limits=limits,
                retention_policy=retention_policy,
            )
        )


_REDIS_SCRIPT_DIGESTS = {
    "_DUE_EXPIRATIONS_SCRIPT": "f06bfc6798e6f5a5b711abd2c3ddd5f4d478a501a1343e22afb74530cd41900d",
    "_APPEND_SCRIPT": "864c6093045f48abab4fc9d77ead3d1518ee3a6bc956c285f536cf05279080c9",
    "_BEGIN_DELETE_SCRIPT": "6b52a1e68a9183d72ca6902d042ab80164b1f148a8aa628e8b447dca5364cf32",
    "_BEGIN_EXPIRATION_SCRIPT": "bd522a5be66f40c1e2a049019071a322a817a5890d6253360659d99dd1735a11",
    "_BEGIN_SETTLEMENT_SCRIPT": "554621573ed7a50c3b5ea0be5bb7166f051d2fdba213db0493423a84f5423414",
    "_CANCEL_SCRIPT": "c4d82de1405dffc62a13ec7efbc11790e5bcab99c746b02fb8cb397ad59fce09",
    "_DELETE_BATCH_SCRIPT": "c018e231de54762346bc095581f23d9bca9107667273102dae4b7d51e20bbf10",
    "_FINALIZE_DELETE_SCRIPT": "aa107e4445ee6e4e224ac80fe80cbb51c14db815f5d21a1d07698e02c5f833bf",
    "_FINISH_SCRIPT": "63e951aacdd317030aa9e776a3eeeab2a5632689d598a2c512fa08b2609cee27",
    "_MESSAGING_STATE_SNAPSHOT_SCRIPT": "40d5362e08cf6dfbad004a3fb1b7d9b3293b1e600012bc23ca624d3fc2f2c429",
    "_PREPARE_SCRIPT": "0d788e29cfde7bd5f904d1674748c3c6fe46d691dfca8ad3b400caadb9449655",
    "_READ_CONTROL_SCRIPT": "0cda87dabd7a112208ade6abe7f2ed806f9ac216b6ed0913c5fc194223a0703c",
    "_RENEW_SCRIPT": "90a2c24ed5f4e9c64f84a41fa6b4bc69e03206c5df48c5f75ec4b66be6c62113",
    "_RUN_SNAPSHOT_SCRIPT": "d6ecf2ef4b7486a08139d96d0a8728b54c1a1345c7f0e4d1fbd2f2f061a4c3e4",
}


def test_redis_lua_scripts_remain_byte_stable() -> None:
    for name, expected in _REDIS_SCRIPT_DIGESTS.items():
        script = getattr(_redis_scripts, name)
        assert isinstance(script, str)
        assert hashlib.sha256(script.encode()).hexdigest() == expected


def test_redis_message_signature_uses_the_current_unversioned_domain() -> None:
    identity = RunIdentity(namespace="test", thread_id="conversation-1", run_id="run-1")

    assert (
        _redis_journal._message_signature(
            identity=identity,
            codec="text",
            payload=b"payload",
            checkpoint=None,
        )
        == "c42f38656e51cdde504a1f0bf4b07b7ce060380dfcffe3a9e20b1131fb9e0866"
    )
    assert (
        _redis_journal._message_signature(
            identity=identity,
            codec="text",
            payload=b"payload",
            checkpoint=RecoveryCheckpoint(
                position=b"42",
                last_message_id="message-1",
            ),
        )
        == "9a6fa1ec2cfccb0bdeaa1a14bed2e49746169b7391148dd042368835b4f58b34"
    )


def _identity(
    *,
    thread_id: str = "conversation-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


def _redis_client(
    client_type: type[_RedisT],
    redis_url: str,
    *,
    max_connections: int | None = None,
    socket_timeout: float | None = 5,
) -> _RedisT:
    return cast(
        _RedisT,
        client_type.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=socket_timeout,
            max_connections=max_connections,
        ),
    )


class _TextCodec:
    codec_id: ClassVar[str] = "test.redis-text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _TextSseCodec(_TextCodec):
    def render(self, *, seq: int, payload: str) -> bytes:
        return f"id: {seq}\ndata: {payload}\n\n".encode()


class _Source:
    def __init__(
        self,
        *items: str,
        release: asyncio.Event | None = None,
        after_release: tuple[str, ...] = (),
    ) -> None:
        self.items = items
        self.release = release
        self.after_release = after_release
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for item in self.items:
                yield item
            if self.release is not None:
                await self.release.wait()
            for item in self.after_release:
                yield item

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()
        self.closed.set()


class _GatedEvalRedis(Redis):
    """Pause immediately before or after one real atomic Redis script."""

    eval_entered: asyncio.Event
    eval_release: asyncio.Event
    eval_returned: asyncio.Event
    return_release: asyncio.Event
    append_committed: asyncio.Event
    append_release: asyncio.Event
    gate_next_eval_before = False
    gate_next_eval_after = False
    gate_next_state_snapshot_before = False
    gate_next_state_snapshot_after = False
    gate_append_response = False

    async def execute_command(self, *args: object, **options: object) -> object:
        command = args[0] if args else ""
        command_name = command.decode() if isinstance(command, bytes) else str(command)
        is_eval = command_name.casefold() == "eval"
        script = args[1] if len(args) > 1 else ""
        script_text = script.decode() if isinstance(script, bytes) else str(script)
        is_state_snapshot = is_eval and "ACTIVE_RUN_CHANGED" in script_text
        if is_eval and (
            self.gate_next_eval_before
            or (is_state_snapshot and self.gate_next_state_snapshot_before)
        ):
            self.gate_next_eval_before = False
            self.gate_next_state_snapshot_before = False
            self.eval_entered.set()
            await self.eval_release.wait()
        response = super().execute_command(*args, **options)
        result = await cast(Awaitable[object], response)
        if is_eval and self.gate_append_response and "'APPENDED'" in script_text:
            self.gate_append_response = False
            self.append_committed.set()
            await self.append_release.wait()
        if is_eval and (
            self.gate_next_eval_after
            or (is_state_snapshot and self.gate_next_state_snapshot_after)
        ):
            self.gate_next_eval_after = False
            self.gate_next_state_snapshot_after = False
            self.eval_returned.set()
            await self.return_release.wait()
        return result


class _PublicConfigurationRedis:
    def __init__(self, *, decode_responses: bool) -> None:
        self._decode_responses = decode_responses

    def get_connection_kwargs(self) -> dict[str, object]:
        return {"decode_responses": self._decode_responses}


class _CommandCountingRedis(Redis):
    """Count real Redis commands without replacing their behavior."""

    command_counts: Counter[str]

    def client(self) -> _CommandCountingRedis:
        client = super().client()
        assert isinstance(client, _CommandCountingRedis)
        client.command_counts = self.command_counts
        return client

    async def execute_command(self, *args: object, **options: object) -> object:
        if args:
            command = args[0]
            command_name = (
                command.decode() if isinstance(command, bytes) else str(command)
            )
            self.command_counts[command_name.lower()] += 1
        response = super().execute_command(*args, **options)
        return await cast(Awaitable[object], response)


class _ConnectionTrackingRedis(Redis):
    """Retain real single-connection children for lifecycle assertions."""

    blocking_children: list[_ConnectionTrackingRedis]

    def client(self) -> _ConnectionTrackingRedis:
        client = super().client()
        assert isinstance(client, _ConnectionTrackingRedis)
        client.blocking_children = self.blocking_children
        self.blocking_children.append(client)
        return client


class _GatedCloseRedis(_ConnectionTrackingRedis):
    """Hold the pinned client's release to exercise concurrent caller cancellation."""

    reading: asyncio.Event
    closing: asyncio.Event
    close_release: asyncio.Event
    fail_close: bool

    def client(self) -> _GatedCloseRedis:
        client = super().client()
        assert isinstance(client, _GatedCloseRedis)
        client.reading = self.reading
        client.closing = self.closing
        client.close_release = self.close_release
        client.fail_close = self.fail_close
        return client

    async def execute_command(self, *args: object, **options: object) -> object:
        if args and str(args[0]).casefold() == "xread":
            self.reading.set()
        return await super().execute_command(*args, **options)

    async def aclose(self, close_connection_pool: bool | None = None) -> None:
        if self.single_connection_client:
            self.closing.set()
            await self.close_release.wait()
        await super().aclose(close_connection_pool)
        if self.single_connection_client and self.fail_close:
            raise RedisError("pinned close diagnostic")


class _GatedXreadRedis(Redis):
    """Pause before one real XREAD reaches Redis."""

    xread_entered: asyncio.Event
    xread_release: asyncio.Event
    gate_next_xread = False

    def client(self) -> _GatedXreadRedis:
        client = super().client()
        assert isinstance(client, _GatedXreadRedis)
        client.xread_entered = self.xread_entered
        client.xread_release = self.xread_release
        client.gate_next_xread = self.gate_next_xread
        self.gate_next_xread = False
        return client

    async def xread(
        self,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> _XReadResponse:
        if self.gate_next_xread:
            self.gate_next_xread = False
            self.xread_entered.set()
            await self.xread_release.wait()
        response = super().xread(
            streams,
            count=count,
            block=block,
        )
        return await cast(Awaitable[_XReadResponse], response)


async def _data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


async def _delete_prefix(client: Redis, prefix: str) -> None:
    cursor = 0
    pattern = f"{prefix}:*"
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=pattern, count=200)
        if keys:
            await client.unlink(*keys)
        if cursor == 0:
            return


def _redis_text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def test_redis_backend_uses_the_public_client_configuration_boundary() -> None:
    binary_client = cast(Redis, _PublicConfigurationRedis(decode_responses=False))
    text_client = cast(Redis, _PublicConfigurationRedis(decode_responses=True))

    assert RedisBackend(binary_client)
    with pytest.raises(ValueError, match="decode_responses=False"):
        RedisBackend(text_client)


@pytest.fixture
async def redis_backends(
    redis_url: str,
) -> AsyncGenerator[
    tuple[RedisBackendHarness, RedisBackendHarness, Redis],
    None,
]:
    first_client = _redis_client(Redis, redis_url)
    second_client = _redis_client(Redis, redis_url)
    cleanup_client = _redis_client(Redis, redis_url)
    prefix = f"tfmsg:test:{uuid4().hex}"
    try:
        try:
            assert await cast(Awaitable[bool], first_client.ping()) is True
            assert await cast(Awaitable[bool], second_client.ping()) is True
        except (OSError, RedisError, TimeoutError) as error:
            pytest.fail(
                f"real Redis PING failed without exposing credentials: {type(error).__name__}"
            )
        yield (
            RedisBackend(
                first_client,
                key_prefix=prefix,
                lease_ttl=0.6,
                poll_interval=0.05,
            ),
            RedisBackend(
                second_client,
                key_prefix=prefix,
                lease_ttl=0.6,
                poll_interval=0.05,
            ),
            cleanup_client,
        )
    finally:
        await _delete_prefix(cleanup_client, prefix)
        await asyncio.gather(
            first_client.aclose(),
            second_client.aclose(),
            cleanup_client.aclose(),
        )


@pytest.fixture
async def counting_redis_backend(
    redis_url: str,
) -> AsyncGenerator[
    tuple[RedisBackendHarness, _CommandCountingRedis, str],
    None,
]:
    """Provide one real backend whose command boundary remains observable."""

    client = _redis_client(_CommandCountingRedis, redis_url)
    client.command_counts = Counter()
    prefix = f"tfmsg:counting:{uuid4().hex}"
    try:
        try:
            assert await cast(Awaitable[bool], client.ping()) is True
        except (OSError, RedisError, TimeoutError) as error:
            pytest.fail(
                "real Redis PING failed without exposing credentials: "
                f"{type(error).__name__}"
            )
        yield (
            RedisBackend(
                client,
                key_prefix=prefix,
                lease_ttl=3,
                poll_interval=0.1,
            ),
            client,
            prefix,
        )
    finally:
        await _delete_prefix(client, prefix)
        await client.aclose()


async def test_channel_follow_binds_generation_across_redis_backend_instances(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    owner_backend, observer_backend, _ = redis_backends
    async with (
        Messaging(backend=owner_backend) as owner_messaging,
        Messaging(backend=observer_backend) as observer_messaging,
    ):
        owner_channel = owner_messaging.channel(name="events", codec=_TextCodec())
        observer_channel = observer_messaging.channel(name="events", codec=_TextCodec())
        old = await owner_channel.wrap(
            _Source("old"),
            identity=_identity(thread_id="thread-1"),
            after=0,
        )
        assert [message.data async for message in old] == ["old"]
        stale = await observer_channel.follow(
            identity=_identity(thread_id="thread-1"),
            after=0,
        )

        await owner_channel.delete_stream(identity=_identity(thread_id="thread-1"))
        replacement = await owner_channel.wrap(
            _Source("new"),
            identity=_identity(thread_id="thread-1"),
            after=0,
        )
        assert [message.data async for message in replacement] == ["new"]

        with pytest.raises(StreamDeleted):
            await anext(aiter(stale))


async def test_public_backend_verifier_accepts_real_redis(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    backend, _, _ = redis_backends

    @asynccontextmanager
    async def open_backend() -> AsyncIterator[RedisStorageBackend]:
        yield backend.storage_backend

    await verify_messaging_backend(open_backend)


@pytest.fixture
async def gated_xread_backends(
    redis_url: str,
) -> AsyncGenerator[
    tuple[RedisBackendHarness, RedisBackendHarness, _GatedXreadRedis],
    None,
]:
    """Provide separate waiting and state-changing clients for one XREAD race."""

    waiting_client = _redis_client(_GatedXreadRedis, redis_url)
    actor_client = _redis_client(Redis, redis_url)
    waiting_client.xread_entered = asyncio.Event()
    waiting_client.xread_release = asyncio.Event()
    prefix = f"tfmsg:gated-xread:{uuid4().hex}"
    try:
        try:
            assert await cast(Awaitable[bool], waiting_client.ping()) is True
            assert await cast(Awaitable[bool], actor_client.ping()) is True
        except (OSError, RedisError, TimeoutError) as error:
            pytest.fail(
                "real Redis PING failed without exposing credentials: "
                f"{type(error).__name__}"
            )
        yield (
            RedisBackend(
                waiting_client,
                key_prefix=prefix,
                lease_ttl=3,
                poll_interval=1,
            ),
            RedisBackend(
                actor_client,
                key_prefix=prefix,
                lease_ttl=3,
                poll_interval=0.1,
            ),
            waiting_client,
        )
    finally:
        waiting_client.xread_release.set()
        await _delete_prefix(actor_client, prefix)
        await asyncio.gather(waiting_client.aclose(), actor_client.aclose())


@pytest.fixture
async def gated_redis_backend(
    redis_url: str,
) -> AsyncGenerator[
    tuple[RedisBackendHarness, _GatedEvalRedis],
    None,
]:
    client = _redis_client(_GatedEvalRedis, redis_url)
    client.eval_entered = asyncio.Event()
    client.eval_release = asyncio.Event()
    client.eval_returned = asyncio.Event()
    client.return_release = asyncio.Event()
    client.append_committed = asyncio.Event()
    client.append_release = asyncio.Event()
    prefix = f"tfmsg:gated-follow:{uuid4().hex}"
    try:
        try:
            assert await cast(Awaitable[bool], client.ping()) is True
        except (OSError, RedisError, TimeoutError) as error:
            pytest.fail(
                f"real Redis PING failed without exposing credentials: {type(error).__name__}"
            )
        yield (
            RedisBackend(client, key_prefix=prefix, poll_interval=0.05),
            client,
        )
    finally:
        client.eval_release.set()
        client.return_release.set()
        client.append_release.set()
        await _delete_prefix(client, prefix)
        await client.aclose()


async def test_real_redis_replays_commits_across_backend_instances(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    owner, follower, _ = redis_backends
    prepared = await owner.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await owner.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"persisted",
    )
    await owner.finish(prepared.handle, status="completed")

    attached = await follower.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    replay = [message async for message in follower.follow(attached.handle, after=0)]

    assert attached.is_owner is False
    assert [message.payload for message in replay] == [b"persisted"]
    assert replay[0].seq == 1


async def test_real_redis_retention_expires_data_and_preserves_generation_tombstone(
    redis_url: str,
) -> None:
    first_client = _redis_client(Redis, redis_url)
    second_client = _redis_client(Redis, redis_url)
    prefix = f"tfmsg:retention:{uuid4().hex}"
    policy = MessagingRetentionPolicy.expire_after(3600)
    first_backend = RedisBackend(
        first_client,
        key_prefix=prefix,
        lease_ttl=1,
        poll_interval=0.01,
        retention_policy=policy,
    )
    second_backend = RedisBackend(
        second_client,
        key_prefix=prefix,
        lease_ttl=1,
        poll_interval=0.01,
        retention_policy=policy,
    )
    try:
        first = await first_backend.prepare(
            channel="events",
            identity=_identity(run_id="retained-run"),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await first_backend.append(
            first.handle,
            message_id="retained-message",
            codec="test.bytes.v1",
            payload=b"retained",
        )
        await first_backend.finish(first.handle, status="completed")
        deployment_base = f"{prefix}:{{{hashlib.sha256(prefix.encode()).hexdigest()}}}"
        channel_scope = hashlib.sha256(b"events").hexdigest()
        stream_scope = hashlib.sha256(
            first.handle.identity.thread.model_dump_json(by_alias=True).encode()
        ).hexdigest()
        stream_base = f"{deployment_base}:channel:{channel_scope}:stream:{stream_scope}"
        control_key = f"{stream_base}:control"
        assert await first_client.exists(control_key) == 1
        # Expire the exact seeded generation without depending on wall-clock delays.
        async with first_client.pipeline(transaction=True) as pipeline:
            pipeline.hset(control_key, "retention_deadline_ms", "1")
            pipeline.zadd(f"{deployment_base}:expirations", {control_key: 1})
            await pipeline.execute()

        with pytest.raises(StreamExpired) as expired:
            await second_backend.read(
                channel="events",
                identity=_identity(run_id="retained-run"),
            )
        assert expired.value.generation == 1

        generation_base = f"{stream_base}:generation:1"
        remaining = await second_client.keys(f"{generation_base}:*")
        assert remaining == [f"{generation_base}:tombstone".encode()]
        assert await second_client.get(f"{generation_base}:tombstone") == b"expired"

        with pytest.raises(StreamExpired):
            await second_backend.prepare(
                channel="events",
                identity=_identity(run_id="replacement"),
                codec="test.bytes.v1",
                after=1,
                cancellable=False,
                recoverable=False,
            )
        replacement = await second_backend.prepare(
            channel="events",
            identity=_identity(run_id="replacement"),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        assert replacement.handle.generation == 2
        with pytest.raises(StreamExpired):
            await anext(first_backend.follow(first.handle, after=0))
        await second_backend.finish(replacement.handle, status="completed")
    finally:
        await _delete_prefix(second_client, prefix)
        await asyncio.gather(first_client.aclose(), second_client.aclose())


async def test_real_redis_rejects_cross_worker_retention_mismatch(
    redis_url: str,
) -> None:
    client = _redis_client(Redis, redis_url)
    prefix = f"tfmsg:retention-mismatch:{uuid4().hex}"
    enabled = RedisBackend(
        client,
        key_prefix=prefix,
        retention_policy=MessagingRetentionPolicy.expire_after(30),
    )
    disabled = RedisBackend(
        client,
        key_prefix=prefix,
        retention_policy=MessagingRetentionPolicy.disabled(),
    )
    try:
        prepared = await enabled.prepare(
            channel="events",
            identity=_identity(run_id="enabled"),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await enabled.finish(prepared.handle, status="completed")

        with pytest.raises(MessagingBackendProtocolError, match="invalid protocol"):
            await disabled.prepare(
                channel="events",
                identity=_identity(run_id="disabled"),
                codec="test.bytes.v1",
                after=0,
                cancellable=False,
                recoverable=False,
            )
    finally:
        await _delete_prefix(client, prefix)
        await client.aclose()


@pytest.mark.parametrize(
    ("message_id", "checkpoint"),
    [
        pytest.param("x" * 1025, None, id="overlong-message-id"),
        pytest.param(
            "message-1",
            RecoveryCheckpoint(
                position=b"invalid-position",
                last_message_id="different-message",
            ),
            id="mismatched-checkpoint",
        ),
    ],
)
async def test_real_redis_rejects_invalid_append_before_lua_or_state_change(
    counting_redis_backend: tuple[RedisBackendHarness, _CommandCountingRedis, str],
    message_id: str,
    checkpoint: RecoveryCheckpoint | None,
) -> None:
    """Moving validation after EVAL must change this complete state snapshot."""

    backend, client, prefix = counting_redis_backend
    prepared = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=True,
    )
    channel_scope = hashlib.sha256(b"events").hexdigest()
    stream_digest = hashlib.sha256(
        prepared.handle.identity.thread.model_dump_json(by_alias=True).encode()
    ).hexdigest()
    run_digest = hashlib.sha256(b"run-1").hexdigest()
    message_digest = hashlib.sha256(message_id.encode()).hexdigest()
    generation_base = (
        f"{prefix}:{{{hashlib.sha256(prefix.encode()).hexdigest()}}}:channel:{channel_scope}"
        f":stream:{stream_digest}:generation:1"
    )
    meta_key = f"{generation_base}:meta"
    run_key = f"{generation_base}:run:{run_digest}"
    messages_key = f"{generation_base}:messages"
    dedupe_key = f"{generation_base}:message:{message_digest}"
    index_key = f"{generation_base}:index"

    async def durable_state() -> tuple[
        int,
        bytes | None,
        dict[bytes, bytes],
        int,
        frozenset[bytes],
    ]:
        return (
            await cast(Awaitable[int], client.xlen(messages_key)),
            await cast(Awaitable[bytes | None], client.hget(meta_key, "seq")),
            await cast(
                Awaitable[dict[bytes, bytes]],
                client.hgetall(run_key),
            ),
            await cast(Awaitable[int], client.exists(dedupe_key)),
            frozenset(await cast(Awaitable[set[bytes]], client.smembers(index_key))),
        )

    before = await durable_state()
    assert before[1] == b"0"
    assert before[2][b"run"] == prepared.handle.identity.run_id.encode()
    assert before[2][b"status"] == b"running"
    assert before[4]
    client.command_counts.clear()

    with pytest.raises(ValueError):
        await backend.append(
            prepared.handle,
            message_id=message_id,
            codec="test.bytes.v1",
            payload=b"must-not-commit",
            checkpoint=checkpoint,
        )

    eval_calls = client.command_counts["eval"]
    after = await durable_state()
    assert eval_calls == 0
    assert after == before
    await backend.finish(prepared.handle, status="completed")


async def test_real_redis_cancelled_consumer_closes_its_pinned_follow_client(
    redis_url: str,
) -> None:
    """A response-task cancellation must close the XREAD client without a retry."""

    client = _redis_client(_ConnectionTrackingRedis, redis_url)
    client.blocking_children = []
    prefix = f"tfmsg:cancelled-consumer:{uuid4().hex}"
    backend = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=30,
        poll_interval=1,
    )
    prepared: PreparedRun | None = None
    subscription: MessageSubscription[str] | None = None
    consumer: asyncio.Task[None] | None = None
    try:
        async with Messaging(backend=backend) as messaging:
            channel = messaging.channel(name="events", codec=_TextSseCodec())
            prepared = await backend.prepare(
                channel="events",
                identity=_identity(),
                codec=_TextSseCodec.codec_id,
                after=0,
                cancellable=False,
                recoverable=False,
            )
            await backend.append(
                prepared.handle,
                message_id="first",
                codec=_TextSseCodec.codec_id,
                payload=b"first",
            )
            subscription = await channel.follow(identity=_identity(), after=0)
            body = subscription.to_sse()
            delivered = asyncio.Event()

            async def consume() -> None:
                async for _frame in body:
                    delivered.set()

            consumer = asyncio.create_task(
                consume(),
                name="test-messaging-cancelled-sse-consumer",
            )
            await asyncio.wait_for(delivered.wait(), timeout=1)
            async with asyncio.timeout(1):
                while not (
                    client.blocking_children
                    and client.blocking_children[-1].connection is not None
                ):
                    await asyncio.sleep(0)

            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

            assert client.blocking_children
            assert all(child.connection is None for child in client.blocking_children)
            await subscription.aclose()
            await backend.finish(prepared.handle, status="completed")
    finally:
        if consumer is not None and not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        if subscription is not None:
            await subscription.aclose()
        await _delete_prefix(client, prefix)
        await client.aclose()


@pytest.mark.parametrize("transition", ["cancel", "finish"])
async def test_real_redis_signal_closes_the_snapshot_to_xread_gap(
    gated_xread_backends: tuple[
        RedisBackendHarness,
        RedisBackendHarness,
        _GatedXreadRedis,
    ],
    transition: str,
) -> None:
    """A state change before XREAD is sent must remain durably observable."""

    waiter, actor, client = gated_xread_backends
    prepared = await actor.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    client.gate_next_xread = True
    waiting = asyncio.create_task(
        waiter.wait_for_cancel(prepared.handle)
        if transition == "cancel"
        else waiter.wait_finished(prepared.handle)
    )
    try:
        await asyncio.wait_for(client.xread_entered.wait(), timeout=0.5)
        if transition == "cancel":
            assert await actor.request_cancel(prepared.handle) is True
        else:
            await actor.finish(prepared.handle, status="completed")
        client.xread_release.set()
        result = await asyncio.wait_for(waiting, timeout=0.5)
        assert result is True if transition == "cancel" else result == "completed"
    finally:
        client.xread_release.set()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    if transition == "cancel":
        assert await actor.begin_settlement(prepared.handle) is True
        await actor.finish(prepared.handle, status="cancelled")


async def test_real_redis_state_change_before_snapshot_is_immediately_visible(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    owner, observer, _ = redis_backends
    prepared = await owner.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=True,
        recoverable=False,
    )

    assert await owner.request_cancel(prepared.handle) is True
    assert (
        await asyncio.wait_for(
            observer.wait_for_cancel(prepared.handle),
            timeout=0.2,
        )
        is True
    )
    assert await owner.begin_settlement(prepared.handle) is True
    await owner.finish(prepared.handle, status="cancelled")
    assert (
        await asyncio.wait_for(observer.wait_finished(prepared.handle), timeout=0.2)
        == "cancelled"
    )


async def test_real_redis_cancellation_cleanup_can_read_current_run_state(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    backend, _, _ = redis_backends
    await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    entered = asyncio.Event()
    observed = asyncio.Event()

    async def observe_during_cleanup() -> None:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            snapshot = await backend.storage_backend.load_messaging_state(
                MessagingStateQuery(channel="events", identity=_identity())
            )
            assert snapshot.target_run is not None
            assert snapshot.target_run.status == "running"
            observed.set()

    waiting = asyncio.create_task(observe_during_cleanup())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        waiting.cancel("close after observing current state")
        with pytest.raises(asyncio.CancelledError, match="close after observing"):
            await asyncio.wait_for(waiting, timeout=1)
        assert observed.is_set()
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.parametrize("fail_close", [False, True])
async def test_real_redis_repeated_wait_cancellation_settles_pinned_client_close(
    redis_url: str,
    fail_close: bool,
) -> None:
    client = _redis_client(_GatedCloseRedis, redis_url, max_connections=1)
    client.blocking_children = []
    client.reading = asyncio.Event()
    client.closing = asyncio.Event()
    client.close_release = asyncio.Event()
    client.fail_close = fail_close
    prefix = f"tfmsg:repeat-close:{uuid4().hex}"
    backend = RedisBackend(client, key_prefix=prefix, lease_ttl=3)
    waiting: asyncio.Task[None] | None = None
    try:
        prepared = await backend.prepare(
            channel="events",
            identity=_identity(),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        assert prepared.handle.generation is not None
        page = await backend.storage_backend.read_committed_messages(
            CommittedMessageQuery(
                channel="events",
                identity=_identity(),
                generation=prepared.handle.generation,
                after_sequence=0,
                through_sequence=None,
                limit=1,
            )
        )
        waiting = asyncio.create_task(
            backend.storage_backend.wait_for_messaging_change(
                MessagingChangeWait(
                    channel="events",
                    identity=_identity(),
                    generation=prepared.handle.generation,
                    after=page.change_cursor,
                    timeout_seconds=3,
                )
            )
        )
        await asyncio.wait_for(client.reading.wait(), timeout=1)
        waiting.cancel("detach requested")
        await asyncio.wait_for(client.closing.wait(), timeout=1)
        connection = client.blocking_children[-1].connection
        assert connection is not None
        waiting.cancel("caller cancelled again")
        await asyncio.sleep(0)
        assert not waiting.done()
        client.close_release.set()
        with pytest.raises(asyncio.CancelledError, match="detach requested") as raised:
            await asyncio.wait_for(waiting, timeout=1)
        if fail_close:
            assert any(
                "blocking client close" in note for note in raised.value.__notes__
            )
        assert connection.socket_timeout == 5
        assert all(child.connection is None for child in client.blocking_children)
        assert await cast(Awaitable[bool], client.ping()) is True
    finally:
        client.close_release.set()
        if waiting is not None and not waiting.done():
            waiting.cancel()
        if waiting is not None:
            await asyncio.gather(waiting, return_exceptions=True)
        await _delete_prefix(client, prefix)
        await client.aclose()


async def test_real_redis_cancellation_settles_an_inflight_snapshot(
    gated_redis_backend: tuple[RedisBackendHarness, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    prepared = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    client.gate_next_eval_before = True
    waiting = asyncio.create_task(backend.wait_for_cancel(prepared.handle))
    try:
        await asyncio.wait_for(client.eval_entered.wait(), timeout=0.5)
        waiting.cancel("caller cancelled during the Redis snapshot")
        await asyncio.sleep(0)
        assert not waiting.done()

        client.eval_release.set()
        with pytest.raises(
            asyncio.CancelledError,
            match="caller cancelled during the Redis snapshot",
        ):
            await asyncio.wait_for(waiting, timeout=0.5)
        assert await cast(Awaitable[bool], client.ping()) is True
        await backend.finish(prepared.handle, status="completed")
    finally:
        client.eval_release.set()
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_real_redis_state_snapshot_remains_coherent_before_a_later_append(
    gated_redis_backend: tuple[RedisBackendHarness, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    identity = _identity()
    prepared = await backend.prepare(
        channel="events",
        identity=identity,
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    client.gate_next_state_snapshot_after = True
    loading = asyncio.create_task(
        backend.load_messaging_state(
            MessagingStateQuery(channel="events", identity=identity)
        )
    )
    try:
        await asyncio.wait_for(client.eval_returned.wait(), timeout=0.5)
        envelope = await backend.append(
            prepared.handle,
            message_id="message-1",
            codec="test.bytes.v1",
            payload=b"value",
        )
        client.return_release.set()
        snapshot = await asyncio.wait_for(loading, timeout=0.5)

        assert envelope.seq == 1
        assert snapshot.stream is not None
        assert snapshot.target_run is not None
        assert snapshot.stream.latest_sequence == snapshot.target_run.end_sequence == 0
    finally:
        client.return_release.set()
        if not loading.done():
            loading.cancel()
            await asyncio.gather(loading, return_exceptions=True)
        await backend.finish(prepared.handle, status="completed")


async def test_real_redis_state_snapshot_observes_an_append_that_precedes_it(
    gated_redis_backend: tuple[RedisBackendHarness, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    identity = _identity()
    prepared = await backend.prepare(
        channel="events",
        identity=identity,
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    client.gate_next_state_snapshot_before = True
    loading = asyncio.create_task(
        backend.load_messaging_state(
            MessagingStateQuery(channel="events", identity=identity)
        )
    )
    try:
        await asyncio.wait_for(client.eval_entered.wait(), timeout=0.5)
        envelope = await backend.append(
            prepared.handle,
            message_id="message-1",
            codec="test.bytes.v1",
            payload=b"value",
        )
        client.eval_release.set()
        snapshot = await asyncio.wait_for(loading, timeout=0.5)

        assert snapshot.stream is not None
        assert snapshot.target_run is not None
        assert (
            snapshot.stream.latest_sequence
            == snapshot.target_run.end_sequence
            == envelope.seq
        )
        assert snapshot.observed_at >= envelope.created_at
    finally:
        client.eval_release.set()
        if not loading.done():
            loading.cancel()
            await asyncio.gather(loading, return_exceptions=True)
        await backend.finish(prepared.handle, status="completed")


async def test_real_redis_state_load_uses_one_atomic_evidence_command(
    counting_redis_backend: tuple[RedisBackendHarness, _CommandCountingRedis, str],
) -> None:
    backend, client, _ = counting_redis_backend
    identity = _identity()
    prepared = await backend.prepare(
        channel="events",
        identity=identity,
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    client.command_counts.clear()

    snapshot = await backend.load_messaging_state(
        MessagingStateQuery(channel="events", identity=identity)
    )

    assert snapshot.target_run is not None
    assert client.command_counts["eval"] == 2
    for command in ("time", "hgetall", "exists", "xrange"):
        assert client.command_counts[command] == 0
    await backend.finish(prepared.handle, status="completed")


async def test_real_redis_workers_bind_channel_codec_atomically(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    first, second, _ = redis_backends

    async def prepare(
        backend: RedisBackendHarness,
        *,
        identity: RunIdentity,
        codec: str,
    ):
        return await backend.prepare(
            channel="shared-channel",
            identity=identity,
            codec=codec,
            after=0,
            cancellable=False,
            recoverable=False,
        )

    outcomes = await asyncio.gather(
        prepare(
            first,
            identity=_identity(thread_id="stream-a", run_id="run-a"),
            codec="test.codec-a.v1",
        ),
        prepare(
            second,
            identity=_identity(thread_id="stream-b", run_id="run-b"),
            codec="test.codec-b.v1",
        ),
        return_exceptions=True,
    )

    owners = [outcome for outcome in outcomes if isinstance(outcome, PreparedRun)]
    mismatches = [outcome for outcome in outcomes if isinstance(outcome, CodecMismatch)]
    assert len(owners) == 1
    assert len(mismatches) == 1
    owner_backend = first if outcomes[0] is owners[0] else second
    await owner_backend.finish(owners[0].handle, status="completed")


async def test_real_redis_persists_channel_and_stream_metadata_separately(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    backend, _, client = redis_backends
    prepared_runs: list[PreparedRun] = []
    for index in (1, 2):
        prepared = await backend.prepare(
            channel="events",
            identity=_identity(
                thread_id=f"stream-{index}",
                run_id=f"run-{index}",
            ),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await backend.append(
            prepared.handle,
            message_id=f"message-{index}",
            codec="test.bytes.v1",
            payload=f"payload-{index}".encode(),
        )
        await backend.finish(prepared.handle, status="completed")
        prepared_runs.append(prepared)

    keys = [key async for key in client.scan_iter(match="tfmsg:test:*")]
    decoded_keys = [key.decode() for key in keys]
    channel_keys = [key for key in decoded_keys if key.endswith(":channel")]
    control_keys = [key for key in decoded_keys if key.endswith(":control")]
    stream_meta_keys = [key for key in decoded_keys if key.endswith(":meta")]
    run_keys = [key for key in decoded_keys if ":generation:1:run:" in key]
    message_stream_keys = [key for key in decoded_keys if key.endswith(":messages")]
    signal_stream_keys = [key for key in decoded_keys if key.endswith(":signals")]
    index_keys = [key for key in decoded_keys if key.endswith(":index")]
    dedupe_keys = [key for key in decoded_keys if ":generation:1:message:" in key]

    assert len(channel_keys) == 1
    assert len(control_keys) == 2
    assert len(stream_meta_keys) == 2
    assert len(run_keys) == 2
    assert len(message_stream_keys) == 2
    assert len(signal_stream_keys) == 2
    assert len(index_keys) == 2
    assert len(dedupe_keys) == 2
    hash_tags = {key.split("{", 1)[1].split("}", 1)[0] for key in decoded_keys}
    assert len(hash_tags) == 1

    channel_meta = await cast(
        Awaitable[dict[bytes, bytes]], client.hgetall(channel_keys[0])
    )
    assert channel_meta == {
        b"channel": b"events",
        b"codec": b"test.bytes.v1",
        b"max_checkpoint_bytes": b"1048576",
        b"max_message_payload_bytes": b"16777216",
        b"retention_ms": b"0",
        b"max_thread_messages": b"100000",
        b"max_thread_payload_bytes": b"1073741824",
        b"max_total_bytes": b"1073741824",
        b"max_total_records": b"100000",
    }
    controls = [
        await cast(Awaitable[dict[bytes, bytes]], client.hgetall(key))
        for key in control_keys
    ]
    assert {control[b"stream"] for control in controls} == {
        _identity(thread_id=thread).thread.model_dump_json(by_alias=True).encode()
        for thread in ("stream-1", "stream-2")
    }
    assert all(control[b"channel"] == b"events" for control in controls)
    assert all(control[b"generation"] == b"1" for control in controls)
    assert all(control[b"state"] == b"active" for control in controls)
    assert all(control[b"signal_seq"] == b"1" for control in controls)
    stream_metadata = [
        await cast(Awaitable[dict[bytes, bytes]], client.hgetall(key))
        for key in stream_meta_keys
    ]
    assert {metadata[b"stream"] for metadata in stream_metadata} == {
        _identity(thread_id=thread).thread.model_dump_json(by_alias=True).encode()
        for thread in ("stream-1", "stream-2")
    }
    assert all(metadata[b"channel"] == b"events" for metadata in stream_metadata)
    assert all(metadata[b"generation"] == b"1" for metadata in stream_metadata)
    assert all(metadata[b"seq"] == b"1" for metadata in stream_metadata)
    assert all(metadata[b"payload_bytes"] == b"9" for metadata in stream_metadata)

    run_metadata = [
        await cast(Awaitable[dict[bytes, bytes]], client.hgetall(key))
        for key in run_keys
    ]
    assert {metadata[b"status"] for metadata in run_metadata} == {b"completed"}
    assert {metadata[b"run"] for metadata in run_metadata} == {b"run-1", b"run-2"}
    assert all(metadata[b"lease_renew_count"] == b"0" for metadata in run_metadata)
    assert all(
        int(metadata[b"lease_last_success_seconds"]) > 0 for metadata in run_metadata
    )
    assert all(
        0 <= int(metadata[b"lease_last_success_microseconds"]) < 1_000_000
        for metadata in run_metadata
    )
    for key in index_keys:
        indexed = {
            _redis_text(member)
            for member in await cast(Awaitable[set[bytes]], client.smembers(key))
        }
        assert len(indexed) == 5
        assert all(":generation:1:" in member for member in indexed)
    for key in message_stream_keys:
        entries = await client.xrange(key)
        assert entries is not None
        assert len(entries) == 1
        identifier, fields = entries[0]
        assert fields is not None
        assert identifier == b"1-0"
        assert fields[b"codec"] == b"test.bytes.v1"
    for key in signal_stream_keys:
        entries = await client.xrange(key)
        assert entries is not None
        assert len(entries) == 1
        identifier, fields = entries[0]
        assert fields is not None
        assert identifier == b"1-0"
        assert fields[b"kind"] == b"finish"
        assert fields[b"generation"] == b"1"
        assert fields[b"run"] in {b"run-1", b"run-2"}


async def test_real_redis_enforces_thread_quotas_after_idempotency(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    _, _, client = redis_backends
    message_prefix = f"tfmsg:quota-messages:{uuid4().hex}"
    payload_prefix = f"tfmsg:quota-payload:{uuid4().hex}"
    try:
        message_backend = RedisBackend(
            client,
            key_prefix=message_prefix,
            limits=MessagingLimits(
                max_message_payload_bytes=4,
                max_checkpoint_bytes=2,
                max_thread_messages=2,
                max_thread_payload_bytes=6,
            ),
        )
        prepared = await message_backend.prepare(
            channel="events",
            identity=_identity(),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        first = await message_backend.append(
            prepared.handle,
            message_id="first",
            codec="test.bytes.v1",
            payload=b"1234",
        )
        await message_backend.append(
            prepared.handle,
            message_id="second",
            codec="test.bytes.v1",
            payload=b"12",
        )
        assert (
            await message_backend.append(
                prepared.handle,
                message_id="first",
                codec="test.bytes.v1",
                payload=b"1234",
            )
            == first
        )
        with pytest.raises(MessagingQuotaExceeded) as message_error:
            await message_backend.append(
                prepared.handle,
                message_id="third",
                codec="test.bytes.v1",
                payload=b"",
            )
        assert message_error.value.resource == "thread_messages"
        assert (
            await message_backend.latest_seq(
                channel="events",
                identity=_identity(),
            )
            == 2
        )

        payload_backend = RedisBackend(
            client,
            key_prefix=payload_prefix,
            limits=MessagingLimits(
                max_message_payload_bytes=4,
                max_checkpoint_bytes=2,
                max_thread_messages=10,
                max_thread_payload_bytes=5,
            ),
        )
        payload_run = await payload_backend.prepare(
            channel="events",
            identity=_identity(thread_id="payload-thread"),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await payload_backend.append(
            payload_run.handle,
            message_id="first",
            codec="test.bytes.v1",
            payload=b"1234",
        )
        with pytest.raises(MessagingQuotaExceeded) as payload_error:
            await payload_backend.append(
                payload_run.handle,
                message_id="second",
                codec="test.bytes.v1",
                payload=b"12",
            )
        assert payload_error.value.resource == "thread_payload_bytes"
        assert (
            await payload_backend.latest_seq(
                channel="events",
                identity=_identity(thread_id="payload-thread"),
            )
            == 1
        )
    finally:
        await _delete_prefix(client, message_prefix)
        await _delete_prefix(client, payload_prefix)


async def test_real_redis_rejects_limits_mismatch_without_mutation(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:limits-mismatch:{uuid4().hex}"
    first_limits = MessagingLimits(
        max_message_payload_bytes=4,
        max_checkpoint_bytes=2,
        max_thread_messages=10,
        max_thread_payload_bytes=20,
    )
    second_limits = MessagingLimits(
        max_message_payload_bytes=5,
        max_checkpoint_bytes=2,
        max_thread_messages=10,
        max_thread_payload_bytes=20,
    )
    first = RedisBackend(client, key_prefix=prefix, limits=first_limits)
    second = RedisBackend(client, key_prefix=prefix, limits=second_limits)
    try:
        prepared = await first.prepare(
            channel="events",
            identity=_identity(),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await first.finish(prepared.handle, status="completed")
        records = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        hashes_before = {
            key: await cast(Awaitable[dict[bytes, bytes]], client.hgetall(key))
            for key in records
            if await cast(Awaitable[bytes], client.type(key)) == b"hash"
        }

        with pytest.raises(
            MessagingBackendProtocolError,
            match="invalid protocol response",
        ) as captured:
            await second.prepare(
                channel="events",
                identity=_identity(run_id="run-2"),
                codec="test.bytes.v1",
                after=0,
                cancellable=False,
                recoverable=False,
            )

        assert "different MessagingLimits" in str(
            captured.value.diagnostic_context["detail"]
        )
        assert {
            key: await cast(Awaitable[dict[bytes, bytes]], client.hgetall(key))
            for key in hashes_before
        } == hashes_before
    finally:
        await _delete_prefix(client, prefix)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        pytest.param("status", "corrupt", "invalid status", id="status"),
        pytest.param("end_seq", "not-an-integer", "invalid end_seq", id="end-seq"),
    ],
)
async def test_real_redis_rejects_malformed_run_snapshot_scalars(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
    field: str,
    value: str,
    message: str,
) -> None:
    backend, _, client = redis_backends
    prepared = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    run_keys = [
        key async for key in client.scan_iter(match="tfmsg:test:*:generation:1:run:*")
    ]
    assert len(run_keys) == 1
    await cast(Awaitable[int], client.hset(run_keys[0], field, value))

    with pytest.raises(
        MessagingBackendProtocolError,
        match="invalid protocol response",
    ) as captured:
        await backend.failure(prepared.handle)
    assert message in str(captured.value.diagnostic_context["detail"])
    assert captured.value.diagnostic_context["implementation"] == "redis"
    assert captured.value.diagnostic_context["operation"] == "protocol_validation"

    await backend.finish(prepared.handle, status="completed")


async def test_public_channel_keeps_redis_protocol_details_out_of_its_message(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    backend, _, client = redis_backends
    storage = cast(RedisStorageBackend, backend.storage_backend)
    identity = _identity()
    async with Messaging(backend=storage) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(_Source("value"), identity=identity, after=0)
        assert [message.data async for message in subscription] == ["value"]

        run_keys = [
            key
            async for key in client.scan_iter(match="tfmsg:test:*:generation:1:run:*")
        ]
        assert len(run_keys) == 1
        await cast(Awaitable[int], client.hset(run_keys[0], "status", "corrupt"))

        with pytest.raises(MessagingBackendProtocolError) as captured:
            await channel.get_run_status(identity=identity)

    assert (
        str(captured.value) == "Messaging backend returned an invalid protocol response"
    )
    assert "Redis Messaging run has an invalid status" == str(
        captured.value.diagnostic_context["detail"]
    )
    assert captured.value.diagnostic_context["implementation"] == "redis"


async def test_real_redis_rejects_a_malformed_snapshot_message_entry(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    backend, _, client = redis_backends
    prepared = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    controls = [key async for key in client.scan_iter(match="tfmsg:test:*:control")]
    assert len(controls) == 1
    stream_base = _redis_text(controls[0]).removesuffix(":control")
    generation_base = f"{stream_base}:generation:1"
    run_keys = [key async for key in client.scan_iter(match=f"{generation_base}:run:*")]
    assert len(run_keys) == 1
    await client.xadd(
        f"{generation_base}:messages",
        {"message_id": "missing-required-fields"},
        id="1-0",
    )
    await cast(Awaitable[int], client.hset(run_keys[0], "end_seq", "1"))
    follower = backend.follow(prepared.handle, after=0)

    try:
        with pytest.raises(
            MessagingBackendProtocolError,
            match="invalid protocol response",
        ) as captured:
            await anext(follower)
        assert "incomplete message fields" in str(
            captured.value.diagnostic_context["detail"]
        )
        assert captured.value.diagnostic_context["implementation"] == "redis"
        assert captured.value.diagnostic_context["operation"] == "protocol_validation"
    finally:
        await follower.aclose()
    await backend.finish(prepared.handle, status="completed")


async def test_real_redis_rejected_prepare_does_not_index_phantom_run_keys(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    first, second, client = redis_backends
    prepared = await first.prepare(
        channel="events",
        identity=_identity(run_id="run-active"),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )

    with pytest.raises(RunAlreadyActive):
        await second.prepare(
            channel="events",
            identity=_identity(run_id="run-rejected"),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )

    index_keys = [
        key async for key in client.scan_iter(match="tfmsg:test:*:generation:1:index")
    ]
    assert len(index_keys) == 1
    indexed = {
        _redis_text(member)
        for member in await cast(Awaitable[set[bytes]], client.smembers(index_keys[0]))
    }
    rejected_digest = hashlib.sha256(b"run-rejected").hexdigest()
    assert not any(rejected_digest in member for member in indexed)
    await first.finish(prepared.handle, status="completed")


async def test_real_redis_cleanup_uses_seal_bounded_purge_and_finish(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    backend, _, _ = redis_backends
    storage = cast(RedisStorageBackend, backend.storage_backend)
    identity = _identity()
    prepared = await backend.prepare(
        channel="events",
        identity=identity,
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    assert prepared.handle.generation is not None
    with pytest.raises(StreamDeleteConflict):
        await storage.purge_stream_generation(
            StreamGenerationPurge(
                channel="events",
                identity=identity,
                generation=prepared.handle.generation,
                maximum_records=1,
                cleanup_token="unowned-cleanup",
            )
        )
    await backend.finish(prepared.handle, status="completed")

    begin = await storage.commit_messaging_transition(
        MessagingTransition(
            kind="begin_generation_cleanup",
            transition_id=uuid4().hex,
            channel="events",
            identity=identity,
            settings=storage.messaging_settings,
            cleanup_reason="deleted",
        )
    )
    assert begin.cleanup_required is True
    assert begin.cleanup_generation == prepared.handle.generation
    assert begin.cleanup_reason == "deleted"
    assert isinstance(begin.cleanup_token, str)

    with pytest.raises(MessagingBackendProtocolError, match="invalid protocol"):
        await storage.commit_messaging_transition(
            MessagingTransition(
                kind="finish_generation_cleanup",
                transition_id=uuid4().hex,
                channel="events",
                identity=identity,
                settings=storage.messaging_settings,
                cleanup_generation=begin.cleanup_generation,
                cleanup_reason=begin.cleanup_reason,
                cleanup_token=begin.cleanup_token,
            )
        )

    removed_records = 0
    while True:
        progress = await storage.purge_stream_generation(
            StreamGenerationPurge(
                channel="events",
                identity=identity,
                generation=prepared.handle.generation,
                maximum_records=1,
                cleanup_token=begin.cleanup_token,
            )
        )
        removed_records += progress.removed_records
        if progress.complete:
            break
    assert removed_records > 1

    await storage.commit_messaging_transition(
        MessagingTransition(
            kind="finish_generation_cleanup",
            transition_id=uuid4().hex,
            channel="events",
            identity=identity,
            settings=storage.messaging_settings,
            cleanup_generation=begin.cleanup_generation,
            cleanup_reason=begin.cleanup_reason,
            cleanup_token=begin.cleanup_token,
        )
    )
    snapshot = await storage.load_messaging_state(
        MessagingStateQuery(
            channel="events",
            identity=identity,
            generation=prepared.handle.generation,
        )
    )
    assert snapshot.stream is None
    assert snapshot.tombstone_generation == prepared.handle.generation
    assert snapshot.tombstone_reason == "deleted"


async def test_real_redis_delete_unlinks_only_the_target_generation(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    backend, _, client = redis_backends
    for stream in ("stream-deleted", "stream-retained"):
        run = f"run-{stream}"
        owner = await backend.prepare(
            channel="events",
            identity=_identity(thread_id=stream, run_id=run),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await backend.append(
            owner.handle,
            message_id=f"message-{stream}",
            codec="test.bytes.v1",
            payload=stream.encode(),
        )
        await backend.finish(owner.handle, status="completed")

    controls = [key async for key in client.scan_iter(match="tfmsg:test:*:control")]
    control_by_stream = {
        ThreadIdentity.model_validate_json(stream_value): _redis_text(key)
        for key in controls
        if (
            stream_value := await cast(
                Awaitable[bytes | None], client.hget(key, "stream")
            )
        )
        is not None
    }
    deleted_base = control_by_stream[
        _identity(thread_id="stream-deleted").thread
    ].removesuffix(":control")
    retained_base = control_by_stream[
        _identity(thread_id="stream-retained").thread
    ].removesuffix(":control")

    await backend.delete_stream(
        channel="events",
        identity=_identity(thread_id="stream-deleted", run_id="run-stream-deleted"),
    )

    deleted_control = await cast(
        Awaitable[dict[bytes, bytes]], client.hgetall(f"{deleted_base}:control")
    )
    assert deleted_control[b"generation"] == b"1"
    assert deleted_control[b"state"] == b"deleted"
    deleted_generation_keys = [
        key
        async for key in client.scan_iter(
            match=f"{deleted_base}:generation:*",
        )
    ]
    assert deleted_generation_keys == [
        f"{deleted_base}:generation:1:tombstone".encode()
    ]
    assert await client.get(deleted_generation_keys[0]) == b"deleted"
    assert await client.exists(f"{deleted_base}:delete-lease") == 0
    assert [
        key
        async for key in client.scan_iter(
            match=f"{retained_base}:generation:*",
        )
    ]
    channel_keys = [key async for key in client.scan_iter(match="tfmsg:test:*:channel")]
    assert len(channel_keys) == 1
    assert (
        await cast(Awaitable[bytes | None], client.hget(channel_keys[0], "codec"))
        == b"test.bytes.v1"
    )

    rebuilt = await backend.prepare(
        channel="events",
        identity=_identity(thread_id="stream-deleted", run_id="run-rebuilt"),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    assert rebuilt.handle.generation == 2
    assert [
        key
        async for key in client.scan_iter(
            match=f"{deleted_base}:generation:1:*",
        )
    ] == [f"{deleted_base}:generation:1:tombstone".encode()]
    assert [
        key
        async for key in client.scan_iter(
            match=f"{deleted_base}:generation:2:*",
        )
    ]
    await backend.finish(rebuilt.handle, status="completed")


async def test_real_redis_concurrent_deletes_converge_after_multiple_batches(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    first, second, client = redis_backends
    prepared = await first.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    for index in range(130):
        await first.append(
            prepared.handle,
            message_id=f"message-{index}",
            codec="test.bytes.v1",
            payload=str(index).encode(),
        )
    await first.finish(prepared.handle, status="completed")

    await asyncio.gather(
        first.delete_stream(channel="events", identity=_identity()),
        second.delete_stream(channel="events", identity=_identity()),
    )

    controls = [key async for key in client.scan_iter(match="tfmsg:test:*:control")]
    assert len(controls) == 1
    assert (
        await cast(Awaitable[bytes | None], client.hget(controls[0], "state"))
        == b"deleted"
    )
    generation_base = _redis_text(controls[0]).removesuffix(":control")
    generation_keys = [
        key async for key in client.scan_iter(match=f"{generation_base}:generation:*")
    ]
    assert generation_keys == [f"{generation_base}:generation:1:tombstone".encode()]
    assert await client.get(generation_keys[0]) == b"deleted"


async def test_real_redis_lifecycle_signal_is_bounded_and_cross_generation(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:cancel-state:{uuid4().hex}"
    backend = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.6,
        poll_interval=0.05,
    )
    try:
        prepared = await backend.prepare(
            channel="events",
            identity=_identity(),
            codec="test.bytes.v1",
            after=0,
            cancellable=True,
            recoverable=False,
        )

        assert await backend.request_cancel(prepared.handle) is True
        assert await backend.wait_for_cancel(prepared.handle) is True
        assert await backend.begin_settlement(prepared.handle) is True
        await backend.finish(prepared.handle, status="cancelled")
        signal_keys = [
            key async for key in client.scan_iter(match=f"{prefix}:*:signals")
        ]

        assert len(signal_keys) == 1
        signal_key = signal_keys[0]
        for index in range(2, 132):
            current = await backend.prepare(
                channel="events",
                identity=_identity(run_id=f"run-{index}"),
                codec="test.bytes.v1",
                after=0,
                cancellable=True,
                recoverable=False,
            )
            assert await backend.request_cancel(current.handle) is True
            assert await backend.begin_settlement(current.handle) is True
            await backend.finish(current.handle, status="cancelled")

        entries = await client.xrange(signal_key)
        assert entries is not None
        signal_entries = cast(
            Sequence[tuple[bytes, Mapping[bytes, bytes]]],
            entries,
        )
        assert len(signal_entries) == 256
        assert signal_entries[-1][0] == b"262-0"
        assert signal_entries[-1][1][b"kind"] == b"finish"
        assert signal_entries[-1][1][b"generation"] == b"1"
        assert signal_entries[-1][1][b"run"] == b"run-131"
        await backend.delete_stream(channel="events", identity=_identity())
        assert await client.xlen(signal_key) == 256
        entries = await client.xrange(signal_key)
        assert entries is not None
        assert entries[-1] == (
            b"263-0",
            {b"kind": b"delete", b"generation": b"1", b"run": b""},
        )

        rebuilt = await backend.prepare(
            channel="events",
            identity=_identity(run_id="run-rebuilt"),
            codec="test.bytes.v1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        assert rebuilt.handle.generation == 2
        await backend.finish(rebuilt.handle, status="completed")
        assert await client.xlen(signal_key) == 256
        entries = await client.xrange(signal_key)
        assert entries is not None
        assert entries[-1] == (
            b"264-0",
            {
                b"kind": b"finish",
                b"generation": b"2",
                b"run": b"run-rebuilt",
            },
        )
        with pytest.raises(StreamDeleted):
            await backend.wait_finished(prepared.handle)
    finally:
        await _delete_prefix(client, prefix)


async def test_real_redis_shutdown_settles_during_the_first_commit(
    gated_redis_backend: tuple[RedisBackendHarness, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    release = asyncio.Event()
    source = _Source("first", release=release)
    messaging = Messaging(backend=backend)

    async def cancel() -> None:
        return None

    await messaging.__aenter__()
    client.gate_append_response = True
    try:
        await messaging.channel(name="events", codec=_TextCodec()).wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel,
        )
        await asyncio.wait_for(client.append_committed.wait(), timeout=1)

        closing = asyncio.create_task(messaging.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not closing.done()
        client.append_release.set()
        await asyncio.wait_for(closing, timeout=2)

        assert source.close_calls == 1
    finally:
        client.append_release.set()
        release.set()
        await messaging.aclose()


async def test_real_redis_running_follower_never_crosses_into_a_later_run(
    gated_redis_backend: tuple[RedisBackendHarness, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    first = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.redis-text.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await backend.append(
        first.handle,
        message_id="run-1:1",
        codec="test.redis-text.v1",
        payload=b"first",
    )
    follower = backend.follow(first.handle, after=1)
    client.gate_next_eval_after = True
    next_message = asyncio.ensure_future(anext(follower))
    await asyncio.wait_for(client.eval_returned.wait(), timeout=1)

    await backend.finish(first.handle, status="completed")
    second = await backend.prepare(
        channel="events",
        identity=_identity(run_id="run-2"),
        codec="test.redis-text.v1",
        after=1,
        cancellable=False,
        recoverable=False,
    )
    await backend.append(
        second.handle,
        message_id="run-2:1",
        codec="test.redis-text.v1",
        payload=b"second",
    )
    await backend.finish(second.handle, status="completed")
    client.return_release.set()

    try:
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(next_message, timeout=1)
    finally:
        await follower.aclose()


async def test_real_redis_terminal_snapshot_precedes_later_deletion(
    gated_redis_backend: tuple[RedisBackendHarness, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    prepared = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.redis-text.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.redis-text.v1",
        payload=b"first",
    )
    await backend.finish(prepared.handle, status="completed")
    follower = backend.follow(prepared.handle, after=1)
    client.gate_next_eval_after = True
    next_message = asyncio.ensure_future(anext(follower))
    await asyncio.wait_for(client.eval_returned.wait(), timeout=1)

    await backend.delete_stream(channel="events", identity=_identity())
    client.return_release.set()

    try:
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(next_message, timeout=1)
    finally:
        await follower.aclose()


async def test_real_redis_deletion_precedes_the_atomic_run_snapshot(
    gated_redis_backend: tuple[RedisBackendHarness, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    prepared = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.redis-text.v1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await backend.finish(prepared.handle, status="completed")
    follower = backend.follow(prepared.handle, after=0)
    client.gate_next_eval_before = True
    next_message = asyncio.ensure_future(anext(follower))
    await asyncio.wait_for(client.eval_entered.wait(), timeout=1)

    await backend.delete_stream(channel="events", identity=_identity())
    client.eval_release.set()

    try:
        with pytest.raises(StreamDeleted):
            await asyncio.wait_for(next_message, timeout=1)
    finally:
        await follower.aclose()


async def test_real_redis_cross_worker_attach_uses_one_producer(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    owner_backend, follower_backend, _ = redis_backends
    release = asyncio.Event()
    owner_source = _Source("one", release=release)
    unused_source = _Source("must-not-run")

    async with (
        Messaging(backend=owner_backend) as owner_messaging,
        Messaging(backend=follower_backend) as follower_messaging,
    ):
        owner_channel = owner_messaging.channel(name="events", codec=_TextCodec())
        follower_channel = follower_messaging.channel(
            name="events",
            codec=_TextCodec(),
        )
        first = await owner_channel.wrap(
            owner_source,
            identity=_identity(),
            after=0,
        )
        await asyncio.wait_for(owner_source.started.wait(), timeout=1)
        second = await follower_channel.wrap(
            unused_source,
            identity=_identity(),
            after=0,
        )
        release.set()

        first_data, second_data = await asyncio.gather(_data(first), _data(second))

    assert first_data == ["one"]
    assert second_data == ["one"]
    assert unused_source.close_calls == 1
    assert not unused_source.started.is_set()


async def test_real_redis_remote_cancel_reaches_the_owner_callback(
    redis_backends: tuple[RedisBackendHarness, RedisBackendHarness, Redis],
) -> None:
    owner_backend, remote_backend, _ = redis_backends
    release = asyncio.Event()
    source = _Source("started", release=release)
    cancel_calls = 0

    async def cancel_run() -> None:
        nonlocal cancel_calls
        cancel_calls += 1
        release.set()

    async with (
        Messaging(backend=owner_backend) as owner_messaging,
        Messaging(backend=remote_backend) as remote_messaging,
    ):
        owner_channel = owner_messaging.channel(name="events", codec=_TextCodec())
        remote_channel = remote_messaging.channel(name="events", codec=_TextCodec())
        subscription = await owner_channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )
        await asyncio.wait_for(source.started.wait(), timeout=1)

        assert await asyncio.wait_for(
            remote_channel.cancel(identity=_identity()),
            timeout=2,
        )
        assert await _data(subscription) == ["started"]

    assert cancel_calls == 1
