"""Supply managed context, identity and workspace to framework tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, cast

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain.tools import ToolRuntime as LangChainToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from .tools import ToolRuntime, _ToolRunScope


class _ToolRuntimeMiddleware(AgentMiddleware):
    """Supply tool-scoped access without changing business context or graph state."""

    def __init__(self, scope: _ToolRunScope[object] | None) -> None:
        self._scope = scope

    def _request(self, request: ToolCallRequest) -> ToolCallRequest:
        # LangChain owns the runtime dataclass. Copy its public fields at this
        # boundary; tool injection remains the ToolNode's responsibility, including
        # replacement of model-supplied runtime arguments. No resource enters state.
        # ToolCallRequest.runtime uses unparameterized ToolRuntime in LangGraph.
        # Its managed state and context are the untyped injection boundary.
        native = cast(LangChainToolRuntime[Any, Any], request.runtime)  # pyright: ignore[reportUnknownMemberType]
        runtime: ToolRuntime[Any, object, Any] = ToolRuntime(
            state=native.state,
            context=native.context,
            config=native.config,
            stream_writer=native.stream_writer,
            tool_call_id=native.tool_call_id,
            store=native.store,
            tools=native.tools,
            execution_info=native.execution_info,
            server_info=native.server_info,
        )
        runtime._scope = self._scope
        return replace(request, runtime=runtime)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return handler(self._request(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        return await handler(self._request(request))


__all__ = ["_ToolRuntimeMiddleware"]
