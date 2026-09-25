"""LangChain call observation and explicit semantic contribution lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import LLMResult
from langgraph.errors import GraphBubbleUp, GraphInterrupt
from pydantic import JsonValue

from tinkerfin_contracts import (
    ContextContributionObservation,
    ContextKind,
    ModelCallObservation,
    ObservationBoundary,
    RunTerminalOutcome,
    ToolExecutionObservation,
)

from ._observation import (
    _json_object,
    _message_record,
    _qualified_name,
    _source_value,
    _stamp,
)
from .errors import TinkerFinStreamProtocolError

if TYPE_CHECKING:
    from ._observation import RuntimeObservationHub

_CallTerminalPhase = Literal[
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "abandoned",
]
_ModelTerminalPhase = _CallTerminalPhase
_ToolTerminalPhase = _CallTerminalPhase


_CURRENT_HUB: ContextVar[RuntimeObservationHub | None] = ContextVar(
    "tinkerfin_current_observation_hub",
    default=None,
)
_CURRENT_CALL_ID: ContextVar[str | None] = ContextVar(
    "tinkerfin_current_call_id",
    default=None,
)
_CURRENT_NAMESPACE: ContextVar[tuple[str, ...]] = ContextVar(
    "tinkerfin_current_call_namespace",
    default=(),
)
_SYNC_CALLBACK: ContextVar[bool] = ContextVar("tinkerfin_sync_callback", default=False)


def bind_observation_hub(
    hub: RuntimeObservationHub,
) -> Token[RuntimeObservationHub | None]:
    """Bind one Runtime hub to work executed by the current upstream pull."""

    return _CURRENT_HUB.set(hub)


def reset_observation_hub(token: Token[RuntimeObservationHub | None]) -> None:
    """Restore the observation context after one upstream pull settles."""

    _CURRENT_HUB.reset(token)


def _checkpoint_segments(
    metadata: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """Return canonical locked checkpoint segments without assigning semantics."""

    if metadata is None:
        return ()
    raw_namespace = metadata.get("langgraph_checkpoint_ns")
    if not isinstance(raw_namespace, str) or not raw_namespace:
        return ()
    segments = tuple(raw_namespace.split("|"))
    return () if any(not segment for segment in segments) else segments


def _callback_namespace(metadata: Mapping[str, object] | None) -> tuple[str, ...]:
    """Resolve the locked LangGraph callback scope to its Native namespace.

    LangGraph 1.2.11 appends the current graph node to
    ``langgraph_checkpoint_ns``. Removing that final segment yields the same child
    namespace carried by the validated v2 Native stream. The locked dependency contract
    test protects this mapping for root and subagent calls.
    """

    segments = _checkpoint_segments(metadata)
    return segments[:-1]


def _optional_text(value: object) -> str | None:
    return (
        value if isinstance(value, str) and value and value == value.strip() else None
    )


def _callback_graph_task_id(metadata: Mapping[str, object] | None) -> str | None:
    """Preserve the executing Graph task independently of callback delivery order.

    LangGraph 1.2.11 ``pregel._algo`` places ``<node>:<task_id>`` in the final
    checkpoint namespace segment. Parent graph segments, including repeated-subgraph
    counters, remain in ``graph_namespace``. Non-Graph Tool calls have no segment.
    """

    segments = _checkpoint_segments(metadata)
    if not segments:
        return None
    node, separator, task_id = segments[-1].partition(":")
    if not node or not separator or not task_id:
        raise TinkerFinStreamProtocolError(
            "Tool callback has invalid Graph task identity"
        )
    return task_id


def _agent_name(metadata: Mapping[str, object] | None) -> str | None:
    return None if metadata is None else _optional_text(metadata.get("lc_agent_name"))


def _provider_and_model(
    metadata: Mapping[str, object] | None,
    invocation: Mapping[str, object],
) -> tuple[str | None, str | None]:
    provider = None if metadata is None else _optional_text(metadata.get("ls_provider"))
    model = next(
        (
            text
            for key in ("model", "model_name", "model_id")
            if (text := _optional_text(invocation.get(key))) is not None
        ),
        None,
    )
    if model is None and metadata is not None:
        model = _optional_text(metadata.get("ls_model_name"))
    return provider, model


def _parent_id(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _error_message(error: BaseException) -> str | None:
    """Return one bounded exception message for observer-controlled retention."""

    message = str(error).strip()
    return message if message and len(message) <= 4096 else None


def _control_flow_phase(
    error: BaseException,
) -> Literal["cancelled", "interrupted", "abandoned"] | None:
    """Classify process control without turning it into an execution failure."""

    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, GraphInterrupt):
        return "interrupted"
    if isinstance(error, GraphBubbleUp):
        return "abandoned"
    return None


def _settles_with_run(error: BaseException) -> bool:
    """Return whether only the authoritative Run terminal can classify closure."""

    return isinstance(error, GeneratorExit)


def _response_values(
    response: LLMResult,
) -> tuple[dict[str, JsonValue] | None, dict[str, JsonValue] | None]:
    usage: dict[str, JsonValue] | None = None
    response_metadata: dict[str, JsonValue] | None = None
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            if not isinstance(message, BaseMessage):
                continue
            raw_usage = getattr(message, "usage_metadata", None)
            if raw_usage is not None:
                usage = _json_object(raw_usage)
            if message.response_metadata:
                response_metadata = _json_object(message.response_metadata)
            break
        if usage is not None or response_metadata is not None:
            break
    if usage is None and isinstance(response.llm_output, Mapping):
        raw_usage = response.llm_output.get("token_usage")
        if raw_usage is None:
            raw_usage = response.llm_output.get("usage")
        if raw_usage is not None:
            normalized = _source_value(raw_usage)
            if isinstance(normalized, dict):
                usage = normalized
    return usage, response_metadata


def _response_tool_call_ids(response: LLMResult) -> tuple[str, ...]:
    """Return stable Tool proposal IDs from one completed provider response."""

    values: list[str] = []
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            if not isinstance(message, BaseMessage):
                continue
            raw_calls: object = getattr(message, "tool_calls", ())
            if not isinstance(raw_calls, list | tuple):
                continue
            for call in cast(Sequence[object], raw_calls):
                if not isinstance(call, Mapping):
                    continue
                resolved_call = cast(Mapping[str, object], call)
                call_id = _optional_text(resolved_call.get("id"))
                if call_id is not None and call_id not in values:
                    values.append(call_id)
    return tuple(values)


def _response_message_ids(response: LLMResult) -> tuple[str, ...]:
    """Return stable message identities from one completed chat-model response."""

    values: list[str] = []
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            if not isinstance(message, BaseMessage):
                continue
            message_id = _optional_text(message.id)
            if message_id is not None and message_id not in values:
                values.append(message_id)
    return tuple(values)


def _chunk_message_ids(value: object) -> tuple[str, ...]:
    """Return the message identity carried by a locked callback chunk, if present."""

    message = getattr(value, "message", None)
    if not isinstance(message, BaseMessage):
        return ()
    message_id = _optional_text(message.id)
    return () if message_id is None else (message_id,)


@dataclass(frozen=True, slots=True)
class _ModelCallState:
    parent_call_id: str | None
    namespace: tuple[str, ...]
    agent_name: str | None
    internal: bool


@dataclass(frozen=True, slots=True)
class _ToolCallState:
    parent_call_id: str | None
    namespace: tuple[str, ...]
    graph_task_id: str | None
    agent_name: str | None
    tool_call_id: str | None
    tool_name: str


class RuntimeCallHandler(AsyncCallbackHandler):
    """Translate one managed LangChain callback tree into Runtime observations.

    The handler is request-scoped and borrowed by LangChain through the invocation
    config. It records provider and Tool callbacks; chain and middleware callbacks are
    deliberately ignored because they do not define product execution events. Native
    state, task payloads, interrupts, messages, and subagent completion remain owned by
    the Native Driver. Parallel callbacks are serialized before Observer delivery.
    """

    def __init__(self, hub: RuntimeObservationHub) -> None:
        self.raise_error = True
        # LangChain otherwise schedules this handler in a sibling task, so the Tool or
        # provider body cannot inherit the call scope established by its start callback.
        # Inline callbacks preserve that public contribution-to-call relationship while
        # the hub remains the single ordered delivery boundary.
        self.run_inline = True
        self._hub = hub
        self._models: dict[str, _ModelCallState] = {}
        self._tools: dict[str, _ToolCallState] = {}
        self._first_outputs: set[str] = set()
        self._call_tokens: dict[str, Token[str | None]] = {}
        self._namespace_tokens: dict[str, Token[tuple[str, ...]]] = {}

    @property
    def ignore_chain(self) -> bool:
        """Exclude chain and middleware callbacks from semantic observations."""

        return True

    @property
    def ignore_llm(self) -> bool:
        """Leave synchronous provider callbacks to the native thread adapter."""
        return not self._hub.in_call_loop()

    @property
    def ignore_chat_model(self) -> bool:
        """Handle model starts only on the Run's asynchronous callback surface."""
        return not self._hub.in_call_loop()

    @property
    def ignore_agent(self) -> bool:
        """Leave synchronous Tool callbacks to the native thread adapter."""
        return not self._hub.in_call_loop()

    def _call_parent(
        self,
        parent_run_id: UUID | None,
        metadata: Mapping[str, object] | None,
    ) -> str | None:
        del metadata
        return _parent_id(parent_run_id)

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Persist the final middleware-processed request before provider execution."""

        del serialized
        if len(messages) != 1:
            raise ValueError(
                "managed model callbacks require exactly one request batch"
            )
        raw_invocation: object = kwargs.get("invocation_params")
        invocation_map: Mapping[str, object] = (
            cast(Mapping[str, object], raw_invocation)
            if isinstance(raw_invocation, Mapping)
            else dict[str, object]()
        )
        raw_options: object = kwargs.get("options")
        provider, model = _provider_and_model(metadata, invocation_map)
        from langchain.agents.middleware.internal_call_transformer import (
            internal_call_metadata,
        )

        internal = all(
            (metadata or {}).get(key) == value
            for key, value in internal_call_metadata().items()
        )
        from ._compaction_observation import _SUMMARY_OPERATION

        call_id = str(run_id)
        state = _ModelCallState(
            parent_call_id=self._call_parent(parent_run_id, metadata),
            namespace=_callback_namespace(metadata),
            agent_name=_agent_name(metadata),
            internal=internal,
        )
        observed_at, monotonic_ns = _stamp()
        observation = ModelCallObservation(
            identity=self._hub.context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            phase="started",
            call_id=call_id,
            contribution_id=_SUMMARY_OPERATION.get(),
            parent_call_id=state.parent_call_id,
            graph_namespace=state.namespace,
            agent_name=state.agent_name,
            provider=provider,
            model=model,
            messages=tuple(_message_record(message) for message in messages[0]),
            invocation=_source_value(cast(object, raw_invocation)),
            options=_source_value(raw_options),
            output_message_ids=(),
        )
        if call_id in self._models:
            raise ValueError("model callback run ID started more than once")
        self._models[call_id] = state
        await self._hub.observe(observation)
        operation = self._hub.compactions.get(_SUMMARY_OPERATION.get() or "")
        if operation is not None:
            await operation.model_started(call_id)
        await self._hub.force(ObservationBoundary.CALL_STARTED)
        if not _SYNC_CALLBACK.get():
            self._call_tokens[call_id] = _CURRENT_CALL_ID.set(call_id)
            self._namespace_tokens[call_id] = _CURRENT_NAMESPACE.set(state.namespace)

    async def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record only the first provider output timestamp, never token content."""

        del token, parent_run_id
        await self._observe_first_model_output(
            str(run_id),
            output_message_ids=_chunk_message_ids(kwargs.get("chunk")),
        )

    async def on_stream_event(
        self,
        event: Mapping[str, object],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the first v3 message boundary without retaining event content."""

        del parent_run_id, kwargs
        if event.get("event") != "message-start":
            return
        message_id = _optional_text(event.get("id"))
        if message_id is None:
            raise TinkerFinStreamProtocolError(
                "v3 message start requires a stable message ID"
            )
        await self._observe_first_model_output(
            str(run_id),
            output_message_ids=(message_id,),
        )

    async def _observe_first_model_output(
        self,
        call_id: str,
        *,
        output_message_ids: tuple[str, ...],
    ) -> None:
        """Emit one provider first-output fact across supported callback surfaces."""

        state = self._models.get(call_id)
        if state is None or call_id in self._first_outputs:
            return
        self._first_outputs.add(call_id)
        observed_at, monotonic_ns = _stamp()
        await self._hub.observe(
            ModelCallObservation(
                identity=self._hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase="first_output",
                call_id=call_id,
                parent_call_id=state.parent_call_id,
                graph_namespace=state.namespace,
                agent_name=state.agent_name,
                output_message_ids=() if state.internal else output_message_ids,
            )
        )

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one successful provider call with bounded usage metadata."""

        del parent_run_id, kwargs
        usage, response_metadata = _response_values(response)
        await self._close_model(
            str(run_id),
            phase="completed",
            usage=usage,
            response_metadata=response_metadata,
            output_message_ids=_response_message_ids(response),
            tool_call_ids=_response_tool_call_ids(response),
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one failed or cancelled provider call without swallowing cancellation."""

        del parent_run_id, kwargs
        if _settles_with_run(error):
            return
        control_phase = _control_flow_phase(error)
        if control_phase is not None:
            await self._close_model(str(run_id), phase=control_phase)
            return
        failure_origin = self._claim_error(error)
        await self._close_model(
            str(run_id),
            phase="failed",
            error_type=_qualified_name(error),
            error_message=_error_message(error),
            failure_origin=failure_origin,
        )

    async def _close_model(
        self,
        call_id: str,
        *,
        phase: _ModelTerminalPhase,
        usage: dict[str, JsonValue] | None = None,
        response_metadata: dict[str, JsonValue] | None = None,
        output_message_ids: tuple[str, ...] = (),
        tool_call_ids: tuple[str, ...] = (),
        error_type: str | None = None,
        error_message: str | None = None,
        failure_origin: bool = False,
    ) -> None:
        state = self._models.get(call_id)
        if state is None:
            return
        observed_at, monotonic_ns = _stamp()
        await self._hub.observe(
            ModelCallObservation(
                identity=self._hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase=phase,
                call_id=call_id,
                parent_call_id=state.parent_call_id,
                graph_namespace=state.namespace,
                agent_name=state.agent_name,
                usage=usage,
                response_metadata=response_metadata,
                # LangChain marks middleware summaries as internal calls. Keep
                # usage and failures observable without creating chat messages.
                output_message_ids=() if state.internal else output_message_ids,
                tool_call_ids=() if state.internal else tool_call_ids,
                error_type=error_type,
                error_message=error_message,
                failure_origin=failure_origin,
            )
        )
        self._models.pop(call_id, None)
        self._first_outputs.discard(call_id)
        self._reset_call_token(call_id)

    async def on_tool_start(
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
        """Persist the actual Tool input after approval and middleware editing."""

        raw_name = serialized.get("name")
        tool_name = _optional_text(raw_name)
        if tool_name is None:
            raise ValueError("Tool callback requires a canonical name")
        raw_tool_call_id = kwargs.get("tool_call_id")
        tool_call_id = _optional_text(raw_tool_call_id)
        execution_id = str(run_id)
        state = _ToolCallState(
            parent_call_id=self._call_parent(parent_run_id, metadata),
            namespace=_callback_namespace(metadata),
            graph_task_id=_callback_graph_task_id(metadata),
            agent_name=_agent_name(metadata),
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        actual_input: object = inputs if inputs is not None else input_str
        observed_at, monotonic_ns = _stamp()
        observation = ToolExecutionObservation(
            identity=self._hub.context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            phase="started",
            execution_id=execution_id,
            parent_call_id=state.parent_call_id,
            graph_namespace=state.namespace,
            graph_task_id=state.graph_task_id,
            agent_name=state.agent_name,
            tool_call_id=state.tool_call_id,
            tool_name=state.tool_name,
            input=_source_value(actual_input),
        )
        if execution_id in self._tools:
            raise ValueError("Tool callback run ID started more than once")
        self._tools[execution_id] = state
        await self._hub.observe(observation)
        await self._hub.force(ObservationBoundary.CALL_STARTED)
        if not _SYNC_CALLBACK.get():
            self._call_tokens[execution_id] = _CURRENT_CALL_ID.set(execution_id)
            self._namespace_tokens[execution_id] = _CURRENT_NAMESPACE.set(
                state.namespace
            )

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one Tool execution, including handled error Tool messages."""

        del parent_run_id, kwargs
        handled_error = isinstance(output, ToolMessage) and output.status == "error"
        await self._close_tool(
            str(run_id),
            phase="failed" if handled_error else "completed",
            output=_source_value(output),
            failure_origin=handled_error,
        )

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one Tool exception while preserving its real error classification."""

        del parent_run_id, kwargs
        if _settles_with_run(error):
            return
        control_phase = _control_flow_phase(error)
        if control_phase is not None:
            await self._close_tool(str(run_id), phase=control_phase)
            return
        failure_origin = self._claim_error(error)
        await self._close_tool(
            str(run_id),
            phase="failed",
            error_type=_qualified_name(error),
            error_message=_error_message(error),
            failure_origin=failure_origin,
        )

    async def _close_tool(
        self,
        execution_id: str,
        *,
        phase: _ToolTerminalPhase,
        output: JsonValue | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        failure_origin: bool = False,
    ) -> None:
        state = self._tools.get(execution_id)
        if state is None:
            return
        observed_at, monotonic_ns = _stamp()
        await self._hub.observe(
            ToolExecutionObservation(
                identity=self._hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase=phase,
                execution_id=execution_id,
                parent_call_id=state.parent_call_id,
                graph_namespace=state.namespace,
                graph_task_id=state.graph_task_id,
                agent_name=state.agent_name,
                tool_call_id=state.tool_call_id,
                tool_name=state.tool_name,
                output=output,
                error_type=error_type,
                error_message=error_message,
                failure_origin=failure_origin,
            )
        )
        self._tools.pop(execution_id, None)
        self._reset_call_token(execution_id)

    async def settle(
        self,
        outcome: RunTerminalOutcome,
        *,
        error: BaseException | None,
    ) -> None:
        """Close callbacks that upstream cancellation or failure left unmatched."""

        del error
        phase: Literal["cancelled", "interrupted", "abandoned"]
        if outcome == "cancelled":
            phase = "cancelled"
        elif outcome == "interrupted":
            phase = "interrupted"
        else:
            phase = "abandoned"
        models = tuple(self._models.items())
        tools = tuple(self._tools.items())
        self._models.clear()
        self._tools.clear()
        self._first_outputs.clear()
        for call_id, state in models:
            observed_at, monotonic_ns = _stamp()
            await self._hub.observe(
                ModelCallObservation(
                    identity=self._hub.context.identity,
                    observed_at=observed_at,
                    monotonic_ns=monotonic_ns,
                    phase=phase,
                    call_id=call_id,
                    parent_call_id=state.parent_call_id,
                    graph_namespace=state.namespace,
                    agent_name=state.agent_name,
                    output_message_ids=(),
                )
            )
        for execution_id, state in tools:
            observed_at, monotonic_ns = _stamp()
            await self._hub.observe(
                ToolExecutionObservation(
                    identity=self._hub.context.identity,
                    observed_at=observed_at,
                    monotonic_ns=monotonic_ns,
                    phase=phase,
                    execution_id=execution_id,
                    parent_call_id=state.parent_call_id,
                    graph_namespace=state.namespace,
                    graph_task_id=state.graph_task_id,
                    agent_name=state.agent_name,
                    tool_call_id=state.tool_call_id,
                    tool_name=state.tool_name,
                )
            )
        self._call_tokens.clear()
        self._namespace_tokens.clear()

    def _claim_error(self, error: BaseException) -> bool:
        """Return whether this callback is the first owner of one propagated failure."""

        return self._hub.claim_error(error)

    def _reset_call_token(self, call_id: str) -> None:
        token = self._call_tokens.pop(call_id, None)
        namespace_token = self._namespace_tokens.pop(call_id, None)
        if token is not None:
            try:
                _CURRENT_CALL_ID.reset(token)
            except ValueError:
                # Some providers finish callbacks from their shielded producer task.
                # That task owns its copied context and exits after the callback.
                pass
        if namespace_token is not None:
            try:
                _CURRENT_NAMESPACE.reset(namespace_token)
            except ValueError:
                pass


class TraceContribution:
    """Collect an optional semantic result for one explicit contribution scope."""

    __slots__ = ("_result", "_result_set")

    def __init__(self) -> None:
        self._result: object = None
        self._result_set = False

    def set_result(self, result: object) -> None:
        """Attach a result captured when the contribution completes successfully."""

        self._result = result
        self._result_set = True


@asynccontextmanager
async def trace_contribution(
    *,
    kind: ContextKind,
    name: str,
    input: object = None,
) -> AsyncGenerator[TraceContribution, None]:
    """Expose one product-semantic context action to active Runtime observers.

    The scope is a no-op when called outside a managed observed Run. This lets reusable
    middleware and Tools publish Memory, Guardrail, retrieval, or custom semantics
    without depending on a Tracer implementation or checking host configuration.

    Args:
        kind: Stable functional category shown to Trace consumers.
        name: User-facing action name.
        input: Optional public input subject to each Observer's capture policy.

    Yields:
        A contribution value whose optional result is recorded on success.

    Raises:
        TypeError: ``kind`` or ``name`` has the wrong type.
        ValueError: ``name`` is blank or not canonical.
        BaseException: The contribution body or active Observer fails.
    """

    if kind not in {"memory", "guardrail", "retrieval", "custom", "compaction"}:
        raise ValueError("kind must be memory, guardrail, retrieval, or custom")
    if not isinstance(name, str):
        raise TypeError("name must be text")
    if not name or name != name.strip():
        raise ValueError("name must be canonical non-empty text")
    value = TraceContribution()
    hub = _CURRENT_HUB.get()
    if hub is None or not hub.enabled:
        yield value
        return
    contribution_id = uuid4().hex
    parent_call_id = _CURRENT_CALL_ID.get()
    namespace = _CURRENT_NAMESPACE.get()
    observed_at, monotonic_ns = _stamp()
    await hub.observe(
        ContextContributionObservation(
            identity=hub.context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            phase="started",
            contribution_id=contribution_id,
            parent_call_id=parent_call_id,
            graph_namespace=namespace,
            context_kind=kind,
            name=name,
            input=None if input is None else _source_value(input),
        )
    )
    try:
        yield value
    except BaseException as error:
        observed_at, monotonic_ns = _stamp()
        control_phase = _control_flow_phase(error)
        settles_with_run = _settles_with_run(error)
        await hub.observe(
            ContextContributionObservation(
                identity=hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase=(
                    "abandoned" if settles_with_run else (control_phase or "failed")
                ),
                contribution_id=contribution_id,
                parent_call_id=parent_call_id,
                graph_namespace=namespace,
                context_kind=kind,
                name=name,
                error_type=(
                    None
                    if control_phase is not None or settles_with_run
                    else _qualified_name(error)
                ),
                failure_origin=(
                    control_phase is None
                    and not settles_with_run
                    and hub.claim_error(error)
                ),
            )
        )
        raise
    else:
        observed_at, monotonic_ns = _stamp()
        await hub.observe(
            ContextContributionObservation(
                identity=hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase="completed",
                contribution_id=contribution_id,
                parent_call_id=parent_call_id,
                graph_namespace=namespace,
                context_kind=kind,
                name=name,
                output=_source_value(value._result) if value._result_set else None,
            )
        )


__all__ = [
    "RuntimeCallHandler",
    "TraceContribution",
    "bind_observation_hub",
    "reset_observation_hub",
    "trace_contribution",
]
