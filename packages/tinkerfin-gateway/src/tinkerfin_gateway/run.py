"""Durable run access independent of Runtime construction and HTTP."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from ag_ui.core import BaseEvent

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import AgUiChannel, MessageSubscription, RunStatus


class GatewayRun:
    """Access an already authorized run without keeping a Runtime alive.

    The channel and Messaging lifecycle remain borrowed. Closing a subscription
    only detaches that reader; cancellation requires the explicit cancel method.
    This handle does not grant permission to read or cancel the bound identity.
    """

    def __init__(self, channel: AgUiChannel, identity: RunIdentity) -> None:
        """Bind the authorized identity and its durable delivery space."""
        self._channel = channel
        self._identity = identity

    @property
    def identity(self) -> RunIdentity:
        """Return this handle's immutable namespace, thread, and run."""
        return self._identity

    @asynccontextmanager
    async def subscribe(
        self, *, after: int | None = None
    ) -> AsyncGenerator[MessageSubscription[BaseEvent], None]:
        """Replay and follow typed output, detaching on every context exit.

        Args:
            after: Last applied sequence; omission replays from the run's beginning.

        Yields:
            Single-use output with its original sequence IDs and event objects.

        Raises:
            MessagingError: The run or cursor is unavailable or invalid.
        """
        subscription = await self._channel.follow(identity=self._identity, after=after)
        async with subscription:
            yield subscription

    async def cancel(self) -> bool:
        """Request durable producer cancellation and await its settled outcome.

        Returns:
            Whether this request cancelled an active run.

        Raises:
            MessagingError: The run cannot be cancelled or settlement fails.
        """
        return await self._channel.cancel(identity=self._identity)

    async def delivery_status(self) -> RunStatus:
        """Read delivery state; this does not infer the Agent's business outcome."""
        return await self._channel.get_run_status(identity=self._identity)


__all__ = ["GatewayRun"]
