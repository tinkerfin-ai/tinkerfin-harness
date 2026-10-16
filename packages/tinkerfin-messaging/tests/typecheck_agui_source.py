"""Static contracts for the recommended typed AG-UI delivery path."""

from collections.abc import AsyncGenerator, Awaitable, Callable

from ag_ui.core import BaseEvent

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    AgUiChannel,
    MessageChannel,
    MessageSource,
    ProfiledMessageSource,
)


async def check_source_types(
    channel: AgUiChannel,
    events: ProfiledMessageSource[BaseEvent, BaseEvent],
    unprofiled: MessageSource[BaseEvent],
    wrong_replay: ProfiledMessageSource[BaseEvent, str],
    on_subscribed: Callable[[], Awaitable[None]],
) -> AsyncGenerator[bytes, None]:
    await channel.open_sse(unprofiled)  # pyright: ignore[reportArgumentType]
    await channel.open_sse(wrong_replay)  # pyright: ignore[reportArgumentType]
    return await channel.open_sse(events, on_subscribed=on_subscribed)


async def check_subscription_notifications(
    channel: MessageChannel[str, str],
    profiled_channel: MessageChannel[BaseEvent, BaseEvent],
    source: MessageSource[str],
    events: ProfiledMessageSource[BaseEvent, BaseEvent],
    identity: RunIdentity,
    on_subscribed: Callable[[], Awaitable[None]],
) -> None:
    ordinary = await channel.open_sse(
        source, identity=identity, on_subscribed=on_subscribed
    )
    await ordinary.aclose()
    profiled = await profiled_channel.open_sse(events, on_subscribed=on_subscribed)
    await profiled.aclose()
