"""Namespace isolation through committed reads and producer control."""

from typing import ClassVar

from backend_harness import MessagingBackendHarness

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import FiniteMessageSource, Messaging


class TextCodec:
    codec_id: ClassVar[str] = "namespace-test-text"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


async def test_identical_ids_do_not_share_replay_or_deletion(
    messaging_backend: MessagingBackendHarness,
) -> None:
    identities = (
        RunIdentity(namespace="first", thread_id="same", run_id="same"),
        RunIdentity(namespace="second", thread_id="same", run_id="same"),
    )
    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=TextCodec())
        for identity in identities:
            subscription = await channel.wrap(
                FiniteMessageSource.from_events([identity.namespace]),
                identity=identity,
                after=0,
            )
            assert [message.data async for message in subscription] == [
                identity.namespace
            ]
        for identity in identities:
            messages = await channel.read(identity=identity, after=0, limit=10)
            assert [
                (message.envelope.identity, message.data) for message in messages
            ] == [(identity, identity.namespace)]
        await channel.delete_stream(identity=identities[0])
        assert await channel.latest_seq(identity=identities[1]) == 1
        assert await channel.get_run_status(identity=identities[1]) == "completed"


async def test_identical_ids_do_not_share_producer_ownership_or_cancellation(
    messaging_backend: MessagingBackendHarness,
) -> None:
    first = RunIdentity(namespace="first", thread_id="same", run_id="same")
    second = RunIdentity(namespace="second", thread_id="same", run_id="same")
    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=TextCodec())
        owners = []
        for identity in (first, second):
            prepared = await messaging_backend.prepare(
                channel=channel.name,
                identity=identity,
                codec=TextCodec.codec_id,
                after=0,
                cancellable=True,
                recoverable=False,
            )
            assert prepared.is_owner
            owners.append(prepared.handle)
        assert await messaging_backend.request_cancel(owners[0]) is True
        assert await channel.get_run_status(identity=first) == "cancel_requested"
        assert await channel.get_run_status(identity=second) == "running"
        await messaging_backend.finish(owners[0], status="cancelled")
        await messaging_backend.finish(owners[1], status="completed")
