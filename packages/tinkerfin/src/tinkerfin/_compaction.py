"""Run explicit compression inside the same checkpoint and backend scope.

Deep Agents 0.7.13 keeps messages immutable and applies a private summary event
to model input. This module shares that event and its archive format and
uses the native tool's eligibility and the configured summary retention policy.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from functools import wraps
from typing import Any, NotRequired, cast

from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.graph import DeepAgentState
from deepagents.middleware.summarization import (
    SUMMARIZATION_EVENT_KEY,
    SUMMARIZATION_SESSION_ID_KEY,
    SummarizationEvent,
    SummarizationMiddleware,
    SummarizationToolMiddleware,
    create_summarization_middleware,
)
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter
from typing_extensions import TypedDict

from ._agent_construction import AgentGraph, resolve_model
from ._agent_spec import AgentSpec
from ._agui_lineage import _read_snapshot, _thread_head
from ._agui_lineage_state import LINEAGE_CONFIG_KEY, LineageMarker
from ._compaction_observation import _CURRENT_COMPACTION, CompactionOperation
from ._middleware_resources import prepare_middleware_resources
from ._state_schema import StateSchemaSource, compose_state_schema
from ._summarization import observe_summarization
from .compaction import CompactionResult
from .errors import TinkerFinLifecycleError
from .plan._state import create_plan_state_schema
from .runtime_profile import DeepAgentsRuntimeProfile

COMPACTION_STATE_KEY = "context_compaction"


class CompactionState(TypedDict):
    context_compaction: NotRequired[dict[str, object] | None]


_EVENT = TypeAdapter(SummarizationEvent)


def _summarizer(spec: AgentSpec[Any]) -> SummarizationMiddleware:
    backend = spec.backend or StateBackend()
    if not isinstance(backend, BackendProtocol):
        raise TypeError("compaction requires a prepared backend")
    # Ordinary graph construction lets the last same-named declaration win.
    for item in reversed(prepare_middleware_resources(spec.middleware)):
        if item.name == "SummarizationMiddleware":
            if not isinstance(item, SummarizationMiddleware):
                raise TinkerFinLifecycleError(
                    "manual compression requires Deep Agents summarization"
                )
            return item
    return observe_summarization(
        create_summarization_middleware(resolve_model(spec.model), backend)
    )


def create_compaction_stream(
    *,
    native: AgentGraph,
    inspect_astream: Callable[..., AsyncIterator[Mapping[str, object]]],
    spec: AgentSpec[Any],
    plan_enabled: bool,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> Callable[..., AsyncIterator[Mapping[str, object]]]:
    """Build a maintenance graph borrowing the ordinary graph's complete state.

    The ordinary graph remains the authority for pending work inspection. Only
    the maintenance node executes; business middleware, tools and the assistant
    model loop are never invoked. StateBackend writes therefore participate in
    the same native node commit as the new effective summary.
    """
    if spec.checkpointer is None:
        raise ValueError("manual compression requires a checkpointer")
    saver = spec.checkpointer
    engine = _summarizer(spec)
    eligibility = SummarizationToolMiddleware(engine)
    base = cast(type[DeepAgentState], native.builder.state_schema)
    if plan_enabled:
        base = create_plan_state_schema(base, middleware=())
    schema = compose_state_schema(
        (
            StateSchemaSource("conversation", base),
            StateSchemaSource("compression", CompactionState),
        ),
        name="CompactionGraphState",
    )

    async def compact(
        state: DeepAgentState, config: RunnableConfig
    ) -> dict[str, object]:
        lineage = config.get("configurable", {}).get(LINEAGE_CONFIG_KEY)
        if not isinstance(lineage, LineageMarker):
            raise TinkerFinLifecycleError("compression requires managed run ownership")

        def result(
            status: str, *, summary: str | None = None, count: int = 0
        ) -> dict[str, object]:
            payload = CompactionResult.model_validate(
                {
                    "run_id": lineage.run_id,
                    "status": status,
                    "summary": summary,
                    "compacted_messages": count,
                }
            )
            return {COMPACTION_STATE_KEY: payload.model_dump(mode="json")}

        operation = _CURRENT_COMPACTION.get()
        assert operation is not None
        messages = state.get("messages", [])
        raw_event = state.get(SUMMARIZATION_EVENT_KEY)
        previous = None if raw_event is None else _EVENT.validate_python(raw_event)
        effective = engine._apply_event_to_messages(messages, previous)
        if not eligibility._is_eligible_for_compaction(effective):
            return result("nothing_to_compact")
        cutoff = engine._determine_cutoff_index(effective)
        absolute = engine._compute_state_cutoff(previous, cutoff)
        previous_cutoff = 0 if previous is None else previous["cutoff_index"]
        if cutoff <= 0 or cutoff >= len(effective) or absolute <= previous_cutoff:
            return result("nothing_to_compact")
        selected = effective[:cutoff]
        operation.selected = selected
        await operation.start()
        backend = engine._backend
        archived, failed_media = await engine._aoffload_inline_media(backend, selected)
        if failed_media:
            raise TinkerFinLifecycleError("compression could not preserve inline media")
        summary = await engine._acreate_summary(archived)
        if not summary:
            raise TinkerFinLifecycleError("compression returned an empty summary")
        session_id = engine._get_session_id(state)
        path = engine._get_history_path(session_id)
        framed = engine._build_new_messages_with_path(summary, path)
        if engine._count_tokens(framed, None, []) >= engine._count_tokens(
            selected, None, []
        ):
            return result("not_reduced")
        await operation.saving()
        get_stream_writer()(
            {
                "operation": "context_compaction",
                "phase": "saving",
                "runId": lineage.run_id,
            }
        )
        saved_path = await engine._aoffload_to_backend(backend, archived, session_id)
        if saved_path is None:
            raise TinkerFinLifecycleError(
                "compression could not archive conversation history"
            )
        summary_message = framed[0]
        if not isinstance(summary_message, HumanMessage):
            raise TinkerFinLifecycleError(
                "compression produced an invalid summary message"
            )
        event: SummarizationEvent = {
            "cutoff_index": absolute,
            "summary_message": summary_message,
            "file_path": saved_path,
        }
        update = {
            **result("compacted", summary=summary, count=absolute - previous_cutoff),
            SUMMARIZATION_EVENT_KEY: event,
            SUMMARIZATION_SESSION_ID_KEY: session_id,
        }
        operation.mark_update(update)
        operation.payload = CompactionResult.model_validate(
            update[COMPACTION_STATE_KEY]
        ).model_dump(mode="json")
        return update

    builder = StateGraph(schema, context_schema=spec.context_schema)
    # Pass the composed input schema explicitly: inferring DeepAgentState from
    # the callback annotation would hide the prior private summary channels.
    builder.add_node("compact_context", compact, input_schema=schema)
    builder.add_edge(START, "compact_context")
    builder.add_edge("compact_context", END)
    graph = builder.compile(checkpointer=saver, store=spec.store)
    graph_stream = runtime_profile.graph_stream(graph)

    async def execute(
        *args: object, **kwargs: object
    ) -> AsyncGenerator[Mapping[str, object], None]:
        bound = inspect.signature(graph.astream).bind(*args, **kwargs)
        config = cast(RunnableConfig, bound.arguments.get("config", {}))
        thread_id = config.get("configurable", {}).get("thread_id")
        if not isinstance(thread_id, str):
            raise TinkerFinLifecycleError("compression requires a thread identity")
        head = await _thread_head(saver, thread_id=thread_id)
        if head is not None:
            snapshot = await _read_snapshot(inspect_astream, head)
            if snapshot.next or snapshot.interrupts:
                raise TinkerFinLifecycleError("conversation has pending work")
            plan = snapshot.values.get("tinkerfin_plan")
            if isinstance(plan, dict) and plan.get("status") == "awaiting_input":
                raise TinkerFinLifecycleError("conversation is waiting for Plan input")
        bound.arguments["input"] = {"messages": [], COMPACTION_STATE_KEY: None}
        bound.arguments["durability"] = "sync"
        source = graph_stream(*bound.args, **bound.kwargs)
        if not isinstance(source, AsyncGenerator):
            raise TypeError(
                "compression graph must return a closeable asynchronous generator"
            )
        committed: Mapping[str, object] | None = None
        try:
            async for part in source:
                # The native call keeps the selected Profile's semantic modes. Only
                # saved state and explicit progress belong to this operation's
                # public stream; summary tokens are not assistant messages.
                data = part.get("data")
                if (
                    part.get("type") == "values"
                    and isinstance(data, Mapping)
                    and data.get(COMPACTION_STATE_KEY) is not None
                ):
                    # Native values may precede the final checkpointer await.
                    # Publish the result only after the graph closes successfully.
                    committed = cast(Mapping[str, object], part)
                elif part.get("type") in {"values", "custom"}:
                    yield cast(Mapping[str, object], part)
        finally:
            await source.aclose()
        if committed is not None:
            yield committed

    @wraps(graph.astream)
    async def stream(
        *args: object, **kwargs: object
    ) -> AsyncIterator[Mapping[str, object]]:
        operation = CompactionOperation("manual")
        token = _CURRENT_COMPACTION.set(operation)
        source = execute(*args, **kwargs)
        try:
            async for part in source:
                data = part.get("data")
                if part.get("type") == "values" and isinstance(data, Mapping):
                    payload = data.get(COMPACTION_STATE_KEY)
                    if payload is not None:
                        result = CompactionResult.model_validate(payload)
                        await operation.complete(result.model_dump(mode="json"))
                yield part
        except BaseException as error:
            await operation.fail(error)
            raise
        finally:
            await source.aclose()
            _CURRENT_COMPACTION.reset(token)

    return stream
