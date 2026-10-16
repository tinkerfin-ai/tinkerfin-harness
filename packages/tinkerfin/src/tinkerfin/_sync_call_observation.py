"""Native synchronous LangChain callbacks delivered through the owning Run loop."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

from ._call_observation import (
    _CURRENT_CALL_ID,
    _CURRENT_NAMESPACE,
    _SYNC_CALLBACK,
    RuntimeCallHandler,
    _callback_namespace,
)
from ._observation import _CALLBACK_TIMESTAMP, RuntimeObservationHub


@dataclass(frozen=True, slots=True)
class _CallScope:
    call_id: str
    call_token: Token[str | None]
    namespace_token: Token[tuple[str, ...]]


_SCOPES: ContextVar[tuple[_CallScope, ...]] = ContextVar(
    "tinkerfin_sync_call_scopes", default=()
)


class SyncRuntimeCallHandler(BaseCallbackHandler):
    """Wait for durable observation before a synchronous provider or Tool executes.

    LangChain 1.5.3 ``handle_event`` preserves synchronous handler failures, whereas
    ``_run_coros`` swallows asynchronous handler failures from a synchronous caller.
    Native sync callbacks therefore bridge to the existing Run loop without creating
    another loop. The LangChain executor bounds the calling threads; this adapter
    creates no executor and preserves the Hub's bounded delivery queue.

    Context tokens remain in the originating callback context so the Tool body can
    inherit its call identity. Late callbacks are cancelled once the Run terminates.
    See test_sync_call_observation.py for the locked public integration contract.
    """

    def __init__(
        self, hub: RuntimeObservationHub, recorder: RuntimeCallHandler
    ) -> None:
        self._hub = hub
        self._recorder = recorder
        self.raise_error = True
        self.run_inline = True

    @property
    def ignore_chain(self) -> bool:
        """Exclude graph orchestration callbacks from call observation."""
        return True

    @property
    def ignore_llm(self) -> bool:
        """Leave callbacks on the Run loop to its asynchronous handler."""
        return self._hub.in_call_loop()

    @property
    def ignore_chat_model(self) -> bool:
        """Select only model starts arriving through a synchronous callback."""
        return self._hub.in_call_loop()

    @property
    def ignore_agent(self) -> bool:
        """Select only Tool callbacks arriving outside the owning Run loop."""
        return self._hub.in_call_loop()

    def _deliver(self, deliver: Callable[[], Awaitable[None]]) -> None:
        loop = self._hub.call_loop
        if not loop.is_running() or loop.is_closed():
            raise asyncio.CancelledError("Runtime callback outlived its event loop")
        sync_token = _SYNC_CALLBACK.set(True)
        clock_token = _CALLBACK_TIMESTAMP.set((datetime.now(UTC), time.monotonic_ns()))
        try:
            coroutine = self._hub.deliver_thread_callback(deliver)
            try:
                submitted = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except RuntimeError:
                coroutine.close()
                if loop.is_closed():
                    raise asyncio.CancelledError from None
                raise
            try:
                submitted.result()
            except FutureCancelledError:
                raise asyncio.CancelledError from None
            except BaseException:
                submitted.cancel()
                raise
        finally:
            _CALLBACK_TIMESTAMP.reset(clock_token)
            _SYNC_CALLBACK.reset(sync_token)

    @staticmethod
    def _enter_scope(call_id: UUID, metadata: Mapping[str, object] | None) -> None:
        scope = _CallScope(
            call_id=str(call_id),
            call_token=_CURRENT_CALL_ID.set(str(call_id)),
            namespace_token=_CURRENT_NAMESPACE.set(_callback_namespace(metadata)),
        )
        _SCOPES.set((*_SCOPES.get(), scope))

    @staticmethod
    def _leave_scope(call_id: UUID) -> None:
        scopes = _SCOPES.get()
        for index in range(len(scopes) - 1, -1, -1):
            scope = scopes[index]
            if scope.call_id != str(call_id):
                continue
            _SCOPES.set(scopes[:index] + scopes[index + 1 :])
            try:
                _CURRENT_CALL_ID.reset(scope.call_token)
                _CURRENT_NAMESPACE.reset(scope.namespace_token)
            except ValueError:
                # A provider may complete in a copied context; the originating
                # executor context owns these tokens and exits with its invocation.
                pass
            return

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Persist the final provider request before synchronous execution."""
        self._deliver(
            lambda: self._recorder.on_chat_model_start(
                serialized,
                messages,
                run_id=run_id,
                parent_run_id=parent_run_id,
                metadata=metadata,
                **kwargs,
            )
        )
        self._enter_scope(run_id, metadata)

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the first synchronous provider output without retaining its text."""
        self._deliver(
            lambda: self._recorder.on_llm_new_token(
                token, run_id=run_id, parent_run_id=parent_run_id, **kwargs
            )
        )

    def on_stream_event(
        self,
        event: Mapping[str, object],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Preserve the first v3 output identity from a synchronous provider."""
        self._deliver(
            lambda: self._recorder.on_stream_event(
                event, run_id=run_id, parent_run_id=parent_run_id, **kwargs
            )
        )

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record provider completion and restore its originating call context."""
        try:
            self._deliver(
                lambda: self._recorder.on_llm_end(
                    response, run_id=run_id, parent_run_id=parent_run_id, **kwargs
                )
            )
        finally:
            self._leave_scope(run_id)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the provider failure without converting cancellation."""
        try:
            self._deliver(
                lambda: self._recorder.on_llm_error(
                    error, run_id=run_id, parent_run_id=parent_run_id, **kwargs
                )
            )
        finally:
            self._leave_scope(run_id)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Reject synchronous Tool execution when its start cannot be observed."""
        self._deliver(
            lambda: self._recorder.on_tool_start(
                serialized,
                input_str,
                run_id=run_id,
                parent_run_id=parent_run_id,
                metadata=metadata,
                inputs=inputs,
                **kwargs,
            )
        )
        self._enter_scope(run_id, metadata)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the actual synchronous Tool result and release its call scope."""
        try:
            self._deliver(
                lambda: self._recorder.on_tool_end(
                    output, run_id=run_id, parent_run_id=parent_run_id, **kwargs
                )
            )
        finally:
            self._leave_scope(run_id)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record a synchronous Tool failure and preserve its control-flow meaning."""
        try:
            self._deliver(
                lambda: self._recorder.on_tool_error(
                    error, run_id=run_id, parent_run_id=parent_run_id, **kwargs
                )
            )
        finally:
            self._leave_scope(run_id)
