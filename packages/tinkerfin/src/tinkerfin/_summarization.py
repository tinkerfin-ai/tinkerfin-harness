"""Observe locked native summarization without duplicating its execution policy.

Only exact upstream declarations are adapted. Application subclasses retain
ownership of their hooks, just as in backend resource preparation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, NotRequired, cast

from deepagents.middleware.summarization import (
    SUMMARIZATION_EVENT_KEY,
    SummarizationMiddleware,
    SummarizationState,
    SummarizationToolMiddleware,
)
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)
from langchain.tools import ToolRuntime
from langchain_core.messages import AnyMessage
from langgraph.types import Command

from ._compaction_observation import (
    _CURRENT_COMPACTION,
    _SUMMARY_OPERATION,
    CompactionOperation,
)


class ObservedSummarizationState(SummarizationState):
    _tinkerfin_compaction_id: NotRequired[Annotated[str, PrivateStateAttr]]


class ObservedSummarization(SummarizationMiddleware):
    """Retain native policy and associate selected history and summary calls."""

    state_schema = ObservedSummarizationState

    @property
    def name(self) -> str:
        return "SummarizationMiddleware"

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        operation = CompactionOperation("automatic")
        operation.original = self._get_effective_messages(request)
        token = _CURRENT_COMPACTION.set(operation)
        try:
            response = await super().awrap_model_call(request, handler)
            if (
                isinstance(response, ExtendedModelResponse)
                and response.command is not None
            ):
                update = cast(dict[str, object] | None, response.command.update)
                if isinstance(update, dict) and SUMMARIZATION_EVENT_KEY in update:
                    operation.mark_update(update)
                    await operation.saving()
            return response
        except BaseException as error:
            if operation.started:
                await operation.fail(error)
            raise
        finally:
            _CURRENT_COMPACTION.reset(token)

    def _partition_messages(
        self, conversation_messages: list[AnyMessage], cutoff_index: int
    ) -> tuple[list[AnyMessage], list[AnyMessage]]:
        selected, preserved = super()._partition_messages(
            conversation_messages, cutoff_index
        )
        operation = _CURRENT_COMPACTION.get()
        if operation is not None:
            # Native truncation can rewrite arguments before partitioning. Keep the
            # original effective selection separate from the actual summary prompt.
            operation.selected = operation.original[: len(selected)]
        return selected, preserved

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        operation = _CURRENT_COMPACTION.get()
        if operation is None:
            return await super()._acreate_summary(messages_to_summarize)
        await operation.start()
        token = _SUMMARY_OPERATION.set(operation.id)
        try:
            summary = await super()._acreate_summary(messages_to_summarize)
            await operation.generated(summary)
            return summary
        finally:
            _SUMMARY_OPERATION.reset(token)


def observe_summarization(source: SummarizationMiddleware) -> SummarizationMiddleware:
    """Copy a native declaration while retaining its configured helper and backend."""
    if type(source) is not SummarizationMiddleware:
        return source
    result = ObservedSummarization.__new__(ObservedSummarization)
    result.__dict__.update(source.__dict__)
    return result


class ObservedCompactionTool(SummarizationToolMiddleware):
    """Keep native Commands and ToolMessages in the active run's tool loop."""

    state_schema = ObservedSummarizationState

    @property
    def name(self) -> str:
        return "SummarizationToolMiddleware"

    async def _arun_compact(self, runtime: ToolRuntime[Any, Any]) -> Command[object]:
        operation = CompactionOperation("tool", tool_call_id=runtime.tool_call_id)
        operation.original = self._summarization._apply_event_to_messages(
            runtime.state.get("messages", []),
            runtime.state.get(SUMMARIZATION_EVENT_KEY),
        )
        token = _CURRENT_COMPACTION.set(operation)
        try:
            # Deep Agents 0.7.13 omits Command's node-name and ToolRuntime's
            # state generics; the native result and its update stay unchanged.
            command = cast(
                Command[object],
                await super()._arun_compact(runtime),  # pyright: ignore[reportUnknownMemberType]
            )
            update = cast(dict[str, object] | None, command.update)
            if isinstance(update, dict) and SUMMARIZATION_EVENT_KEY in update:
                operation.mark_update(update)
                await operation.saving()
            elif operation.tool_error is not None:
                await operation.fail(operation.tool_error)
            elif not operation.finished:
                await operation.complete({"status": "nothing_to_compact"})
            return command
        except BaseException as error:
            await operation.fail(error)
            raise
        finally:
            _CURRENT_COMPACTION.reset(token)

    @staticmethod
    def _compact_error(tool_call_id: str, exc: BaseException) -> Command[object]:
        # Native tools convert errors into a regular ToolMessage. Record the error
        # as an operation fact at the async boundary, without parsing its prose.
        operation = _CURRENT_COMPACTION.get()
        if operation is not None:
            operation.tool_error = exc
        # The upstream helper also returns an unparameterized Command.
        return cast(
            Command[object],
            SummarizationToolMiddleware._compact_error(tool_call_id, exc),  # pyright: ignore[reportUnknownMemberType]
        )
