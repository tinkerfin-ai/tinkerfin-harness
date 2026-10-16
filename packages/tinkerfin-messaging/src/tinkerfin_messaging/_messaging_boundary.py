"""Backend error translation and subscription lifecycle operations."""

from __future__ import annotations

__all__ = [
    "_derived_message_id",
    "_invoke_cancel",
    "_iterate_backend",
    "_join_owned_task",
    "_normalize_cancel_callback",
    "_read_backend",
    "_validate_optional_cursor",
]

import asyncio
import hashlib
import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Generic, TypeAlias, TypeVar, cast

from tinkerfin_contracts import RunIdentity

from .errors import (
    MessagingBackendError,
    MessagingError,
    SseRenderingUnsupported,
    UnexpectedMessagingBackendError,
)
from .models import DecodedMessage

if TYPE_CHECKING:
    from .messaging import CancelContext, MessageSubscription

BackendResultT = TypeVar("BackendResultT")
ProducedT = TypeVar("ProducedT")
ReplayT = TypeVar("ReplayT")

_CancelResult: TypeAlias = (
    Iterable[ProducedT] | Awaitable[Iterable[ProducedT] | None] | None
)
_ContextCancelCallback: TypeAlias = Callable[
    ["CancelContext"],
    _CancelResult[ProducedT],
]
CancelCallback: TypeAlias = (
    Callable[[], _CancelResult[ProducedT]] | _ContextCancelCallback[ProducedT]
)


def _unexpected_backend_error(
    operation: str,
    error: Exception,
) -> UnexpectedMessagingBackendError:
    return UnexpectedMessagingBackendError(
        f"Messaging backend {operation} failed",
        diagnostic_context={"operation": operation},
        cause=error,
    )


async def _await_backend(
    operation: str,
    awaitable: Awaitable[BackendResultT],
) -> BackendResultT:
    try:
        return await awaitable
    except MessagingError as error:
        if isinstance(error, MessagingBackendError):
            error._enrich_diagnostic_context({"operation": operation})
        raise
    except Exception as error:
        translated = _unexpected_backend_error(operation, error)
        raise translated from error


def _read_backend(
    operation: str,
    reader: Callable[[], BackendResultT],
) -> BackendResultT:
    try:
        return reader()
    except MessagingError as error:
        if isinstance(error, MessagingBackendError):
            error._enrich_diagnostic_context({"operation": operation})
        raise
    except Exception as error:
        translated = _unexpected_backend_error(operation, error)
        raise translated from error


async def _iterate_backend(
    operation: str,
    iterator: AsyncIterator[BackendResultT],
) -> AsyncGenerator[BackendResultT, None]:
    try:
        async for item in iterator:
            yield item
    except MessagingError as error:
        if isinstance(error, MessagingBackendError):
            error._enrich_diagnostic_context({"operation": operation})
        raise
    except Exception as error:
        translated = _unexpected_backend_error(operation, error)
        raise translated from error


def _derived_message_id(identity: RunIdentity, ordinal: int) -> str:
    """Keep ordinary message IDs bounded without changing the common readable form."""

    candidate = f"{identity.run_id}:{ordinal}"
    if len(candidate) <= 1024:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()
    return f"sha256:{digest}"


def _validate_optional_cursor(after: int | None) -> None:
    if after is not None and (isinstance(after, bool) or not isinstance(after, int)):
        raise TypeError("after must be an integer or None")


async def _close_async_iterator(iterator: AsyncIterator[object]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _invoke_cancel(
    callback: _ContextCancelCallback[ProducedT],
    context: CancelContext,
) -> Iterable[ProducedT] | None:
    result = callback(context)
    if inspect.isawaitable(result):
        return await result
    return result


def _normalize_cancel_callback(
    callback: CancelCallback[ProducedT],
) -> _ContextCancelCallback[ProducedT]:
    """Normalize one supported public signature before claiming a durable run."""

    from .messaging import CancelContext

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError) as error:
        raise TypeError(
            "cancel callback must expose an inspectable signature"
        ) from error

    try:
        signature.bind()
    except TypeError:
        pass
    else:
        zero_argument = cast(Callable[[], _CancelResult[ProducedT]], callback)

        def invoke_without_context(
            _context: CancelContext,
        ) -> _CancelResult[ProducedT]:
            return zero_argument()

        return invoke_without_context

    try:
        signature.bind(
            CancelContext(
                channel="callback",
                identity=RunIdentity(
                    namespace="callback", thread_id="callback", run_id="callback"
                ),
            )
        )
    except TypeError as error:
        raise TypeError(
            "cancel callback must accept no arguments or one positional CancelContext"
        ) from error
    return cast(_ContextCancelCallback[ProducedT], callback)


async def _join_owned_task(task: asyncio.Task[BackendResultT]) -> None:
    """Settle an owned task while preserving caller cancellation and task failure."""

    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    caller_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            next_cancel_count = current.cancelling() if current is not None else 0
            if next_cancel_count > cancel_count:
                if caller_cancellation is None:
                    caller_cancellation = cancellation
                cancel_count = next_cancel_count
                continue
            if task.done():
                break
            raise
        except BaseException:
            if task.done():
                break
            raise

    task_error: BaseException | None = None
    try:
        task.result()
    except BaseException as error:  # noqa: BLE001 - preserve owned task outcome
        task_error = error
    if caller_cancellation is not None:
        if task_error is not None:
            caller_cancellation.add_note(
                "Messaging close also failed: "
                f"{type(task_error).__name__}: {task_error}"
            )
        raise caller_cancellation.with_traceback(caller_cancellation.__traceback__)
    if task_error is not None:
        raise task_error.with_traceback(task_error.__traceback__)


