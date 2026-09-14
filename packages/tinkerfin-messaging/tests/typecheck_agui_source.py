"""Static contracts for the recommended typed AG-UI delivery path."""

from collections.abc import AsyncGenerator

from ag_ui.core import BaseEvent

from tinkerfin_messaging import AgUiChannel, MessageSource, ProfiledMessageSource


async def check_source_types(
    channel: AgUiChannel,
    events: ProfiledMessageSource[BaseEvent, BaseEvent],
    unprofiled: MessageSource[BaseEvent],
    wrong_replay: ProfiledMessageSource[BaseEvent, str],
) -> AsyncGenerator[bytes, None]:
    await channel.open_sse(unprofiled)  # pyright: ignore[reportArgumentType]
    await channel.open_sse(wrong_replay)  # pyright: ignore[reportArgumentType]
    return await channel.open_sse(events)
