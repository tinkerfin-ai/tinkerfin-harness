"""Private durable Plan handoff evidence and transient model instruction."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, NotRequired, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, SystemMessage

PLAN_HANDOFF_STATE_KEY = "_tinkerfin_plan_handoff_digest"


class _PlanHandoffState(AgentState[Any], total=False):
    """Native state contribution proving which approved Plan was staged."""

    _tinkerfin_plan_handoff_digest: NotRequired[str]


_ACTIVE_HANDOFF_INSTRUCTION: ContextVar[str | None] = ContextVar(
    "tinkerfin_active_plan_handoff_instruction",
    default=None,
)


def _request_with_handoff_instruction(
    request: ModelRequest[Any],
) -> ModelRequest[Any]:
    """Append request-scoped approved Plan context without mutating message state."""

    instruction = _ACTIVE_HANDOFF_INSTRUCTION.get()
    if instruction is None:
        return request
    system_message = request.system_message
    if system_message is None:
        updated = SystemMessage(content=instruction)
    else:
        content_blocks = [
            *system_message.content_blocks,
            {"type": "text", "text": f"\n\n{instruction}"},
        ]
        updated = system_message.model_copy(
            update={"content": cast(list[str | dict[str, Any]], content_blocks)}
        )
    return request.override(system_message=updated)


class _PlanHandoffMiddleware(AgentMiddleware[_PlanHandoffState, Any, Any]):
    """Expose approved Plan context only to model calls in the native handoff run."""

    state_schema = _PlanHandoffState
    tools = ()

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage:
        """Inject the active handoff into one synchronous model request."""

        return handler(_request_with_handoff_instruction(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage:
        """Inject the active handoff into one asynchronous model request."""

        return await handler(_request_with_handoff_instruction(request))


def create_plan_handoff_middleware() -> AgentMiddleware[Any, Any, Any]:
    """Create the internal stateless middleware installed on a Plan-capable Agent."""

    return cast(AgentMiddleware[Any, Any, Any], _PlanHandoffMiddleware())


@contextmanager
def activate_plan_handoff_instruction(instruction: str) -> Generator[None]:
    """Scope approved Plan context to one pull or close without crossing a yield."""

    if not isinstance(instruction, str) or not instruction:
        raise ValueError("Plan handoff instruction must be non-empty text")
    token = _ACTIVE_HANDOFF_INSTRUCTION.set(instruction)
    try:
        yield
    finally:
        _ACTIVE_HANDOFF_INSTRUCTION.reset(token)


class PlanHandoffStream(AsyncIterator[Mapping[str, object]]):
    """Keep approved Plan context local to each native stream operation.

    Managed Observation can consume successive parts in different asyncio tasks.
    A ContextVar token must therefore be restored before returning a part, while
    native model tasks inherit the instruction from the pull that starts them.
    Explicit closure establishes its own context and owns the upstream iterator.
    ``test_plan_approval_hands_off_to_native_with_the_same_message_id`` covers the
    public observed and unobserved Runtime paths.
    """

    def __init__(
        self,
        source: AsyncIterator[Mapping[str, object]],
        instruction: str | None,
    ) -> None:
        self._source = source
        self._instruction = instruction
        self._closed = False

    async def __anext__(self) -> Mapping[str, object]:
        if self._closed:
            raise StopAsyncIteration
        context = (
            nullcontext()
            if self._instruction is None
            else activate_plan_handoff_instruction(self._instruction)
        )
        with context:
            return await anext(self._source)

    async def aclose(self) -> None:
        """Close native work once with task-local approved Plan context."""

        if self._closed:
            return
        self._closed = True
        close = getattr(self._source, "aclose", None)
        if close is None:
            return
        context = (
            nullcontext()
            if self._instruction is None
            else activate_plan_handoff_instruction(self._instruction)
        )
        with context:
            await cast(Callable[[], Awaitable[object]], close)()


__all__ = [
    "PLAN_HANDOFF_STATE_KEY",
    "PlanHandoffStream",
    "create_plan_handoff_middleware",
]