class _SubscriptionIterator(Generic[ReplayT]):
    """Own one decoded pull so detachment never closes a running generator.

    The consumer owns its waiting task; this iterator owns the underlying pull and
    close tasks. Their settlement preserves backpressure and consumer cancellation.
    """

    def __init__(self, source: AsyncGenerator[DecodedMessage[ReplayT], None]) -> None:
        self._source = source
        self._pull: asyncio.Task[DecodedMessage[ReplayT]] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    def __aiter__(self) -> _SubscriptionIterator[ReplayT]:
        return self

    async def __anext__(self) -> DecodedMessage[ReplayT]:
        if self._closed:
            raise StopAsyncIteration
        if self._pull is not None:
            raise RuntimeError("a message subscription supports only one active pull")
        task = asyncio.create_task(
            self._next_message(), name="tinkerfin-messaging-subscription-pull"
        )
        self._pull = task
        try:
            return await asyncio.shield(task)
        except StopAsyncIteration:
            self._closed = True
            raise
        except asyncio.CancelledError as cancellation:
            if not task.done() and not task.cancelling():
                task.cancel()
            try:
                await _join_owned_task(task)
            except asyncio.CancelledError:
                # The owned pull is settled; the consumer's cancellation remains
                # the public outcome, including when detachment cancelled the pull.
                pass
            except Exception as error:  # noqa: BLE001 - keep cancellation primary
                cancellation.add_note(
                    f"Messaging pull cleanup also failed: {type(error).__name__}"
                )
            raise cancellation
        finally:
            self._pull = None

    async def _next_message(self) -> DecodedMessage[ReplayT]:
        return await anext(self._source)

    async def aclose(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_once(), name="tinkerfin-messaging-decoder-close"
            )
            self._close_task.add_done_callback(_close_task_finished)
        await _join_owned_task(self._close_task)

    async def _close_once(self) -> None:
        task = self._pull
        try:
            if task is not None and not task.done():
                if not task.cancelling():
                    task.cancel()
                try:
                    await _join_owned_task(task)
                except (asyncio.CancelledError, StopAsyncIteration):
                    # This close task is retained independently of its waiters.
                    # Cancellation here belongs to the pull it deliberately stopped.
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
        finally:
            await self._source.aclose()


def __aiter__(
    self: MessageSubscription[ReplayT],
) -> AsyncIterator[DecodedMessage[ReplayT]]:
    if self._claimed:
        raise RuntimeError("a message subscription can only be consumed once")
    if self._close_task is not None:
        raise RuntimeError("a message subscription is closed")
    self._claimed = True
    delivery = _SubscriptionIterator(self._iterate())
    self._delivery = delivery
    return delivery


def to_sse(self: MessageSubscription[ReplayT]) -> AsyncGenerator[bytes, None]:
    """Render committed messages while preserving their durable sequences."""

    renderer = self._renderer
    if renderer is None:
        raise SseRenderingUnsupported("This channel has no SSE renderer")

    async def iterate() -> AsyncGenerator[bytes, None]:
        try:
            async for message in self:
                yield renderer.render(
                    seq=message.envelope.seq,
                    payload=message.data,
                )
        finally:
            await self.aclose()

    return iterate()


async def aclose(self: MessageSubscription[ReplayT]) -> None:
    """Detach this subscriber through one cancellation-safe close task."""

    task = self._close_task
    if task is None:
        task = asyncio.create_task(
            _close_subscription_once(self),
            name="tinkerfin-messaging-subscription-close",
        )
        self._close_task = task
        task.add_done_callback(_close_task_finished)
    await _join_owned_task(task)


async def _close_subscription_once(self: MessageSubscription[ReplayT]) -> None:
    primary: BaseException | None = None
    delivery = self._delivery
    if delivery is not None:
        try:
            await _close_async_iterator(cast(AsyncIterator[object], delivery))
        except BaseException as error:  # noqa: BLE001 - settle cancellation and failure
            primary = error
        finally:
            self._delivery = None

    backend_iterator = self._backend_iterator
    if backend_iterator is not None:
        try:
            await _close_backend_iterator(self, backend_iterator)
        except BaseException as error:  # noqa: BLE001 - preserve primary close outcome
            if primary is None:
                primary = error
            else:
                primary.add_note(
                    "Messaging backend iterator close also failed: "
                    f"{type(error).__name__}: {error}"
                )
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)


async def _close_backend_iterator(
    self: MessageSubscription[ReplayT],
    iterator: AsyncIterator[object],
) -> None:
    task = self._backend_close_task
    if task is None:
        task = asyncio.create_task(
            _await_backend("follow close", _close_async_iterator(iterator)),
            name="tinkerfin-messaging-backend-follower-close",
        )
        self._backend_close_task = task
        task.add_done_callback(_close_task_finished)
    await _join_owned_task(task)
    if self._backend_iterator is iterator:
        self._backend_iterator = None


def _close_task_finished(task: asyncio.Task[None]) -> None:
    """Consume a retained close failure when no caller waits again."""

    if not task.cancelled():
        task.exception()
