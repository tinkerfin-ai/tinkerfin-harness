"""Backend-author verification helpers for the public Messaging contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import ClassVar
from uuid import uuid4

from tinkerfin_contracts import RunIdentity

from .backend_contract import MessagingBackend
from .errors import RunRequestConflict
from .messaging import Messaging
from .sources import FiniteMessageSource

MessagingBackendFactory = Callable[[], AbstractAsyncContextManager[MessagingBackend]]


class _VerificationTextCodec:
    codec_id: ClassVar[str] = "tinkerfin.testing.utf8-text"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _VerificationCancellableSource:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncIterator[str]:
            self.started.set()
            yield "started"
            await self.released.wait()

        return iterate()

    async def aclose(self) -> None:
        self.closed = True
        self.released.set()


async def verify_messaging_backend(
    open_isolated_backend: MessagingBackendFactory,
) -> None:
    """Verify one Backend factory through supported Messaging user operations.

    The factory is called once and must return an asynchronous context manager that
    yields an empty, isolated Backend. The context manager owns test resources and must
    close them after verification; Messaging borrows the yielded Backend and never
    closes its clients. Verification mutates and deletes only the isolated namespace.

    This helper checks storage preparation, ordered commits, finite reads, replay,
    cancellation, terminal status, generation deletion, and clean reconstruction. A
    distributed Backend must additionally test its database-specific contention,
    ownership expiry, transport uncertainty, and process-failure behavior.

    Args:
        open_isolated_backend: Zero-argument factory returning one owned, empty Backend
            context for the verification run.

    Returns:
        ``None`` after every observable contract scenario succeeds.

    Raises:
        TypeError: The factory is not callable or yields an incomplete Backend.
        AssertionError: A scenario violates the public Messaging behavior.
        MessagingError: The Backend rejects a valid verification operation.
    """

    if not callable(open_isolated_backend):
        raise TypeError("open_isolated_backend must be callable")
    namespace = uuid4().hex
    thread_id = f"backend-verification-{namespace}"
    codec = _VerificationTextCodec()
    async with open_isolated_backend() as backend:
        if not isinstance(backend, MessagingBackend):
            raise TypeError("factory must yield a complete MessagingBackend")
        async with Messaging(backend=backend) as messaging:
            channel = messaging.channel(name="verification-events", codec=codec)
            completed_identity = RunIdentity(
                namespace="messaging-verification",
                thread_id=thread_id,
                run_id="completed-run",
            )
            completed = await channel.wrap(
                FiniteMessageSource[str].from_events(("first", "second")),
                identity=completed_identity,
                after=0,
            )
            delivered = [message async for message in completed]
            assert [message.data for message in delivered] == ["first", "second"], (
                "ordered-commit",
                delivered,
            )
            assert [message.envelope.seq for message in delivered] == [1, 2], (
                "contiguous-sequence",
                delivered,
            )
            assert await channel.get_run_status(identity=completed_identity) == (
                "completed"
            ), "terminal-status"
            page = await channel.read(identity=completed_identity, after=0, limit=10)
            assert [message.data for message in page] == ["first", "second"], (
                "finite-replay",
                page,
            )
            follower = await channel.follow(identity=completed_identity, after=0)
            assert [message.data async for message in follower] == [
                "first",
                "second",
            ], (
                "terminal-follow",
                completed_identity,
            )

            cancellable_identity = RunIdentity(
                namespace="messaging-verification",
                thread_id=thread_id,
                run_id="cancelled-run",
            )
            source = _VerificationCancellableSource()

            def cancel_source() -> tuple[str, ...]:
                source.released.set()
                return ("cancelled-tail",)

            cancelled = await channel.wrap(
                source,
                identity=cancellable_identity,
                after=2,
                cancel=cancel_source,
            )
            cancelled_delivery = aiter(cancelled)
            first_cancelled_message = await anext(cancelled_delivery)
            assert first_cancelled_message.data == "started", (
                "cancellation-prefix",
                first_cancelled_message,
            )
            assert await channel.cancel(identity=cancellable_identity) is True, (
                "cancellation-initiation",
                cancellable_identity,
            )
            cancelled_messages = [message.data async for message in cancelled_delivery]
            assert cancelled_messages == ["cancelled-tail"], (
                "cancellation-tail",
                cancelled_messages,
            )
            assert source.closed, "source-cleanup"

            await channel.delete_stream(identity=cancellable_identity)
            replacement_identity = RunIdentity(
                namespace="messaging-verification",
                thread_id=thread_id,
                run_id="replacement-run",
            )
            replacement = await channel.wrap(
                FiniteMessageSource[str].from_events(("replacement",)),
                identity=replacement_identity,
                after=0,
            )
            replacement_messages = [message async for message in replacement]
            assert [message.data for message in replacement_messages] == [
                "replacement"
            ], (
                "generation-replacement",
                replacement_messages,
            )
            assert replacement_messages[0].envelope.seq == 1, (
                "generation-sequence-reset",
                replacement_messages,
            )
            bound_identity = RunIdentity(
                namespace="messaging-verification",
                thread_id=f"{thread_id}-command",
                run_id="bound-command",
            )
            bound = await channel.wrap(
                FiniteMessageSource[str].from_events(("accepted",)),
                identity=bound_identity,
                request_digest="a" * 64,
                after=0,
            )
            assert [item.data async for item in bound] == ["accepted"], "bound-command"
            attached = await channel.wrap(
                FiniteMessageSource[str].from_events(("must not execute",)),
                identity=bound_identity,
                request_digest="a" * 64,
                after=0,
            )
            assert [item.data async for item in attached] == ["accepted"], (
                "bound-attachment"
            )
            for requested in (None, "b" * 64):
                try:
                    await channel.wrap(
                        FiniteMessageSource[str].from_events(("must not execute",)),
                        identity=bound_identity,
                        request_digest=requested,
                    )
                except RunRequestConflict:
                    pass
                else:
                    raise AssertionError("request-binding-conflict")
            try:
                await channel.wrap(
                    FiniteMessageSource[str].from_events(("must not execute",)),
                    identity=replacement_identity,
                    request_digest="a" * 64,
                )
            except RunRequestConflict:
                pass
            else:
                raise AssertionError("unbound-run-cannot-be-adopted")


__all__ = ["MessagingBackendFactory", "verify_messaging_backend"]
