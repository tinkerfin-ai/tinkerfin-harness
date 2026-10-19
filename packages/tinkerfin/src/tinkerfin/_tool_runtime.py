"""Supply managed context, identity and workspace to framework tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import replace
from typing import Any, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain.tools import ToolRuntime as LangChainToolRuntime
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.config import get_config
from langgraph.runtime import Runtime, get_runtime
from langgraph.types import Command
from pydantic import Field, JsonValue

from ._file_paths import (
    FILE_CONTRACT_CHANNEL,
    FileContractState,
    _WorkspaceFilePaths,
    file_contract_digest,
    validate_file_contract,
)
from ._workspace_operations import WorkspaceOperations
from ._workspace_tools import model_tool_declarations
from .tools import ToolRuntime, _ToolRunScope


class _InvocationTool(BaseTool):
    """Keep ToolNode validation while binding execution to its durable task.

    LangGraph 1.2.11 ToolNode._arun_one closes over its original config. Forwarding
    that config would replace the functional task's scratchpad and enter the wrong
    child checkpoint. Only this owned delegation boundary substitutes the current
    execution config; the original tool retains its schema, callbacks and result.
    """

    original: BaseTool = Field(exclude=True)
    invocation: RunnableConfig = Field(exclude=True)

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "Persistent delegation requires asynchronous execution"
        )

    async def ainvoke(
        self,
        input: str | dict[str, Any] | ToolCall,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self.original.ainvoke(input, self.invocation, **kwargs)


class _ToolRuntimeMiddleware(AgentMiddleware):
    """Supply tool-scoped access and preserve the proposal's file declarations."""

    state_schema = FileContractState

    def __init__(
        self,
        scope: _ToolRunScope[object] | None,
        file_paths: _WorkspaceFilePaths | None = None,
    ) -> None:
        self._scope = scope
        self._file_paths = file_paths
        self._operations = None if scope is None else WorkspaceOperations(scope)

    def after_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object]:
        # This hook runs first under LangChain 1.4.2's reverse middleware order.
        # A fresh proposal replaces completed history's private evidence; resuming
        # its review or tool node retains the original declaration set instead.
        return {FILE_CONTRACT_CHANNEL: file_contract_digest(self._file_paths)}

    async def aafter_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object]:
        return self.after_model(state, runtime)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        return handler(request.override(tools=model_tool_declarations(request.tools)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        return await handler(
            request.override(tools=model_tool_declarations(request.tools))
        )

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
        validate_file_contract(request.state, self._file_paths)
        if self._file_paths is not None:
            raise NotImplementedError(
                "Workspace file paths require asynchronous execution"
            )
        return handler(self._request(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        from ._durable_delegation import (
            current_delegation_attempt,
            current_delegation_dispatch,
        )
        from .errors import DelegationReplayError

        validate_file_contract(request.state, self._file_paths)
        prepared = self._request(request)
        if self._file_paths is not None:
            prepared = await self._file_paths.prepare_execution(prepared)
        attempt = current_delegation_attempt()
        dispatch = current_delegation_dispatch()
        if attempt is not None and attempt.owns_request(prepared):
            try:
                await attempt.accept_request(prepared)
            except DelegationReplayError as error:
                if dispatch is not None:
                    dispatch.conflict = error
                raise
            config = get_config()
            native = cast(LangChainToolRuntime[Any, Any], prepared.runtime)  # pyright: ignore[reportUnknownMemberType]
            runtime = replace(
                native,
                config=config,
                execution_info=get_runtime().execution_info,
            )
            # ToolRuntime's managed workspace is excluded from dataclass init.
            if isinstance(runtime, ToolRuntime):
                runtime._scope = self._scope
            original = prepared.tool
            if original is None:
                raise ValueError("managed delegation has no tool")
            prepared = replace(
                prepared,
                runtime=runtime,
                tool=_InvocationTool(
                    name=original.name,
                    description=original.description,
                    args_schema=original.args_schema,
                    return_direct=original.return_direct,
                    original=original,
                    invocation=config,
                ),
            )
        elif dispatch is not None and dispatch.owns_request(prepared):
            dispatch.accept_direct(prepared)

        async def emit_output(value: JsonValue) -> None:
            native = cast(LangChainToolRuntime[Any, Any], prepared.runtime)  # pyright: ignore[reportUnknownMemberType]
            native.stream_writer(
                {
                    "operation": "tool_output",
                    "toolCallId": prepared.tool_call["id"],
                    "value": value,
                }
            )

        with (
            nullcontext()
            if self._operations is None
            else self._operations.tool(
                prepared.tool_call, prepared.state, emit_output=emit_output
            )
        ):
            return await handler(prepared)


__all__ = ["_ToolRuntimeMiddleware"]
