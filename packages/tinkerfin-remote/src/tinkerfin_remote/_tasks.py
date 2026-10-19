"""Join accepted cleanup before propagating cancellation to its owner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
from typing import TYPE_CHECKING, Concatenate, ParamSpec, TypeVar

from .errors import RemoteError, RemoteErrorCode

if TYPE_CHECKING:
    from ._store import Ledger

ResultT = TypeVar("ResultT")
Parameters = ParamSpec("Parameters")


async def settle(
    operation: Awaitable[ResultT], *, primary_error: BaseException | None = None
) -> ResultT:
    task = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            # CPython 3.14 shield logs a late failure after its waiter is
            # cancelled, even when the owner later retrieves that failure.
            # wait preserves child ownership without disclosing private causes.
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    try:
        result = task.result()
    except (Exception, asyncio.CancelledError) as error:
        if primary_error is not None and not isinstance(primary_error, Exception):
            raise primary_error from error
        if cancellation is not None:
            raise cancellation from error
        raise
    if cancellation is not None:
        raise cancellation
    return result


def raise_cleanup_errors(results: list[BaseException | None]) -> None:
    """Report all cleanup failures after every owned resource has been joined."""
    failures = [result for result in results if isinstance(result, Exception)]
    cause = (
        ExceptionGroup("Remote resource cleanup failed", failures) if failures else None
    )
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            if isinstance(result, asyncio.CancelledError) and cause is not None:
                raise result from cause
            raise result
    if cause is not None:
        raise RemoteError(
            RemoteErrorCode.CLEANUP,
            "Remote resource cleanup did not complete successfully",
            context={"failure_count": len(failures)},
            cause=cause,
        )


def owned_io(
    operation: Callable[
        Concatenate[Ledger, Parameters], Coroutine[object, object, ResultT]
    ],
) -> Callable[Concatenate[Ledger, Parameters], Coroutine[object, object, ResultT]]:
    """Settle an accepted storage operation before its caller leaves the resource scope.

    SQLAlchemy 2.0.52's aiosqlite cursor closes after fetch, not in a finally.
    Cancelling between execute and fetch can retain a live SQLite statement in
    the cancellation traceback even after pool invalidation. The provider waits
    for accepted SQL, cursor cleanup and commit acknowledgment before propagating
    cancellation. Hosts must configure driver and database deadlines on the
    borrowed engine; a receive timeout alone is not a total statement deadline.
    """

    @wraps(operation)
    async def execute(
        owner: Ledger, /, *args: Parameters.args, **kwargs: Parameters.kwargs
    ) -> ResultT:
        if owner._io_closed and asyncio.current_task() not in owner._io_tasks:
            raise RemoteError(RemoteErrorCode.CLOSED, "Remote storage is closed")
        task = asyncio.create_task(operation(owner, *args, **kwargs))
        owner._io_tasks.add(task)
        try:
            return await settle(task)
        finally:
            owner._io_tasks.discard(task)

    return execute
