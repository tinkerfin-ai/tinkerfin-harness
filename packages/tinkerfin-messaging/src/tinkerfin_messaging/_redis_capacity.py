"""Reclaim indexed terminal generations before Redis capacity admission."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tinkerfin_contracts import RunIdentity, ThreadIdentity

from ._redis_control import (
    _redis_call,
    _redis_protocol_error,
    complete_generation_cleanup,
)
from ._redis_scripts import _DUE_EXPIRATIONS_SCRIPT

if TYPE_CHECKING:
    from .redis import RedisBackend


async def reclaim_expired(backend: RedisBackend) -> None:
    """Reclaim at most 16 elapsed terminal generations through fenced cleanup.

    Discovery is bounded by an ordered namespace index. Cleanup awaits batches of at
    most 64 generation-private keys and remains resumable after caller cancellation.
    The owning scripts recheck generation, state, deadlines, and lease tokens; a Run
    admitted before its deadline removes the member and cannot be reclaimed here.
    """

    members = await backend._eval(
        _DUE_EXPIRATIONS_SCRIPT, [backend._expirations_key], []
    )
    for raw_control in members:
        control = backend._text(raw_control)
        values = await _redis_call(
            "expiry identity lookup", backend._client.hgetall(control)
        )
        try:
            channel = values[b"channel"].decode()
            thread = ThreadIdentity.model_validate_json(values[b"stream"])
        except (KeyError, ValueError) as error:
            raise _redis_protocol_error(
                "Redis expiry index has no valid thread identity", cause=error
            ) from error
        identity = RunIdentity(
            namespace=thread.namespace,
            thread_id=thread.thread_id,
            run_id="expiry-cleanup",
        )
        if backend._scope(channel, identity).control != control:
            raise _redis_protocol_error("Redis expiry index escapes its storage scope")
        await complete_generation_cleanup(
            backend, channel=channel, identity=identity, requested_reason="expired"
        )
