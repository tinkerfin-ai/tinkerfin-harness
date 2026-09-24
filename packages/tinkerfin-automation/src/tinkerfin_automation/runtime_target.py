"""Target that invokes the public TinkerFin Runtime facade."""

from __future__ import annotations

from typing import cast

from langchain.agents.middleware.types import InputAgentState

from tinkerfin import AgentRuntime
from tinkerfin.plan import AgentMode

from .errors import TargetExecutionError
from .targets import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionSucceeded,
    normalize_target_result,
)


class TinkerFinTarget:
    """Execute scheduled work with a configured AgentRuntime in its namespace.

    Task input is passed as ordinary graph input. The host registers the Runtime;
    task data cannot choose its model, tools, workspace, or namespace. Each attempt
    keeps the thread and run identity assigned by Automation.
    TinkerFin cancellation settles Runtime-owned resources, but synchronous tools and
    external side effects prevent a general final-cancellation guarantee.
    """

    def __init__(
        self,
        runtime: AgentRuntime[None],
        *,
        mode: AgentMode | None = None,
    ) -> None:
        """Borrow a built Runtime without preparing resources or executing work.

        Args:
            runtime: Runtime matching the execution identity's saved namespace.
            mode: Optional execution mode already supported by this Runtime.

        Raises:
            TypeError: runtime is not an AgentRuntime.
        """

        if not isinstance(runtime, AgentRuntime):
            raise TypeError("runtime must be an AgentRuntime")
        self._runtime = runtime
        self._mode: AgentMode | None = mode

    @property
    def cancellation_is_final(self) -> bool:
        """Return false because arbitrary external tool effects may outlive cancellation."""

        return False

    async def run(self, request: ExecutionRequest) -> ExecutionOutcome:
        """Run the assigned attempt and return success or an unfinished interrupt.

        Args:
            request: Authorized execution input and immutable attempt identity.

        Returns:
            Success without copying private state, or the pending interrupt IDs.

        Raises:
            TargetExecutionError: The execution identity belongs to another Runtime
                namespace; the scheduling namespace does not select Runtime resources.
            BaseException: Runtime execution fails or is cancelled after cleanup.
        """

        identity = request.execution.identity
        if identity.namespace != self._runtime.namespace:
            raise TargetExecutionError("Execution and Runtime namespaces must match")

        result = await self._runtime.ainvoke(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=cast(InputAgentState, dict(request.execution.input)),
            mode=self._mode,
        )
        if result.get("__interrupt__"):
            return normalize_target_result({"__interrupt__": result["__interrupt__"]})
        # Runtime and Trace retain the complete root state. Automation intentionally
        # avoids copying messages, tool output, or private state into execution rows.
        return ExecutionSucceeded()


__all__ = ["TinkerFinTarget"]
