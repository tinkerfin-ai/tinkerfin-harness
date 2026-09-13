"""Own workspace borrowing until native execution and recovery have settled."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Self, TypeVar

from tinkerfin_contracts import PreparedWorkspace, RunIdentity, Workspace

from ._failure_evidence import retain_failure
from ._run_owner import RunOwner
from .tools import _ToolRunScope

WorkspaceT = TypeVar("WorkspaceT")
BackendT = TypeVar("BackendT")


class RunResources:
    """Keep workspace contexts in the native Task that entered them.

    Graph source closure and resume settlement precede workspace release. The
    coordinator's supervisor then releases admission before terminal observation.
    Started cleanup cannot be cancelled again by an external close or lease loss.
    """

    def __init__(self, owner: RunOwner) -> None:
        self.owner = owner
        self._stack = AsyncExitStack()
        self._closed = False
        self._close_error: BaseException | None = None
        self._transferred = False

    async def __aenter__(self) -> Self:
        """Own preparation until a native stream accepts the prepared resources."""
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close preparation exits that did not transfer to a managed stream."""
        if not self._transferred:
            if error is None:
                await self.aclose(None)
            else:
                await self.close_after_failure(error)

    def transfer(self) -> None:
        """Transfer close ownership once without an interruptible handoff."""
        if self._transferred or self._closed:
            raise RuntimeError(
                "Run resource ownership was already transferred or closed"
            )
        self._transferred = True

    async def prepare_workspace(
        self, declaration: Workspace[WorkspaceT, BackendT], identity: RunIdentity
    ) -> PreparedWorkspace[WorkspaceT, BackendT]:
        """Borrow a workspace in the Task that will release it after Graph closure."""
        return await self._stack.enter_async_context(declaration.prepare(identity))

    def borrow_tools(
        self, identity: RunIdentity, workspace: WorkspaceT | None
    ) -> _ToolRunScope[WorkspaceT]:
        """Keep tool access valid until graph settlement, then revoke before release."""
        scope = _ToolRunScope(identity, workspace)
        self._stack.callback(scope.close)
        return scope

    async def aclose(self, error: BaseException | None) -> None:
        """Release workspace and admission once before reporting a final outcome."""
        if self._closed:
            if self._close_error is not None:
                raise self._close_error
            return
        self._closed = True
        await self.owner.begin_settlement()
        failures: list[BaseException] = []
        try:
            await self._stack.__aexit__(
                type(error) if error is not None else None,
                error,
                error.__traceback__ if error is not None else None,
            )
        except BaseException as failure:  # noqa: BLE001 - release coordination before reporting resource failures
            failures.append(failure)
        try:
            await self.owner.release_coordination(
                error or (failures[0] if failures else None)
            )
        except BaseException as failure:  # noqa: BLE001 - select cancellation/control priority after both exits
            failures.append(failure)
        if failures:
            primary = next(
                (
                    failure
                    for failure in failures
                    if not isinstance(failure, Exception | asyncio.CancelledError)
                ),
                next(
                    (
                        failure
                        for failure in failures
                        if isinstance(failure, asyncio.CancelledError)
                    ),
                    failures[0],
                ),
            )
            for failure in failures:
                if failure is not primary:
                    retain_failure(
                        primary, failure, label="Additional resource cleanup failure"
                    )
            self._close_error = primary
            raise primary

    async def close_after_failure(self, primary: BaseException) -> None:
        """Release preparation ownership while retaining the original failure."""
        try:
            await self.aclose(primary)
        except BaseException as cleanup:
            if cleanup is primary:
                return
            if not isinstance(cleanup, Exception | asyncio.CancelledError) or (
                isinstance(primary, Exception)
                and isinstance(cleanup, asyncio.CancelledError)
            ):
                retain_failure(cleanup, primary, label="Run preparation also failed")
                raise cleanup
            retain_failure(primary, cleanup, label="Run resource cleanup also failed")
