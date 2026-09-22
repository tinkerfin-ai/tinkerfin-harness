"""Private per-Run semantic mapping and bounded writer lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from pydantic import JsonValue

from tinkerfin_contracts import (
    ContextContributionObservation,
    ModelCallObservation,
    NativeExtraObservation,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeReasoningObservation,
    NativeStateObservation,
    NativeTaskObservation,
    ObservationBoundary,
    RunClosedObservation,
    RunInputObservation,
    RunObserverFailedObservation,
    RunResumeCheckpointedObservation,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RuntimeObservation,
    ToolExecutionObservation,
)

from ._ids import scope_id as _scope_id
from ._observation_values import (
    append_json_content,
    classify_interaction,
    discard_private_state,
    extract_user_message,
    fingerprint,
    interaction_tool_call_ids,
    make_fact,
    message_role,
    parsed_json,
    plan_metadata,
)
from .capture import (
    CapturedValue,
    CapturePolicy,
    ReasoningCapturePolicy,
    TraceCapturePipeline,
)
from .errors import TraceCaptureRejected, TraceCorruption
from .facts import (
    CallTrackingFact,
    ContextContributionFact,
    InteractionFact,
    MessageFact,
    ModelCallFact,
    NativeExtraFact,
    PlanRevisionFact,
    ReasoningFact,
    RunFact,
    StateRevisionFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceEvent,
    TraceSemanticFact,
    TurnFact,
)
from .limits import TraceLimits
from .query import (
    load_core_projection_state,
)
from .redaction import (
    RedactionContentKind,
    RedactionContext,
    TraceRedactor,
)
from .store import (
    TraceStore,
    TraceWriter,
)
from .writing import TraceBatchWriter, TraceWritePolicy


@dataclass(frozen=True, slots=True)
class _PendingInteraction:
    """Retain exact review correlation until resolution or session hydration."""

    kind: str
    tool_call_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SubagentDescriptor:
    """Retain one proven Deep Agents task Tool to child-namespace relationship."""

    parent_tool_call_id: str
    parent_task_id: str
    agent_name: str
    description: str


@dataclass(frozen=True, slots=True)
class _PendingToolExecution:
    """Retain actual Tool input until its execution callback reaches a terminal."""

    input: JsonValue | None
    parent_call_id: str | None
    namespace: tuple[str, ...]
    agent_name: str | None
    tool_name: str
    traced: bool
    observed_at: datetime
    monotonic_ns: int


@dataclass(frozen=True, slots=True)
class _ContextAnchor:
    """Retain the latest visible boundary that precedes context preparation."""

    occurred_at: datetime
    monotonic_ns: int | None


class _TracingSession:
    """Own one Run writer and its ordered, bounded semantic mapping lifecycle.

    Observations arrive serially from one Runtime session. The session owns the
    ``TraceBatchWriter`` worker but only borrows the Store and writer contract returned
    by that Store. Dedupe, Tool fragments, state baselines, and pending interactions are
    advanced only after facts are accepted in order. Terminal and close facts use the
    Store's mandatory reserve after ordinary reasoning completion is forced separately.
    Any worker failure becomes the active Observer failure and is never hidden by close.
    """

    def __init__(
        self,
        *,
        writer: TraceWriter,
        store: TraceStore,
        context: RunSourceContext,
        capture_policy: CapturePolicy,
        reasoning_capture_policy: ReasoningCapturePolicy,
        redactor: TraceRedactor | None,
        limits: TraceLimits,
        write_policy: TraceWritePolicy,
        on_closed: Callable[[_TracingSession], None],
        prior_events: tuple[TraceEvent, ...] = (),
    ) -> None:
        """Initialize one request session from the current lineage prefix.

        Args:
            writer: Run-exclusive Store writer transferred to this session.
            store: Borrowed Store used for projection checkpoint advancement.
            context: Immutable Runtime input and privacy facts for this Run.
            capture_policy: Public semantic payload capture policy.
            reasoning_capture_policy: Independent provider reasoning retention policy.
            redactor: Optional business value Redactor applied after framework safety.
            limits: Store-aligned event and payload capacity limits.
            write_policy: Batching, pending-byte, backpressure, and delay policy.
            on_closed: Callback that removes this session from its Tracer owner.
            prior_events: Selected lineage facts used only to hydrate dedupe state.

        Raises:
            TraceCorruption: Prior facts cannot hydrate one deterministic state.
        """

        self._writer = writer
        self._store = store
        self._context = context
        self._policy = capture_policy
        self._capture_pipeline = TraceCapturePipeline(
            policy=capture_policy,
            reasoning_policy=reasoning_capture_policy,
            redactor=redactor,
        )
        self._limits = limits
        self._on_closed = on_closed
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._observation_index = 0
        self._run_scope = hashlib.sha256(writer.run_id.encode("utf-8")).hexdigest()
        self._private_state_keys = frozenset(context.private_state_keys)
        self._message_fingerprints: dict[tuple[tuple[str, ...], str], str] = {}
        self._delivered_message_fingerprints: dict[
            tuple[tuple[str, ...], str], str
        ] = {}
        self._initial_state_scopes: set[tuple[str, ...]] = set()
        self._inheritable_message_fingerprints: dict[
            tuple[tuple[str, ...], str], str
        ] = {}
        self._inherited_message_fingerprints: dict[
            tuple[tuple[str, ...], str], str
        ] = {}
        self._message_seen: set[tuple[tuple[str, ...], str]] = set()
        self._message_contents: dict[tuple[tuple[str, ...], str], CapturedValue] = {}
        self._message_completed: set[tuple[tuple[str, ...], str]] = set()
        self._pending_assistant_messages: dict[
            tuple[tuple[str, ...], str], tuple[str | None, str | None, bool]
        ] = {}
        self._waiting_assistant_messages: set[tuple[tuple[str, ...], str]] = set()
        self._completed_model_messages: set[tuple[tuple[str, ...], str]] = set()
        self._reasoning_contents: dict[tuple[tuple[str, ...], str], JsonValue] = {}
        self._reasoning_extractors: dict[tuple[tuple[str, ...], str], str] = {}
        self._message_model_names: dict[tuple[tuple[str, ...], str], str] = {}
        self._reasoning_completed: set[tuple[tuple[str, ...], str]] = set()
        self._active_reasoning: dict[tuple[tuple[str, ...], str], None] = {}
        self._tool_slots: dict[
            tuple[tuple[str, ...], str, int | str], tuple[str, str]
        ] = {}
        self._tool_names: dict[tuple[tuple[str, ...], str], str] = {}
        self._tool_started: set[tuple[tuple[str, ...], str]] = set()
        self._tool_argument_fragments: dict[tuple[tuple[str, ...], str], str] = {}
        self._tool_argument_snapshots: set[tuple[tuple[str, ...], str]] = set()
        self._tool_completed: set[tuple[tuple[str, ...], str]] = set()
        self._tool_results: set[tuple[tuple[str, ...], str]] = set()
        self._settled_tool_calls: set[tuple[tuple[str, ...], str]] = set()
        self._tool_call_models: dict[tuple[tuple[str, ...], str], str] = {}
        self._tool_executions: dict[str, _PendingToolExecution] = {}
        self._tool_execution_by_call: dict[tuple[tuple[str, ...], str], str] = {}
        self._visible_tool_execution_calls: set[tuple[tuple[str, ...], str]] = set()
        self._callback_entries: dict[str, str] = {}
        self._model_component_names: dict[str, str] = {}
        self._states: dict[tuple[str, ...], dict[str, JsonValue]] = {}
        self._pending_interactions: dict[
            tuple[tuple[str, ...], str], _PendingInteraction
        ] = {}
        self._subagent_descriptors: dict[tuple[str, ...], _SubagentDescriptor] = {}
        self._active_subagents: dict[tuple[str, ...], tuple[str, str | None]] = {}
        self._subagent_parent_calls: dict[tuple[str, ...], str] = {}
        self._subagent_task_descriptions: dict[tuple[str, ...], str] = {}
        self._subagent_task_message_scopes: set[tuple[str, ...]] = set()
        self._subagent_task_message_keys: set[tuple[tuple[str, ...], str]] = set()
        self._context_anchors: dict[tuple[str, ...], _ContextAnchor] = {}
        self._failure_origin_seen = False
        self._batch_writer = TraceBatchWriter(
            writer,
            policy=write_policy,
            on_committed=self._checkpoint_committed,
        )
        self._hydrate(prior_events)

    def _hydrate(self, events: tuple[TraceEvent, ...]) -> None:
        """Restore dedupe and state baselines from the current semantic Ledger."""

        for event in events:
            fact = event.fact
            self._advance_context_anchors((fact,), historical=True)
            if isinstance(fact, MessageFact) and fact.source_message_id is not None:
                key = (fact.graph_namespace, fact.source_message_id)
                self._message_seen.add(key)
                if fact.phase == "removed":
                    self._message_seen.discard(key)
                    self._message_fingerprints.pop(key, None)
                    self._delivered_message_fingerprints.pop(key, None)
                    self._message_contents.pop(key, None)
                    self._message_completed.discard(key)
                    continue
                if fact.content is not None:
                    self._track_message_content(
                        key,
                        fact.content,
                        append=fact.phase == "content",
                    )
                if fact.phase in {"completed", "reconciled", "cancelled", "abandoned"}:
                    self._message_completed.add(key)
                    self._waiting_assistant_messages.discard(key)
                elif fact.phase == "interrupted":
                    self._waiting_assistant_messages.add(key)
                    self._message_completed.discard(key)
                if fact.fingerprint is not None:
                    target = (
                        self._message_fingerprints
                        if fact.from_state_snapshot
                        else self._delivered_message_fingerprints
                    )
                    target[key] = fact.fingerprint
            elif isinstance(fact, ReasoningFact):
                key = (fact.graph_namespace, fact.source_message_id)
                previous_extractor = self._reasoning_extractors.get(key)
                if (
                    previous_extractor is not None
                    and previous_extractor != fact.extractor
                ):
                    raise TraceCorruption(
                        "Reasoning extractor changed inside one scoped message"
                    )
                self._reasoning_extractors[key] = fact.extractor
                if fact.content is not None:
                    self._track_reasoning_content(
                        key,
                        fact.content,
                        append=fact.phase == "content",
                    )
                if fact.phase == "completed":
                    self._reasoning_completed.add(key)
            elif isinstance(fact, ToolFact):
                key = (fact.graph_namespace, fact.source_tool_call_id)
                self._tool_names[key] = fact.tool_name
                if fact.phase == "started":
                    self._tool_started.add(key)
                if fact.phase == "arguments" and fact.content is not None:
                    self._tool_argument_snapshots.add(key)
                if fact.phase in {"completed", "result", "cancelled", "abandoned"}:
                    self._tool_completed.add(key)
                if fact.phase in {"result", "cancelled", "abandoned"}:
                    self._tool_results.add(key)
            elif isinstance(fact, ModelCallFact):
                if fact.model is not None:
                    for message_id in fact.output_message_ids:
                        self._message_model_names[
                            (fact.graph_namespace, message_id)
                        ] = fact.model
                if fact.phase == "completed":
                    self._completed_model_messages.update(
                        (fact.graph_namespace, message_id)
                        for message_id in fact.output_message_ids
                    )
                    for tool_call_id in fact.tool_call_ids:
                        self._tool_call_models[(fact.graph_namespace, tool_call_id)] = (
                            fact.call_id
                        )
            elif (
                isinstance(fact, ToolExecutionFact)
                and fact.source_tool_call_id is not None
            ):
                key = (fact.graph_namespace, fact.source_tool_call_id)
                if fact.phase == "started":
                    self._tool_execution_by_call[key] = fact.execution_id
                    self._settled_tool_calls.discard(key)
                elif fact.phase != "interrupted":
                    self._settled_tool_calls.add(key)
            elif isinstance(fact, StateRevisionFact):
                state = self._states.setdefault(fact.graph_namespace, {})
                for key in fact.removed_keys:
                    state.pop(key, None)
                if fact.changes.disposition == "inline" and isinstance(
                    fact.changes.value, dict
                ):
                    state.update(fact.changes.value)
            elif isinstance(fact, PlanRevisionFact):
                state = self._states.setdefault(fact.graph_namespace, {})
                if fact.plan.disposition == "inline":
                    state["tinkerfin_plan"] = fact.plan.value
            elif isinstance(fact, InteractionFact):
                key = (fact.graph_namespace, fact.source_interaction_id)
                if fact.phase == "opened":
                    self._pending_interactions[key] = _PendingInteraction(
                        kind=fact.interaction_kind,
                        tool_call_ids=fact.tool_call_ids,
                    )
                else:
                    self._pending_interactions.pop(key, None)
            elif isinstance(fact, SubagentFact):
                if fact.phase == "started" and fact.parent_tool_call_id is not None:
                    if not fact.graph_namespace:
                        raise TraceCorruption("A child call requires a graph namespace")
                    self._subagent_parent_calls[fact.graph_namespace] = (
                        fact.parent_tool_call_id
                    )
                if (
                    fact.phase == "started"
                    and fact.input is not None
                    and fact.input.disposition == "inline"
                    and isinstance(fact.input.value, dict)
                ):
                    task_input = fact.input.value
                    if set(task_input) == {""} and isinstance(task_input[""], dict):
                        task_input = task_input[""]
                    description = task_input.get("description")
                    if isinstance(description, str):
                        self._subagent_task_descriptions[fact.graph_namespace] = (
                            description
                        )
                if fact.phase in {"started", "updated"} and fact.status in {
                    "running",
                    "waiting",
                }:
                    self._active_subagents[fact.graph_namespace] = (
                        fact.subagent_id,
                        fact.agent_name,
                    )
                else:
                    self._active_subagents.pop(fact.graph_namespace, None)
                    self._subagent_task_descriptions.pop(fact.graph_namespace, None)

        # A failed attempt can still own checkpoint-backed child requests. Keep
        # only those proven relationships or scopes that are currently active.
        pending_scopes = {
            namespace[:depth]
            for namespace, _interrupt_id in self._pending_interactions
            for depth in range(1, len(namespace) + 1)
        }
        self._subagent_parent_calls = {
            namespace: call
            for namespace, call in self._subagent_parent_calls.items()
            if namespace in self._active_subagents or namespace in pending_scopes
        }

    async def observe(self, observation: RuntimeObservation) -> None:
        """Map and enqueue one already ordered Runtime observation.

        Args:
            observation: Validated lifecycle or Native semantic observation.

        Raises:
            RuntimeError: The request session is already closed.
            TraceCorruption: Observation order or correlation is inconsistent.
            TraceObserverFailed: The bounded writer or Store commit failed.
        """

        if self._closed:
            raise RuntimeError("Trace observation session is closed")
        self._observation_index += 1
        source_id = (
            f"observation:{self._writer.key.generation}:{self._run_scope}:"
            f"{self._observation_index}"
        )
        if isinstance(observation, (RunTerminalObservation, RunClosedObservation)):
            common = {
                "source_observation_id": source_id,
                "identity": observation.identity,
                "occurred_at": observation.observed_at,
                "monotonic_ns": observation.monotonic_ns,
            }
            completions = self._complete_open_reasoning(common)
            if completions:
                await self._append(tuple(completions), mandatory=False)
        if (
            isinstance(observation, (ModelCallObservation, ToolExecutionObservation))
            and observation.phase == "started"
        ):
            # A child callback may arrive before its first Native part. The verified
            # parent task and its observed Tool start already establish the same
            # Subagent boundary that the Native path will use.
            openings = tuple(self._subagent_start(observation, source_id=source_id))
            if openings:
                await self._append(openings, mandatory=False)
                self._advance_context_anchors(openings)
        facts = self._facts(observation, source_id=source_id)
        if not facts:
            return
        if isinstance(observation, RunTerminalObservation):
            settlement = tuple(facts[:-1])
            if settlement:
                await self._append(settlement, mandatory=False)
                self._advance_context_anchors(settlement)
            await self._append((facts[-1],), mandatory=True)
            self._advance_context_anchors((facts[-1],))
            return
        mandatory = isinstance(
            observation, (RunTerminalObservation, RunClosedObservation)
        )
        await self._append(tuple(facts), mandatory=mandatory)
        self._advance_context_anchors(tuple(facts))
        if isinstance(observation, RunInputObservation):
            await self._batch_writer.force()

    def _model_context_started_at(
        self,
        observation: ModelCallObservation,
    ) -> datetime:
        """Return the exact preceding boundary for one provider attempt."""

        scope = (
            observation.graph_namespace
            if self._in_subagent_scope(observation.graph_namespace)
            else ()
        )
        anchor = self._context_anchors.get(scope)
        if anchor is None:
            raise TraceCorruption("Model call has no preceding context boundary")
        if anchor.occurred_at > observation.observed_at or (
            anchor.monotonic_ns is not None
            and anchor.monotonic_ns > observation.monotonic_ns
        ):
            raise TraceCorruption("Model context boundary follows provider start")
        return anchor.occurred_at

    def _advance_context_anchors(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        historical: bool = False,
    ) -> None:
        """Advance scope-local context anchors from visible execution boundaries.

        Persisted monotonic values cannot be compared after a process restart. Ledger
        order therefore owns historical hydration, while live observations retain the
        process-local monotonic check. A resumed invocation reopens preparation in every
        active Subagent scope so external approval wait time is not reported as work.
        """

        for fact in facts:
            namespaces: tuple[tuple[str, ...], ...] = ()
            if isinstance(fact, TurnFact):
                namespaces = ((),)
            elif isinstance(fact, RunFact) and fact.phase in {"input", "resumed"}:
                namespaces = ((),)
                if fact.phase == "resumed" and not historical:
                    namespaces = (*namespaces, *sorted(self._active_subagents))
            elif isinstance(fact, MessageFact) and fact.phase in {
                "completed",
                "reconciled",
            }:
                namespaces = (fact.graph_namespace if fact.in_subagent_scope else (),)
            elif isinstance(fact, ModelCallFact) and fact.phase in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "abandoned",
            }:
                namespaces = (fact.graph_namespace if fact.in_subagent_scope else (),)
            elif isinstance(fact, ToolExecutionFact) and fact.phase in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "abandoned",
            }:
                namespaces = (fact.graph_namespace if fact.in_subagent_scope else (),)
            elif isinstance(fact, ToolFact) and fact.phase in {
                "result",
                "cancelled",
                "abandoned",
            }:
                namespaces = (fact.graph_namespace if fact.in_subagent_scope else (),)
            elif isinstance(fact, SubagentFact):
                if fact.phase == "started":
                    namespaces = (fact.graph_namespace,)
                elif fact.phase == "completed" or fact.status == "waiting":
                    namespaces = (fact.graph_namespace[:-1],)
            elif isinstance(fact, InteractionFact) and fact.phase == "resolved":
                namespaces = (fact.graph_namespace if fact.in_subagent_scope else (),)
            candidate = _ContextAnchor(
                fact.occurred_at,
                None if historical else fact.monotonic_ns,
            )
            for namespace in namespaces:
                current = self._context_anchors.get(namespace)
                if (
                    historical
                    or current is None
                    or current.monotonic_ns is None
                    or (
                        candidate.monotonic_ns is not None
                        and candidate.monotonic_ns >= current.monotonic_ns
                    )
                ):
                    self._context_anchors[namespace] = candidate

    async def _append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool,
    ) -> None:
        """Submit facts to the bounded writer; mandatory calls force prior work."""

        await self._batch_writer.submit(facts, mandatory=mandatory)

    async def _checkpoint_committed(
        self,
        events: tuple[TraceEvent, ...],
    ) -> None:
        """Advance framework-owned core cache after one Store transaction commits."""

        await load_core_projection_state(
            self._store,
            self._writer.key,
            as_of_seq=events[-1].trace_seq,
        )

    async def force(self, boundary: ObservationBoundary) -> None:
        """Commit all accepted facts at a Runtime hard boundary.

        Args:
            boundary: Runtime reason for the flush. All current boundary kinds share the
                same commit guarantee; the value remains available for observability.

        Raises:
            RuntimeError: The request session is already closed.
            TraceObserverFailed: A pending or forced Store transaction failed.
        """

        del boundary
        if self._closed:
            raise RuntimeError("Trace observation session is closed")
        await self._batch_writer.force()

    async def flush(self) -> None:
        """Commit every locally accepted fact before a same-Tracer query snapshot."""

        if not self._closed:
            await self._batch_writer.force()

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        """Return the session-owned future that completes on background failure."""

        return self._batch_writer.failure_waiter()

    async def aclose(self) -> None:
        """Settle accepted writes, close the writer once, and release Tracer ownership.

        Repeated calls are idempotent. Cancellation and the first writer failure retain
        precedence over secondary cleanup errors.

        Raises:
            BaseException: Cancellation or the primary batching/Store failure.
        """

        async with self._close_lock:
            if self._closed:
                return
            try:
                await self._batch_writer.aclose()
            finally:
                self._closed = True
                self._on_closed(self)

    def _facts(
        self,
        observation: RuntimeObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        common = {
            "source_observation_id": source_id,
            "identity": observation.identity,
            "occurred_at": observation.observed_at,
            "monotonic_ns": observation.monotonic_ns,
        }
        if isinstance(observation, RunStartedObservation):
            return [
                make_fact(
                    RunFact,
                    common,
                    phase="started",
                    input_kind=self._context.input_kind,
                    parent_run_id=self._context.parent_run_id,
                )
            ]
        if isinstance(observation, RunInputObservation):
            source = observation.source
            facts: list[TraceSemanticFact] = []
            public_input = discard_private_state(
                source.input,
                self._private_state_keys,
            )
            public_config = discard_private_state(
                source.config,
                self._private_state_keys,
            )
            user_message = extract_user_message(public_input)
            implicit_resume_ids: tuple[str, ...] = ()
            if (
                source.input_kind == "resume"
                and not source.resume
                and len(self._pending_interactions) == 1
            ):
                implicit_resume_ids = (next(iter(self._pending_interactions))[1],)
            resume_ids = (
                tuple(item.interrupt_id for item in source.resume) + implicit_resume_ids
            )
            if source.input_kind in {"ordinary", "branch", "compaction"}:
                facts.append(
                    make_fact(
                        TurnFact,
                        common,
                        turn_id=(
                            f"turn:{self._writer.key.generation}:"
                            f"{observation.identity.run_id}"
                        ),
                        user_message_id=(
                            None if user_message is None else user_message[0]
                        ),
                        parent_run_id=source.parent_run_id,
                    )
                )
            facts.append(
                make_fact(
                    RunFact,
                    common,
                    phase=(
                        "resumed"
                        if source.input_kind in {"continuation", "resume", "abandon"}
                        else "input"
                    ),
                    input_kind=source.input_kind,
                    parent_run_id=source.parent_run_id,
                    input=(
                        self._capture_structure(
                            public_input,
                            content_kind="custom",
                            component_name="run_input",
                            divisor=2,
                        )
                        if source.input_kind in {"ordinary", "branch", "compaction"}
                        else self._capture(
                            public_input
                            if source.input_kind == "continuation"
                            else [
                                item.model_dump(mode="json", by_alias=True)
                                for item in source.resume
                            ],
                            content_kind=(
                                "custom"
                                if source.input_kind == "continuation"
                                else "interaction"
                            ),
                            component_name=(
                                "run_input"
                                if source.input_kind == "continuation"
                                else "resume"
                            ),
                            divisor=2,
                        )
                    ),
                    config=self._capture_structure(
                        public_config,
                        content_kind="custom",
                        component_name="run_config",
                        divisor=2,
                    ),
                    interrupt_ids=resume_ids,
                )
            )
            if source.call_tracking_enabled:
                facts.append(make_fact(CallTrackingFact, common))
            if user_message is not None and source.input_kind in {"ordinary", "branch"}:
                user_message_id, user_content = user_message
                scoped_message_id = _scope_id("message", (), user_message_id)
                user_key = ((), user_message_id)
                self._message_seen.add(user_key)
                captured_user_content = self._capture(
                    user_content,
                    content_kind="message",
                )
                self._track_message_content(
                    user_key,
                    captured_user_content,
                    append=False,
                )
                self._message_completed.add(user_key)
                facts.append(
                    MessageFact(
                        source_observation_id=source_id,
                        identity=observation.identity,
                        occurred_at=observation.observed_at,
                        monotonic_ns=observation.monotonic_ns,
                        phase="reconciled",
                        message_id=scoped_message_id,
                        source_message_id=user_message_id,
                        role="user",
                        content=captured_user_content,
                    )
                )
            if source.input_kind == "abandon":
                facts.extend(self._resolved_interaction_facts(source.resume, common))
            # An unbound request is not proof of native resume either. Native
            # snapshots close an implicit interrupt after it actually disappears.
            return facts
        if isinstance(observation, ModelCallObservation):
            if observation.failure_origin:
                self._failure_origin_seen = True
            call_id = _scope_id(
                "model-call",
                observation.graph_namespace,
                observation.call_id,
            )
            component_name = observation.model or self._model_component_names.get(
                observation.call_id
            )
            if observation.phase == "started":
                self._callback_entries[observation.call_id] = call_id
                if observation.model is not None:
                    self._model_component_names[observation.call_id] = observation.model
            elif observation.phase in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "abandoned",
            }:
                self._callback_entries.pop(observation.call_id, None)
                self._model_component_names.pop(observation.call_id, None)
            if observation.phase == "completed":
                for tool_call_id in observation.tool_call_ids:
                    self._tool_call_models[
                        (observation.graph_namespace, tool_call_id)
                    ] = call_id
            request = (
                None
                if observation.phase != "started"
                else self._capture(
                    {
                        "messages": [
                            message.model_dump(mode="json", by_alias=True)
                            for message in observation.messages
                        ],
                        "invocation": observation.invocation,
                        "options": observation.options,
                    },
                    content_kind="model_request",
                    component_name=component_name,
                )
            )
            if component_name is not None:
                for message_id in observation.output_message_ids:
                    self._message_model_names[
                        (observation.graph_namespace, message_id)
                    ] = component_name
            for message_id in observation.output_message_ids:
                message_key = (observation.graph_namespace, message_id)
                self._inherited_message_fingerprints.pop(message_key, None)
                if observation.phase == "completed":
                    self._completed_model_messages.add(message_key)
                if message_key not in self._message_completed:
                    self._pending_assistant_messages.setdefault(
                        message_key,
                        (
                            message_id,
                            None,
                            self._in_subagent_scope(observation.graph_namespace),
                        ),
                    )
            return [
                make_fact(
                    ModelCallFact,
                    common,
                    graph_namespace=observation.graph_namespace,
                    in_subagent_scope=self._in_subagent_scope(
                        observation.graph_namespace
                    ),
                    phase=observation.phase,
                    call_id=call_id,
                    contribution_id=(
                        None
                        if observation.contribution_id is None
                        else _scope_id(
                            "context",
                            observation.graph_namespace,
                            observation.contribution_id,
                        )
                    ),
                    parent_call_id=(
                        None
                        if observation.parent_call_id is None
                        else self._callback_entries.get(observation.parent_call_id)
                    ),
                    agent_name=observation.agent_name,
                    provider=observation.provider,
                    model=observation.model,
                    context_started_at=(
                        self._model_context_started_at(observation)
                        if observation.phase == "started"
                        else None
                    ),
                    request=request,
                    system_message_positions=(
                        tuple(
                            index
                            for index, message in enumerate(observation.messages)
                            if message.message_type == "system"
                        )
                        if observation.phase == "started"
                        else ()
                    ),
                    output_message_ids=observation.output_message_ids,
                    usage=(
                        None
                        if observation.usage is None
                        else self._capture(
                            observation.usage,
                            content_kind="model_response",
                            component_name=component_name,
                        )
                    ),
                    response_metadata=(
                        None
                        if observation.response_metadata is None
                        else self._capture(
                            observation.response_metadata,
                            content_kind="model_response",
                            component_name=component_name,
                        )
                    ),
                    tool_call_ids=observation.tool_call_ids,
                    error_type=observation.error_type,
                    error_message=(
                        self._capture(
                            observation.error_message,
                            content_kind="model_response",
                            component_name=component_name,
                        )
                        if self._policy.include_error_messages
                        and observation.error_message is not None
                        else None
                    ),
                    failure_origin=observation.failure_origin,
                )
            ]
        if isinstance(observation, ToolExecutionObservation):
            execution_id = _scope_id(
                "tool-execution",
                observation.graph_namespace,
                observation.execution_id,
            )
            if observation.phase == "started":
                if execution_id in self._tool_executions:
                    raise TraceCorruption("Tool execution started more than once")
                if observation.input is None:  # pragma: no cover - contract validation
                    raise TraceCorruption("Tool execution start has no input")
                if observation.tool_call_id is not None:
                    self._settled_tool_calls.discard(
                        (observation.graph_namespace, observation.tool_call_id)
                    )
                    self._tool_execution_by_call[
                        (observation.graph_namespace, observation.tool_call_id)
                    ] = execution_id
                traced = self._policy.traces_tool(observation.tool_name)
                if traced and observation.tool_call_id is not None:
                    self._visible_tool_execution_calls.add(
                        (observation.graph_namespace, observation.tool_call_id)
                    )
                parent_call_id = (
                    None
                    if observation.parent_call_id is None
                    else self._callback_entries.get(observation.parent_call_id)
                )
                self._tool_executions[execution_id] = _PendingToolExecution(
                    input=observation.input if traced else None,
                    parent_call_id=parent_call_id,
                    namespace=observation.graph_namespace,
                    agent_name=observation.agent_name,
                    tool_name=observation.tool_name,
                    traced=traced,
                    observed_at=observation.observed_at,
                    monotonic_ns=observation.monotonic_ns,
                )
                if not traced:
                    return []
                self._callback_entries[observation.execution_id] = execution_id
                return [
                    make_fact(
                        ToolExecutionFact,
                        common,
                        graph_namespace=observation.graph_namespace,
                        in_subagent_scope=self._in_subagent_scope(
                            observation.graph_namespace
                        ),
                        phase="started",
                        execution_id=execution_id,
                        parent_call_id=parent_call_id,
                        agent_name=observation.agent_name,
                        source_tool_call_id=observation.tool_call_id,
                        tool_name=observation.tool_name,
                        input=self._capture_tool(
                            tool_name=observation.tool_name,
                            value=observation.input,
                            target="arguments",
                        ),
                    )
                ]
            pending = self._tool_executions.pop(execution_id, None)
            self._callback_entries.pop(observation.execution_id, None)
            if pending is None:
                raise TraceCorruption("Tool execution terminal has no matching start")
            if (
                pending.namespace != observation.graph_namespace
                or pending.agent_name != observation.agent_name
                or pending.tool_name != observation.tool_name
            ):
                raise TraceCorruption("Tool execution identity changed before terminal")
            if not pending.traced:
                return []
            if (
                observation.tool_call_id is not None
                and observation.phase != "interrupted"
            ):
                self._settled_tool_calls.add(
                    (observation.graph_namespace, observation.tool_call_id)
                )
            if observation.failure_origin:
                self._failure_origin_seen = True
            output = (
                None
                if observation.output is None
                else self._capture_tool(
                    tool_name=observation.tool_name,
                    value=observation.output,
                    target="result",
                )
            )
            return [
                make_fact(
                    ToolExecutionFact,
                    common,
                    graph_namespace=observation.graph_namespace,
                    in_subagent_scope=self._in_subagent_scope(
                        observation.graph_namespace
                    ),
                    phase=observation.phase,
                    execution_id=execution_id,
                    parent_call_id=pending.parent_call_id,
                    agent_name=observation.agent_name,
                    source_tool_call_id=observation.tool_call_id,
                    tool_name=observation.tool_name,
                    output=output,
                    error_type=observation.error_type,
                    error_message=(
                        self._capture(
                            observation.error_message,
                            content_kind="tool_result",
                            component_name=observation.tool_name,
                        )
                        if self._policy.include_error_messages
                        and observation.error_message is not None
                        else None
                    ),
                    failure_origin=observation.failure_origin,
                )
            ]
        if isinstance(observation, ContextContributionObservation):
            if observation.failure_origin:
                self._failure_origin_seen = True
            parent_call_id = (
                None
                if observation.parent_call_id is None
                else self._callback_entries.get(observation.parent_call_id)
            )
            return [
                make_fact(
                    ContextContributionFact,
                    common,
                    graph_namespace=observation.graph_namespace,
                    in_subagent_scope=self._in_subagent_scope(
                        observation.graph_namespace
                    ),
                    phase=observation.phase,
                    contribution_id=_scope_id(
                        "context",
                        observation.graph_namespace,
                        observation.contribution_id,
                    ),
                    parent_call_id=parent_call_id,
                    parent_tool_call_id=observation.parent_tool_call_id,
                    compaction_origin=observation.compaction_origin,
                    model_call_ids=tuple(
                        _scope_id("model-call", observation.graph_namespace, call_id)
                        for call_id in observation.model_call_ids
                    ),
                    context_kind=observation.context_kind,
                    name=observation.name,
                    input=(
                        None
                        if observation.input is None
                        else self._capture(
                            observation.input,
                            content_kind="custom",
                            component_name=observation.name,
                        )
                    ),
                    output=(
                        None
                        if observation.output is None
                        else self._capture(
                            observation.output,
                            content_kind="custom",
                            component_name=observation.name,
                        )
                    ),
                    error_type=observation.error_type,
                    failure_origin=observation.failure_origin,
                )
            ]
        if isinstance(observation, RunResumeCheckpointedObservation):
            # A resume request can fail validation before the Runtime accepts it.
            # Only saver-readable acceptance may close its pending cards; see
            # test_blank_discussion_is_rejected_before_accepting_the_card.
            accepted = tuple(
                summary
                for summary in self._context.resume
                if summary.interrupt_id in observation.native_interrupt_ids
            )
            return [
                make_fact(
                    RunFact,
                    common,
                    phase="resume_checkpointed",
                    interrupt_ids=observation.native_interrupt_ids,
                ),
                *self._resolved_interaction_facts(accepted, common),
            ]
        if isinstance(observation, RunObserverFailedObservation):
            return [
                make_fact(
                    RunFact,
                    common,
                    phase="observer_failed",
                    observer_name=observation.observer_name,
                    error_type=observation.error_type,
                )
            ]
        if isinstance(observation, RunTerminalObservation):
            facts = self._terminal_settlement_facts(
                observation,
                common=common,
                source_id=source_id,
            )
            self._active_subagents.clear()
            self._callback_entries.clear()
            self._model_component_names.clear()
            self._tool_executions.clear()
            facts.append(
                make_fact(
                    RunFact,
                    common,
                    phase="terminal",
                    outcome=observation.outcome,
                    code=observation.code,
                    error_type=observation.error_type,
                    failure_origin=(
                        observation.outcome == "failed"
                        and observation.error_type is not None
                        and not self._failure_origin_seen
                    ),
                )
            )
            return facts
        if isinstance(observation, RunClosedObservation):
            return [
                make_fact(
                    RunFact,
                    common,
                    phase="closed",
                    outcome=observation.outcome,
                ),
            ]
        if isinstance(observation, NativeTaskObservation):
            self._remember_subagent_descriptors(observation)
        namespace = observation.graph_namespace
        facts = self._subagent_start(observation, source_id=source_id)
        if isinstance(observation, NativeMessageObservation):
            facts.extend(self._message_facts(observation, source_id=source_id))
        elif isinstance(observation, NativeReasoningObservation):
            facts.extend(self._reasoning_facts(observation, source_id=source_id))
        elif isinstance(observation, NativeTaskObservation):
            facts.extend(self._subagent_completions(observation, source_id=source_id))
        elif isinstance(observation, NativeStateObservation):
            facts.extend(self._state_facts(observation, source_id=source_id))
        elif isinstance(observation, NativeExtraObservation):
            facts.append(
                make_fact(
                    NativeExtraFact,
                    common,
                    graph_namespace=namespace,
                    in_subagent_scope=self._in_subagent_scope(namespace),
                    mode=observation.mode,
                    data_type=observation.data_type,
                    top_level_keys=tuple(
                        key
                        for key in observation.top_level_keys
                        if key not in self._private_state_keys
                    ),
                )
            )
        return facts

    def _terminal_settlement_facts(
        self,
        observation: RunTerminalObservation,
        *,
        common: Mapping[str, object],
        source_id: str,
    ) -> list[TraceSemanticFact]:
        """Settle Native work that emitted no result before the Run terminal.

        Actual execution terminals and Native task results remain authoritative.
        An execution callback does not suppress a later ToolMessage result. Missing results cannot
        inherit a Run failure as their own error: interrupted work waits, explicit Run
        cancellation cancels it, and every other unmatched node is abandoned. Tool
        proposals remain waiting across an interrupt so a resumed checkpoint can still
        resolve the same proposal.
        """

        terminal_phase: Literal["interrupted", "cancelled", "abandoned"]
        if observation.outcome == "interrupted":
            terminal_phase = "interrupted"
        elif observation.outcome == "cancelled":
            terminal_phase = "cancelled"
        else:
            terminal_phase = "abandoned"
        facts: list[TraceSemanticFact] = []
        # Native error callbacks do not promise a final Assistant snapshot (LangGraph
        # StreamMessagesHandler.on_llm_error). Wait for Runtime's observation drain so
        # every accepted Native fragment precedes this one bounded delivery snapshot.
        for key, (source_message_id, name, in_subagent_scope) in sorted(
            self._pending_assistant_messages.items()
        ):
            namespace, message_source_id = key
            if key in self._message_completed:
                continue
            content = self._message_contents.get(key)
            model_completed = key in self._completed_model_messages
            if model_completed:
                # A completed provider output proves success, but it contains no
                # complete response body in the observation contract. Preserve that
                # success and explicitly omit incomplete capture instead of presenting
                # a received prefix as the complete successful message.
                content = CapturedValue(
                    disposition="omitted",
                    safe_size_bytes=0 if content is None else content.safe_size_bytes,
                    reason="incomplete_message",
                )
            elif content is not None and content.disposition == "inline":
                # Redaction must also see the assembled value: a sensitive token
                # or business pattern can span individually safe Native fragments.
                content = self._capture(
                    content.value,
                    content_kind="message",
                    component_name=self._message_model_names.get(key),
                )
            facts.append(
                make_fact(
                    MessageFact,
                    common,
                    graph_namespace=namespace,
                    in_subagent_scope=in_subagent_scope,
                    phase="completed" if model_completed else terminal_phase,
                    message_id=_scope_id("message", namespace, message_source_id),
                    source_message_id=source_message_id,
                    role="assistant",
                    content=content,
                    name=name,
                )
            )
            if model_completed or terminal_phase != "interrupted":
                self._message_completed.add(key)
        self._pending_assistant_messages.clear()
        for namespace, (subagent_id, agent_name) in sorted(
            self._active_subagents.items()
        ):
            waiting = terminal_phase == "interrupted"
            subagent_status: Literal["waiting", "cancelled", "abandoned"]
            if waiting:
                subagent_status = "waiting"
            elif terminal_phase == "cancelled":
                subagent_status = "cancelled"
            else:
                subagent_status = "abandoned"
            facts.append(
                SubagentFact(
                    source_observation_id=source_id,
                    identity=observation.identity,
                    graph_namespace=namespace,
                    occurred_at=observation.observed_at,
                    monotonic_ns=observation.monotonic_ns,
                    phase="updated" if waiting else "completed",
                    subagent_id=subagent_id,
                    agent_name=agent_name,
                    status=subagent_status,
                )
            )
            self._subagent_descriptors.pop(namespace, None)
        if terminal_phase != "interrupted":
            unresolved_tools = sorted(
                self._tool_started - self._tool_results - self._settled_tool_calls
            )
            for namespace, tool_call_id in unresolved_tools:
                tool_name = self._tool_names.get((namespace, tool_call_id))
                if tool_name is None or not self._policy.traces_tool(tool_name):
                    continue
                facts.append(
                    make_fact(
                        ToolFact,
                        common,
                        graph_namespace=namespace,
                        in_subagent_scope=self._in_subagent_scope(namespace),
                        phase=(
                            "cancelled"
                            if terminal_phase == "cancelled"
                            else "abandoned"
                        ),
                        tool_call_id=_scope_id("tool", namespace, tool_call_id),
                        source_tool_call_id=tool_call_id,
                        parent_call_id=self._tool_call_models.get(
                            (namespace, tool_call_id)
                        ),
                        tool_name=tool_name,
                    )
                )
                self._tool_results.add((namespace, tool_call_id))
                self._tool_completed.add((namespace, tool_call_id))
        return facts

    def _reasoning_facts(
        self,
        observation: NativeReasoningObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        """Reconcile one explicitly extracted reasoning delta or snapshot."""

        namespace = observation.graph_namespace
        key = (namespace, observation.message_id)
        common = {
            "source_observation_id": source_id,
            "identity": observation.identity,
            "graph_namespace": namespace,
            "in_subagent_scope": self._in_subagent_scope(namespace),
            "occurred_at": observation.observed_at,
            "monotonic_ns": observation.monotonic_ns,
        }
        previous_extractor = self._reasoning_extractors.get(key)
        if (
            previous_extractor is not None
            and previous_extractor != observation.extractor
        ):
            raise TraceCorruption(
                "Reasoning extractor changed inside one scoped message"
            )
        self._reasoning_extractors[key] = observation.extractor
        facts: list[TraceSemanticFact] = []
        if not observation.snapshot:
            for active_key in tuple(self._active_reasoning):
                if active_key[0] == namespace and active_key != key:
                    facts.append(
                        self._complete_reasoning(
                            active_key,
                            common=common,
                        )
                    )
            captured = self._capture_pipeline.capture_reasoning(
                observation.content,
                component_name=self._message_model_names.get(key),
                max_bytes=self._payload_budget,
            )
            if captured.disposition == "omitted" and key in self._active_reasoning:
                return facts
            if key in self._reasoning_completed and captured.disposition == "omitted":
                return facts
            self._reasoning_completed.discard(key)
            self._active_reasoning[key] = None
            self._track_reasoning_content(key, captured, append=True)
            facts.append(
                self._reasoning_content_fact(
                    observation,
                    common=common,
                    phase="content",
                    content=captured,
                )
            )
            return facts

        captured = self._capture_pipeline.capture_reasoning(
            observation.content,
            component_name=self._message_model_names.get(key),
            max_bytes=self._payload_budget,
        )
        content_matches = (
            captured.disposition == "inline"
            and key in self._reasoning_contents
            and self._reasoning_contents[key] == captured.value
        )
        already_completed = key in self._reasoning_completed
        if already_completed and (content_matches or captured.disposition == "omitted"):
            self._active_reasoning.pop(key, None)
            return facts
        omission_already_recorded = (
            captured.disposition == "omitted" and key in self._active_reasoning
        )
        if not content_matches and not omission_already_recorded:
            self._track_reasoning_content(key, captured, append=False)
            facts.append(
                self._reasoning_content_fact(
                    observation,
                    common=common,
                    phase="reconciled",
                    content=captured,
                )
            )
        self._active_reasoning.pop(key, None)
        self._reasoning_completed.add(key)
        facts.append(self._complete_reasoning(key, common=common))
        return facts

    def _reasoning_content_fact(
        self,
        observation: NativeReasoningObservation,
        *,
        common: Mapping[str, object],
        phase: str,
        content: CapturedValue,
    ) -> TraceSemanticFact:
        return make_fact(
            ReasoningFact,
            common,
            phase=phase,
            reasoning_id=_scope_id(
                "reasoning",
                observation.graph_namespace,
                observation.message_id,
            ),
            message_id=_scope_id(
                "message",
                observation.graph_namespace,
                observation.message_id,
            ),
            source_message_id=observation.message_id,
            extractor=observation.extractor,
            content=content,
        )

    def _complete_reasoning(
        self,
        key: tuple[tuple[str, ...], str],
        *,
        common: Mapping[str, object],
    ) -> TraceSemanticFact:
        namespace, source_message_id = key
        extractor = self._reasoning_extractors.get(key)
        if extractor is None:
            raise TraceCorruption("Reasoning completion has no registered extractor")
        self._active_reasoning.pop(key, None)
        self._reasoning_completed.add(key)
        return make_fact(
            ReasoningFact,
            common,
            graph_namespace=namespace,
            in_subagent_scope=self._in_subagent_scope(namespace),
            phase="completed",
            reasoning_id=_scope_id("reasoning", namespace, source_message_id),
            message_id=_scope_id("message", namespace, source_message_id),
            source_message_id=source_message_id,
            extractor=extractor,
        )

    def _complete_open_reasoning(
        self,
        common: Mapping[str, object],
    ) -> list[TraceSemanticFact]:
        return [
            self._complete_reasoning(key, common=common)
            for key in tuple(self._active_reasoning)
        ]

    def _message_facts(
        self,
        observation: NativeMessageObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        message = observation.message
        if message.message_type == "tool" and message.tool_call_id is not None:
            tool_key = (observation.graph_namespace, message.tool_call_id)
            tool_name = self._tool_names.get(tool_key) or message.name or "unknown"
            if not self._policy.traces_tool(tool_name):
                return self._tool_result(
                    message,
                    namespace=observation.graph_namespace,
                    common={
                        "source_observation_id": source_id,
                        "identity": observation.identity,
                        "graph_namespace": observation.graph_namespace,
                        "in_subagent_scope": self._in_subagent_scope(
                            observation.graph_namespace
                        ),
                        "occurred_at": observation.observed_at,
                        "monotonic_ns": observation.monotonic_ns,
                    },
                )
        message_source_id = message.id or (
            f"anonymous-{self._message_fingerprint(message, observation.graph_namespace)}"
        )
        message_id = _scope_id(
            "message", observation.graph_namespace, message_source_id
        )
        key = (observation.graph_namespace, message_source_id)
        inherited = self._inherited_message_fingerprints.pop(key, None)
        self._register_inheritable_message(message, key=key)
        common = {
            "source_observation_id": source_id,
            "identity": observation.identity,
            "graph_namespace": observation.graph_namespace,
            "in_subagent_scope": self._in_subagent_scope(observation.graph_namespace),
            "occurred_at": observation.observed_at,
            "monotonic_ns": observation.monotonic_ns,
        }
        facts: list[TraceSemanticFact] = []
        if message.message_type == "remove":
            if message.id is None:
                raise TraceCorruption("RemoveMessage requires a stable target ID")
            suppressed = (
                key in self._subagent_task_message_keys or inherited is not None
            )
            self._message_seen.discard(key)
            self._message_fingerprints.pop(key, None)
            self._delivered_message_fingerprints.pop(key, None)
            self._message_contents.pop(key, None)
            self._message_completed.discard(key)
            self._pending_assistant_messages.pop(key, None)
            self._waiting_assistant_messages.discard(key)
            self._subagent_task_message_keys.discard(key)
            if suppressed:
                return []
            return [
                make_fact(
                    MessageFact,
                    common,
                    phase="removed",
                    message_id=message_id,
                    source_message_id=message.id,
                    role="other",
                )
            ]
        if message.message_type == "human":
            fingerprint = self._message_fingerprint(
                message, observation.graph_namespace
            )
            if (
                key in self._subagent_task_message_keys
                and self._message_fingerprints.get(key) == fingerprint
            ):
                return []
            if self._register_subagent_task_message(
                message,
                namespace=observation.graph_namespace,
                key=key,
                fingerprint=fingerprint,
            ):
                return []
            self._subagent_task_message_keys.discard(key)
            captured = self._message_content(message)
            unchanged = (
                key in self._message_contents
                and self._message_contents[key] == captured
            )
            self._message_seen.add(key)
            self._message_completed.add(key)
            self._delivered_message_fingerprints[key] = fingerprint
            self._track_message_content(key, captured, append=False)
            if unchanged:
                return []
            return [
                make_fact(
                    MessageFact,
                    common,
                    phase="reconciled",
                    message_id=message_id,
                    source_message_id=message.id,
                    role="user",
                    content=captured,
                    fingerprint=fingerprint,
                    name=message.name,
                )
            ]
        role = message_role(message)
        if key not in self._message_seen or key in self._waiting_assistant_messages:
            self._message_seen.add(key)
            self._waiting_assistant_messages.discard(key)
            facts.append(
                make_fact(
                    MessageFact,
                    common,
                    phase="started",
                    message_id=message_id,
                    source_message_id=message.id,
                    role=role,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
            )
        if (
            role == "assistant"
            and message.message_type == "assistant_chunk"
            and key not in self._message_completed
        ):
            self._pending_assistant_messages.setdefault(
                key,
                (
                    message.id,
                    message.name,
                    self._in_subagent_scope(observation.graph_namespace),
                ),
            )
        if role == "assistant" and message.content not in ("", []):
            captured_content = self._message_content(message)
            self._track_message_content(
                key,
                captured_content,
                append=message.message_type == "assistant_chunk",
            )
        if role == "assistant" and message.message_type != "assistant_chunk":
            captured_content = self._message_content(message)
            fingerprint = self._message_fingerprint(
                message, observation.graph_namespace
            )
            self._delivered_message_fingerprints[key] = fingerprint
            self._message_completed.add(key)
            self._pending_assistant_messages.pop(key, None)
            facts.append(
                make_fact(
                    MessageFact,
                    common,
                    phase="reconciled",
                    message_id=message_id,
                    source_message_id=message.id,
                    role=role,
                    content=captured_content,
                    fingerprint=fingerprint,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
            )
        facts.extend(
            self._tool_facts(
                message,
                namespace=observation.graph_namespace,
                parent_message_id=message_id,
                common=common,
            )
        )
        if message.message_type == "tool" and message.tool_call_id is not None:
            facts.extend(
                self._tool_result(
                    message,
                    namespace=observation.graph_namespace,
                    common=common,
                )
            )
        if message.message_type != "assistant_chunk" and role != "assistant":
            self._message_completed.add(key)
            facts.append(
                make_fact(
                    MessageFact,
                    common,
                    phase="completed",
                    message_id=message_id,
                    source_message_id=message.id,
                    role=role,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
            )
        return facts

    def _tool_facts(
        self,
        message: NativeMessageRecord,
        *,
        namespace: tuple[str, ...],
        parent_message_id: str,
        common: Mapping[str, object],
    ) -> list[TraceSemanticFact]:
        facts: list[TraceSemanticFact] = []
        for chunk in message.tool_call_chunks:
            # Unindexed calls carry their own identity (NativeToolCallChunk's
            # contract); sharing a None slot would mix independent Tool arguments.
            slot = (
                namespace,
                parent_message_id,
                chunk.index if chunk.index is not None else str(chunk.id),
            )
            if chunk.id is not None and chunk.name is not None:
                self._tool_slots[slot] = (chunk.id, chunk.name)
                key = (namespace, chunk.id)
                self._tool_names[key] = chunk.name
                if key not in self._tool_started:
                    self._tool_started.add(key)
                    if self._policy.traces_tool(chunk.name):
                        facts.append(
                            make_fact(
                                ToolFact,
                                common,
                                phase="started",
                                tool_call_id=_scope_id("tool", namespace, chunk.id),
                                source_tool_call_id=chunk.id,
                                parent_call_id=self._tool_call_models.get(key),
                                tool_name=chunk.name,
                            )
                        )
            binding = self._tool_slots.get(slot)
            if binding is not None and chunk.arguments:
                tool_id, tool_name = binding
                tool_key = (namespace, tool_id)
                arguments = (
                    self._tool_argument_fragments.get(tool_key, "") + chunk.arguments
                )
                self._tool_argument_fragments[tool_key] = arguments
                parsed_arguments = parsed_json(arguments)
                content = (
                    None
                    if parsed_arguments is None
                    else self._capture_tool(
                        tool_name=tool_name,
                        value=parsed_arguments,
                        target="arguments",
                    )
                )
                if content is not None:
                    self._tool_argument_snapshots.add(tool_key)
                if self._policy.traces_tool(tool_name) and content is not None:
                    facts.append(
                        make_fact(
                            ToolFact,
                            common,
                            phase="arguments",
                            tool_call_id=_scope_id("tool", namespace, tool_id),
                            source_tool_call_id=tool_id,
                            parent_call_id=self._tool_call_models.get(tool_key),
                            tool_name=tool_name,
                            content=content,
                        )
                    )
        for call in message.tool_calls:
            key = (namespace, call.id)
            self._tool_names[key] = call.name
            if key not in self._tool_completed:
                if key not in self._tool_started:
                    self._tool_started.add(key)
                    if self._policy.traces_tool(call.name):
                        facts.append(
                            make_fact(
                                ToolFact,
                                common,
                                phase="started",
                                tool_call_id=_scope_id("tool", namespace, call.id),
                                source_tool_call_id=call.id,
                                parent_call_id=self._tool_call_models.get(key),
                                tool_name=call.name,
                            ),
                        )
                streamed_arguments = self._tool_argument_fragments.get(key)
                arguments_match = False
                if streamed_arguments is not None:
                    try:
                        arguments_match = (
                            json.loads(streamed_arguments) == call.arguments
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        arguments_match = False
                if self._policy.traces_tool(call.name) and (
                    not arguments_match or key not in self._tool_argument_snapshots
                ):
                    facts.append(
                        make_fact(
                            ToolFact,
                            common,
                            phase="arguments",
                            tool_call_id=_scope_id("tool", namespace, call.id),
                            source_tool_call_id=call.id,
                            parent_call_id=self._tool_call_models.get(key),
                            tool_name=call.name,
                            content=self._capture_tool(
                                tool_name=call.name,
                                value=call.arguments,
                                target="arguments",
                            ),
                        )
                    )
                if self._policy.traces_tool(call.name):
                    facts.append(
                        make_fact(
                            ToolFact,
                            common,
                            phase="completed",
                            tool_call_id=_scope_id("tool", namespace, call.id),
                            source_tool_call_id=call.id,
                            parent_call_id=self._tool_call_models.get(key),
                            tool_name=call.name,
                        )
                    )
                self._tool_completed.add(key)
                self._tool_argument_fragments.pop(key, None)
        return facts

    def _tool_result(
        self,
        message: NativeMessageRecord,
        *,
        namespace: tuple[str, ...],
        common: Mapping[str, object],
    ) -> list[TraceSemanticFact]:
        assert message.tool_call_id is not None
        key = (namespace, message.tool_call_id)
        # A real parent result settles its call even if an earlier failed Run
        # provisionally marked the attempt abandoned. Its exact ID and scope,
        # not result text or the new Run's outcome, identify owned requests.
        facts = self._complete_subagent_call(key, common=common)
        if key in self._tool_results:
            return facts
        self._tool_results.add(key)
        self._tool_argument_fragments.pop(key, None)
        tool_name = self._tool_names.get(key) or message.name or "unknown"
        if not self._policy.traces_tool(tool_name):
            self._tool_completed.add(key)
            return facts
        if key not in self._tool_completed:
            facts.append(
                make_fact(
                    ToolFact,
                    common,
                    phase="completed",
                    tool_call_id=_scope_id("tool", namespace, message.tool_call_id),
                    source_tool_call_id=message.tool_call_id,
                    parent_call_id=self._tool_call_models.get(key),
                    tool_name=tool_name,
                )
            )
            self._tool_completed.add(key)
        result_status = message.tool_status or "success"
        failure_origin = (
            result_status == "error" and key not in self._visible_tool_execution_calls
        )
        if failure_origin:
            self._failure_origin_seen = True
        facts.append(
            make_fact(
                ToolFact,
                common,
                phase="result",
                tool_call_id=_scope_id("tool", namespace, message.tool_call_id),
                source_tool_call_id=message.tool_call_id,
                parent_call_id=self._tool_call_models.get(key),
                tool_name=tool_name,
                content=self._capture_tool(
                    tool_name=tool_name,
                    value=message.content,
                    target="result",
                ),
                result_status=result_status,
                failure_origin=failure_origin,
            )
        )
        return facts

    def _state_facts(
        self,
        observation: NativeStateObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        namespace = observation.graph_namespace
        common = {
            "source_observation_id": source_id,
            "identity": observation.identity,
            "graph_namespace": namespace,
            "in_subagent_scope": self._in_subagent_scope(namespace),
            "occurred_at": observation.observed_at,
            "monotonic_ns": observation.monotonic_ns,
        }
        facts: list[TraceSemanticFact] = []
        source_state = cast(
            dict[str, JsonValue],
            discard_private_state(
                observation.state,
                self._private_state_keys,
            ),
        )
        source_plan = source_state.get("tinkerfin_plan")
        public_state = cast(
            dict[str, JsonValue],
            self._capture_pipeline.redact(
                {
                    key: item
                    for key, item in source_state.items()
                    if key != "tinkerfin_plan"
                },
                context=RedactionContext(content_kind="state"),
            ),
        )
        if "tinkerfin_plan" in public_state:
            raise TraceCaptureRejected(
                "State redaction introduced the reserved Plan field"
            )
        if "tinkerfin_plan" in source_state:
            public_state["tinkerfin_plan"] = self._capture_pipeline.redact(
                source_plan,
                context=RedactionContext(content_kind="plan"),
            )
        previous = self._states.get(namespace, {})
        changes = {
            key: value
            for key, value in public_state.items()
            if key not in previous or previous[key] != value
        }
        removed = tuple(sorted(set(previous) - set(public_state)))
        plan_changed = "tinkerfin_plan" in changes
        state_changes = {
            key: value for key, value in changes.items() if key != "tinkerfin_plan"
        }
        if state_changes or removed:
            facts.append(
                make_fact(
                    StateRevisionFact,
                    common,
                    revision_id=_scope_id(
                        "state",
                        namespace,
                        f"{self._observation_index}",
                    ),
                    changes=self._capture_pipeline.bound(
                        state_changes,
                        max_bytes=self._payload_budget,
                    ),
                    removed_keys=removed,
                )
            )
        if plan_changed:
            plan_value = changes["tinkerfin_plan"]
            plan_revision, plan_status = plan_metadata(plan_value)
            facts.append(
                make_fact(
                    PlanRevisionFact,
                    common,
                    revision_id=_scope_id(
                        "plan",
                        namespace,
                        f"{self._observation_index}",
                    ),
                    revision=plan_revision,
                    status=plan_status,
                    plan=self._capture_pipeline.bound(
                        plan_value,
                        max_bytes=self._payload_budget,
                    ),
                )
            )
        self._states[namespace] = dict(public_state)
        first_snapshot = namespace not in self._initial_state_scopes
        self._initial_state_scopes.add(namespace)
        current_message_keys: set[tuple[tuple[str, ...], str]] = set()
        first_human_index = next(
            (
                index
                for index, message in enumerate(observation.messages)
                if message.message_type == "human"
            ),
            None,
        )
        for index, message in enumerate(observation.messages):
            message_source_id = message.id or (
                f"anonymous-{self._message_fingerprint(message, namespace)}"
            )
            key = (namespace, message_source_id)
            current_message_keys.add(key)
            fingerprint = self._message_fingerprint(message, namespace)
            self._register_inheritable_message(message, key=key)
            if self._is_inherited_message(
                message,
                key=key,
                fingerprint=fingerprint,
                first_snapshot=first_snapshot,
            ):
                continue
            if index == first_human_index and self._register_subagent_task_message(
                message,
                namespace=namespace,
                key=key,
                fingerprint=fingerprint,
            ):
                continue
            if self._message_fingerprints.get(key) == fingerprint:
                continue
            self._subagent_task_message_keys.discard(key)
            delivered_matches = (
                self._delivered_message_fingerprints.get(key) == fingerprint
            )
            self._message_fingerprints[key] = fingerprint
            message_id = _scope_id("message", namespace, message_source_id)
            self._message_seen.add(key)
            captured_content = (
                None
                if message.message_type == "tool"
                else self._message_content(message)
            )
            content_matches = (
                captured_content is not None
                and captured_content.disposition == "inline"
                and key in self._message_contents
                and self._message_contents[key] == captured_content
            )
            role = message_role(message)
            if role == "user" and content_matches:
                self._message_completed.add(key)
                continue
            phase = (
                "reconciled"
                if role == "assistant" and not delivered_matches
                else ("completed" if content_matches else "reconciled")
            )
            if captured_content is not None and not content_matches:
                self._track_message_content(
                    key,
                    captured_content,
                    append=False,
                )
            self._message_completed.add(key)
            self._pending_assistant_messages.pop(key, None)
            self._waiting_assistant_messages.discard(key)
            facts.append(
                make_fact(
                    MessageFact,
                    common,
                    phase=phase,
                    message_id=message_id,
                    source_message_id=message.id,
                    role=role,
                    content=(
                        captured_content
                        if role == "assistant" and not delivered_matches
                        else (None if content_matches else captured_content)
                    ),
                    fingerprint=fingerprint,
                    from_state_snapshot=True,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
            )
            facts.extend(
                self._tool_facts(
                    message,
                    namespace=namespace,
                    parent_message_id=message_id,
                    common=common,
                )
            )
            if message.message_type == "tool" and message.tool_call_id is not None:
                facts.extend(
                    self._tool_result(message, namespace=namespace, common=common)
                )
        for baselines in (
            self._inherited_message_fingerprints,
            self._inheritable_message_fingerprints,
        ):
            for key in tuple(baselines):
                if key[0] == namespace and key not in current_message_keys:
                    baselines.pop(key)
        previous_message_keys = {
            key for key in self._message_fingerprints if key[0] == namespace
        }
        for key in previous_message_keys - current_message_keys:
            suppressed = key in self._subagent_task_message_keys
            self._message_fingerprints.pop(key, None)
            self._delivered_message_fingerprints.pop(key, None)
            self._message_seen.discard(key)
            self._message_contents.pop(key, None)
            self._message_completed.discard(key)
            self._pending_assistant_messages.pop(key, None)
            self._waiting_assistant_messages.discard(key)
            self._subagent_task_message_keys.discard(key)
            if suppressed:
                continue
            facts.append(
                make_fact(
                    MessageFact,
                    common,
                    phase="removed",
                    message_id=_scope_id("message", namespace, key[1]),
                    source_message_id=key[1],
                    role="other",
                )
            )
        current_interrupts = {(namespace, item.id) for item in observation.interrupts}
        for interrupt in observation.interrupts:
            key = (namespace, interrupt.id)
            if key in self._pending_interactions:
                continue
            # LangGraph v2 propagates a dynamic child's interrupt through every
            # ancestor values snapshot. The deepest observed scope owns correlation;
            # treating the later root copy as another review would compare child
            # actions with the parent's unrelated Tool calls and fail the whole Run.
            if any(
                pending_id == interrupt.id
                and len(pending_namespace) > len(namespace)
                and pending_namespace[: len(namespace)] == namespace
                for pending_namespace, pending_id in self._pending_interactions
            ):
                continue
            interaction_kind = classify_interaction(interrupt.value)
            tool_call_ids = interaction_tool_call_ids(
                interrupt.value,
                observation.messages,
            )
            self._pending_interactions[key] = _PendingInteraction(
                kind=interaction_kind,
                tool_call_ids=tool_call_ids,
            )
            facts.append(
                make_fact(
                    InteractionFact,
                    common,
                    phase="opened",
                    interaction_id=_scope_id("interaction", namespace, interrupt.id),
                    source_interaction_id=interrupt.id,
                    interaction_kind=interaction_kind,
                    tool_call_ids=tool_call_ids,
                    status="pending",
                    payload=self._capture_interaction(
                        interrupt.value,
                        interaction_kind=interaction_kind,
                    ),
                )
            )
        for key in tuple(self._pending_interactions):
            if key[0] != namespace or key in current_interrupts:
                continue
            pending = self._pending_interactions.pop(key)
            facts.append(
                make_fact(
                    InteractionFact,
                    common,
                    phase="resolved",
                    interaction_id=_scope_id("interaction", namespace, key[1]),
                    source_interaction_id=key[1],
                    interaction_kind=pending.kind,
                    tool_call_ids=pending.tool_call_ids,
                    status="resolved",
                )
            )
        return facts

    def _subagent_start(
        self,
        observation: RuntimeObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        """Open one proven child scope before its first observed work.

        Child callbacks can precede Native parts. Their verified parent task Tool
        execution supplies the existing preparation boundary; a callback's own start
        time must not replace missing evidence. Native-only observations retain their
        first-child-part boundary when no Tool callback was captured.

        Args:
            observation: Native child activity or the start of a child callback.
            source_id: Identity of the observation that first exposes this scope.

        Returns:
            One opening fact, or no facts for an existing or unproven child scope.

        Raises:
            TraceCorruption: A proven child callback lacks its parent execution evidence.
        """

        if not isinstance(
            observation,
            (
                ModelCallObservation,
                ToolExecutionObservation,
                NativeMessageObservation,
                NativeReasoningObservation,
                NativeTaskObservation,
                NativeStateObservation,
                NativeExtraObservation,
            ),
        ):
            return []
        namespace = observation.graph_namespace
        if not namespace:
            return []
        if namespace in self._active_subagents:
            return []
        descriptor = self._subagent_descriptors.get(namespace)
        if descriptor is None:
            return []
        callback_start = isinstance(
            observation, (ModelCallObservation, ToolExecutionObservation)
        )
        if callback_start and observation.phase != "started":
            return []
        agent_name: str | None = descriptor.agent_name
        if isinstance(observation, NativeMessageObservation):
            raw_name = observation.metadata.get("lc_agent_name")
            if isinstance(raw_name, str) and raw_name:
                if agent_name is not None and raw_name != agent_name:
                    raise TraceCorruption(
                        "Subagent name conflicts with its parent task Tool"
                    )
                agent_name = raw_name
        subagent_id = _scope_id("subagent", namespace, namespace[-1])
        parent_execution_id = self._tool_execution_by_call.get(
            (namespace[:-1], descriptor.parent_tool_call_id)
        )
        parent_execution = (
            None
            if parent_execution_id is None
            else self._tool_executions.get(parent_execution_id)
        )
        if callback_start and (
            parent_execution is None or parent_execution.tool_name != "task"
        ):
            raise TraceCorruption(
                "Subagent callback has no observed parent task execution boundary"
            )
        if (
            callback_start
            and parent_execution is not None
            and (
                parent_execution.observed_at > observation.observed_at
                or parent_execution.monotonic_ns > observation.monotonic_ns
            )
        ):
            raise TraceCorruption("Subagent execution boundary follows child callback")
        parent_tool_call_id = descriptor.parent_tool_call_id
        model_call_id = self._tool_call_models.get(
            (namespace[:-1], descriptor.parent_tool_call_id)
        )
        started_at = (
            observation.observed_at
            if parent_execution is None
            else parent_execution.observed_at
        )
        started_monotonic_ns = (
            observation.monotonic_ns
            if parent_execution is None
            else parent_execution.monotonic_ns
        )
        self._active_subagents[namespace] = (subagent_id, agent_name)
        self._subagent_parent_calls[namespace] = parent_tool_call_id
        return [
            SubagentFact(
                source_observation_id=source_id,
                identity=observation.identity,
                graph_namespace=namespace,
                occurred_at=started_at,
                monotonic_ns=started_monotonic_ns,
                phase="started",
                subagent_id=subagent_id,
                agent_name=agent_name,
                parent_tool_call_id=parent_tool_call_id,
                parent_execution_id=parent_execution_id,
                model_call_id=model_call_id,
                input=self._capture_tool(
                    tool_name="task",
                    value={
                        "description": descriptor.description,
                        "subagent_type": descriptor.agent_name,
                    },
                    target="arguments",
                ),
                status="running",
            )
        ]

    def _in_subagent_scope(self, namespace: tuple[str, ...]) -> bool:
        return bool(namespace) and (
            namespace in self._active_subagents
            or namespace in self._subagent_descriptors
        )

    def _remember_subagent_descriptors(
        self,
        observation: NativeTaskObservation,
    ) -> None:
        """Index verified Deep Agents task Tool inputs before child parts arrive.

        Deep Agents 0.7.5 emits the parent ``tools`` task start before each child
        namespace. The task input carries model Tool calls; only exact ``task`` calls
        with the locked ``description`` and ``subagent_type`` fields establish public
        subagent identity. Contract coverage mirrors the adapter provenance tests.
        """

        if (
            observation.phase != "start"
            or observation.name != "tools"
            or not isinstance(observation.input, list)
        ):
            return
        descriptors: list[tuple[str, str, str]] = []
        for raw_call in observation.input:
            if not isinstance(raw_call, dict) or raw_call.get("name") != "task":
                continue
            raw_id = raw_call.get("id")
            raw_args = raw_call.get("args")
            if (
                not isinstance(raw_id, str)
                or not raw_id
                or not isinstance(raw_args, dict)
            ):
                continue
            description = raw_args.get("description")
            agent_name = raw_args.get("subagent_type")
            if (
                not isinstance(description, str)
                or not description
                or not isinstance(agent_name, str)
                or not agent_name
            ):
                continue
            descriptors.append((raw_id, agent_name, description))
        multiple = len(descriptors) > 1
        for index, (tool_call_id, agent_name, description) in enumerate(descriptors):
            suffix = (
                f"{observation.task_id}:{index}" if multiple else observation.task_id
            )
            namespace = (*observation.graph_namespace, f"tools:{suffix}")
            descriptor = _SubagentDescriptor(
                parent_tool_call_id=tool_call_id,
                parent_task_id=observation.task_id,
                agent_name=agent_name,
                description=description,
            )
            existing = self._subagent_descriptors.get(namespace)
            if existing is not None and existing != descriptor:
                raise TraceCorruption(
                    "Subagent parent task Tool changed before child execution"
                )
            self._subagent_descriptors[namespace] = descriptor
            self._subagent_task_descriptions[namespace] = description

    def _subagent_completions(
        self,
        observation: NativeTaskObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        """Close direct child graph scopes when their owning task returns."""

        if observation.phase != "result" or observation.interrupts:
            return []
        parent = observation.graph_namespace
        # LangGraph v2 scopes encode a direct child as ``<node>:<owning-task-id>``.
        # The locked Plan fixture and the parent-task regression test verify this join.
        matches = [
            namespace
            for namespace in self._active_subagents
            if len(namespace) == len(parent) + 1
            and namespace[: len(parent)] == parent
            and (
                (
                    self._subagent_descriptors[namespace].parent_task_id
                    if namespace in self._subagent_descriptors
                    else namespace[-1].partition(":")[2]
                )
                == observation.task_id
            )
        ]
        facts: list[TraceSemanticFact] = []
        for namespace in sorted(matches):
            subagent_id, agent_name = self._active_subagents.pop(namespace)
            self._subagent_descriptors.pop(namespace, None)
            self._subagent_task_descriptions.pop(namespace, None)
            facts.append(
                SubagentFact(
                    source_observation_id=source_id,
                    identity=observation.identity,
                    graph_namespace=namespace,
                    occurred_at=observation.observed_at,
                    monotonic_ns=observation.monotonic_ns,
                    phase="completed",
                    subagent_id=subagent_id,
                    agent_name=agent_name,
                    status="failed" if observation.error_type else "succeeded",
                )
            )
        return facts

    def _complete_subagent_call(
        self,
        parent_key: tuple[tuple[str, ...], str],
        *,
        common: Mapping[str, object],
    ) -> list[TraceSemanticFact]:
        """Cancel unresolved child reviews only when their parent call has returned.

        A parent result can precede the owning task's authoritative completion.
        Without a pending review, keep that task active so its actual outcome is
        recorded. A still-pending review cannot resume after its parent returns.
        """

        returned_scopes = tuple(
            namespace
            for namespace, call in self._subagent_parent_calls.items()
            if (namespace[:-1], call) == parent_key
        )
        facts: list[TraceSemanticFact] = []
        for namespace in returned_scopes:
            for scope in tuple(self._subagent_parent_calls):
                if scope[: len(namespace)] == namespace:
                    self._subagent_parent_calls.pop(scope)
            if not any(
                scope[: len(namespace)] == namespace
                for scope, _interrupt_id in self._pending_interactions
            ):
                continue
            for scope in tuple(self._active_subagents):
                if scope[: len(namespace)] != namespace:
                    continue
                subagent_id, agent_name = self._active_subagents.pop(scope)
                facts.append(
                    make_fact(
                        SubagentFact,
                        common,
                        graph_namespace=scope,
                        in_subagent_scope=True,
                        phase="completed",
                        subagent_id=subagent_id,
                        agent_name=agent_name,
                        status="abandoned",
                    )
                )
                self._subagent_descriptors.pop(scope, None)
                self._subagent_task_descriptions.pop(scope, None)
            for key in tuple(self._pending_interactions):
                scope, interrupt_id = key
                if scope[: len(namespace)] != namespace:
                    continue
                pending = self._pending_interactions.pop(key)
                facts.append(
                    make_fact(
                        InteractionFact,
                        common,
                        graph_namespace=scope,
                        in_subagent_scope=True,
                        phase="resolved",
                        interaction_id=_scope_id("interaction", scope, interrupt_id),
                        source_interaction_id=interrupt_id,
                        interaction_kind=pending.kind,
                        tool_call_ids=pending.tool_call_ids,
                        status="cancelled",
                    )
                )
            unresolved = (
                self._tool_started - self._tool_results - self._settled_tool_calls
            )
            for scope, tool_call_id in sorted(unresolved):
                if scope[: len(namespace)] != namespace:
                    continue
                tool_key = (scope, tool_call_id)
                tool_name = self._tool_names.get(tool_key)
                if tool_name is not None and self._policy.traces_tool(tool_name):
                    facts.append(
                        make_fact(
                            ToolFact,
                            common,
                            graph_namespace=scope,
                            in_subagent_scope=True,
                            phase="abandoned",
                            tool_call_id=_scope_id("tool", scope, tool_call_id),
                            source_tool_call_id=tool_call_id,
                            parent_call_id=self._tool_call_models.get(tool_key),
                            tool_name=tool_name,
                        )
                    )
                self._tool_results.add(tool_key)
                self._tool_completed.add(tool_key)
        return facts

    def _resolved_interaction_facts(
        self,
        summaries: tuple[RunResumeSummary, ...],
        common: Mapping[str, object],
    ) -> list[TraceSemanticFact]:
        facts: list[TraceSemanticFact] = []
        for summary in summaries:
            (
                interaction_namespace,
                pending_interaction,
            ) = self._resolve_pending_interaction(
                summary.interrupt_id,
            )
            facts.append(
                make_fact(
                    InteractionFact,
                    common,
                    phase="resolved",
                    interaction_id=_scope_id(
                        "interaction",
                        interaction_namespace,
                        summary.interrupt_id,
                    ),
                    source_interaction_id=summary.interrupt_id,
                    graph_namespace=interaction_namespace,
                    in_subagent_scope=self._in_subagent_scope(interaction_namespace),
                    interaction_kind=pending_interaction.kind,
                    tool_call_ids=pending_interaction.tool_call_ids,
                    status=summary.status,
                    payload=(
                        None
                        if summary.decision is None
                        else self._capture(
                            {"decision": summary.decision},
                            content_kind="interaction",
                            component_name=pending_interaction.kind,
                        )
                    ),
                )
            )
        return facts

    def _resolve_pending_interaction(
        self,
        interrupt_id: str,
    ) -> tuple[tuple[str, ...], _PendingInteraction]:
        matches = [key for key in self._pending_interactions if key[1] == interrupt_id]
        if len(matches) > 1:
            raise TraceCorruption(
                "Native interrupt ID is ambiguous across graph scopes",
                context={"interrupt_id": interrupt_id},
            )
        if not matches:
            return (), _PendingInteraction(kind="resume", tool_call_ids=())
        key = matches[0]
        pending = self._pending_interactions.pop(key)
        return key[0], pending

    @property
    def _payload_budget(self) -> int:
        return max(1024, self._limits.max_event_bytes // 2)

    def _capture(
        self,
        value: JsonValue,
        *,
        content_kind: RedactionContentKind,
        component_name: str | None = None,
        divisor: int = 1,
    ) -> CapturedValue:
        return self._capture_pipeline.capture(
            value,
            context=RedactionContext(
                content_kind=content_kind,
                component_name=component_name,
            ),
            max_bytes=max(1024, self._payload_budget // divisor),
        )

    def _capture_structure(
        self,
        value: JsonValue,
        *,
        content_kind: RedactionContentKind,
        component_name: str | None = None,
        divisor: int = 1,
    ) -> CapturedValue:
        return self._capture_pipeline.capture_structure(
            value,
            context=RedactionContext(
                content_kind=content_kind,
                component_name=component_name,
            ),
            max_bytes=max(1024, self._payload_budget // divisor),
        )

    def _capture_tool(
        self,
        *,
        tool_name: str,
        value: JsonValue,
        target: Literal["arguments", "result"],
    ) -> CapturedValue:
        return self._capture_pipeline.capture_tool(
            tool_name=tool_name,
            value=value,
            target=target,
            max_bytes=self._payload_budget,
        )

    def _track_message_content(
        self,
        key: tuple[tuple[str, ...], str],
        content: CapturedValue,
        *,
        append: bool,
    ) -> None:
        if not append or key not in self._message_contents:
            self._message_contents[key] = content
            return
        previous = self._message_contents[key]
        # Once any prefix is omitted, later fragments cannot recover the whole
        # delivery. Only an authoritative complete snapshot can replace omission.
        if previous.disposition == "omitted":
            return
        if content.disposition == "omitted":
            self._message_contents[key] = content
            return
        combined = append_json_content(previous.value, content.value)
        self._message_contents[key] = self._capture_pipeline.bound(
            combined,
            max_bytes=self._payload_budget,
        )

    def _track_reasoning_content(
        self,
        key: tuple[tuple[str, ...], str],
        content: CapturedValue,
        *,
        append: bool,
    ) -> None:
        if content.disposition == "omitted":
            self._reasoning_contents.pop(key, None)
            return
        if not append or key not in self._reasoning_contents:
            self._reasoning_contents[key] = content.value
            return
        combined = append_json_content(self._reasoning_contents[key], content.value)
        bounded = self._capture_pipeline.bound(
            combined,
            max_bytes=self._payload_budget,
        )
        if bounded.disposition == "inline":
            self._reasoning_contents[key] = bounded.value
        else:
            self._reasoning_contents.pop(key, None)

    def _capture_interaction(
        self,
        value: JsonValue,
        *,
        interaction_kind: str,
    ) -> CapturedValue:
        if not isinstance(value, dict):
            return self._capture_required_interaction(
                self._capture(
                    value,
                    content_kind="interaction",
                    component_name=interaction_kind,
                )
            )
        raw_actions = value.get("action_requests")
        if not isinstance(raw_actions, list):
            return self._capture_required_interaction(
                self._capture(
                    value,
                    content_kind="interaction",
                    component_name=interaction_kind,
                )
            )
        redacted_base = self._capture_pipeline.redact(
            {key: item for key, item in value.items() if key != "action_requests"},
            context=RedactionContext(
                content_kind="interaction",
                component_name=interaction_kind,
            ),
        )
        if not isinstance(redacted_base, dict):  # pragma: no cover - shape validation
            raise TraceCaptureRejected("Interaction redaction must return an object")
        actions: list[JsonValue] = []
        for raw_action in raw_actions:
            if not isinstance(raw_action, dict):
                actions.append(
                    self._capture_structure(
                        raw_action,
                        content_kind="interaction",
                        component_name=interaction_kind,
                    ).model_dump(
                        mode="json",
                        by_alias=True,
                    )
                )
                continue
            raw_name = raw_action.get("name")
            tool_name = (
                raw_name if isinstance(raw_name, str) and raw_name else "unknown"
            )
            raw_arguments = raw_action.get("args")
            arguments: JsonValue = raw_arguments if raw_arguments is not None else {}
            action: dict[str, JsonValue] = {"name": tool_name}
            captured_arguments = self._capture_tool(
                tool_name=tool_name,
                value=arguments,
                target="arguments",
            )
            action["arguments"] = captured_arguments.model_dump(
                mode="json",
                by_alias=True,
            )
            # Selected captures contain JSON Pointer keys, not original arguments.
            action["arguments_retention"] = {
                "full_content": "full",
                "selected_content": "selected",
                "metadata_only": "none",
                "disabled": "none",
            }[self._policy.tool_capture(tool_name).mode]
            description = raw_action.get("description")
            if (
                isinstance(description, str)
                and description
                and captured_arguments.disposition == "inline"
                and self._policy.captures_review_description(tool_name)
            ):
                captured_description = self._capture(
                    description,
                    content_kind="interaction",
                    component_name=tool_name,
                )
                if captured_description.disposition == "inline":
                    action["description"] = captured_description.value
            actions.append(action)
        redacted_base["action_requests"] = actions
        return self._capture_required_interaction(
            self._capture_pipeline.bound(
                redacted_base,
                max_bytes=self._payload_budget,
            )
        )

    @staticmethod
    def _capture_required_interaction(captured: CapturedValue) -> CapturedValue:
        """Reject a pause that cannot be reconstructed from its durable Trace."""

        if captured.disposition == "omitted":
            raise TraceCaptureRejected(
                "Pending interaction payload exceeds the safe Trace boundary"
            )
        return captured

    def _message_fingerprint(
        self,
        message: NativeMessageRecord,
        namespace: tuple[str, ...],
    ) -> str:
        if message.message_type == "tool":
            tool_name = (
                self._tool_names.get((namespace, message.tool_call_id or ""))
                or message.name
                or "unknown"
            )
            content = self._capture_tool(
                tool_name=tool_name,
                value=message.content,
                target="result",
            )
        else:
            content = self._message_content(message)
        safe_calls: list[JsonValue] = [
            {
                "id": call.id,
                "name": call.name,
                "arguments": self._capture_tool(
                    tool_name=call.name,
                    value=call.arguments,
                    target="arguments",
                ).model_dump(mode="json", by_alias=True),
            }
            for call in message.tool_calls
        ]
        safe_chunks: list[JsonValue] = [
            {
                "index": chunk.index,
                "id": chunk.id,
                "name": chunk.name,
                "safeSizeBytes": len(chunk.arguments.encode()),
            }
            for chunk in message.tool_call_chunks
        ]
        return fingerprint(
            {
                "messageType": message.message_type,
                "id": message.id,
                "name": message.name,
                "content": content.model_dump(mode="json", by_alias=True),
                "toolCalls": safe_calls,
                "toolCallChunks": safe_chunks,
                "toolCallId": message.tool_call_id,
                "toolStatus": message.tool_status,
            }
        )

    def _register_inheritable_message(
        self,
        message: NativeMessageRecord,
        *,
        key: tuple[tuple[str, ...], str],
    ) -> None:
        """Retain equality evidence only for complete, unchanged public messages."""

        self._inheritable_message_fingerprints.pop(key, None)
        if (
            message.id is None
            or message.message_type in {"assistant_chunk", "remove"}
            or message.tool_call_chunks
        ):
            return
        if message.message_type == "tool":
            tool_name = self._tool_names.get((key[0], message.tool_call_id or ""))
            content = self._capture_tool(
                tool_name=tool_name or message.name or "unknown",
                value=message.content,
                target="result",
            )
        else:
            content = self._message_content(message)
        if content.disposition != "inline" or content.value != message.content:
            return
        for call in message.tool_calls:
            captured = self._capture_tool(
                tool_name=call.name, value=call.arguments, target="arguments"
            )
            if captured.disposition != "inline" or captured.value != call.arguments:
                return
        self._inheritable_message_fingerprints[key] = self._message_fingerprint(
            message, key[0]
        )

    def _is_inherited_message(
        self,
        message: NativeMessageRecord,
        *,
        key: tuple[tuple[str, ...], str],
        fingerprint: str,
        first_snapshot: bool,
    ) -> bool:
        """Keep proven ancestor inputs from becoming new child outputs.

        A child's first state can contain the parent's conversation. Both copies
        must have been observed with complete, unchanged capture in this session;
        historical sanitized facts alone cannot prove that raw inputs were equal.
        Actual local message delivery or model output always takes precedence.
        """

        inherited = self._inherited_message_fingerprints.pop(key, None)
        if self._inheritable_message_fingerprints.get(key) != fingerprint:
            return False
        if inherited == fingerprint:
            self._inherited_message_fingerprints[key] = fingerprint
            return True
        namespace, source_id = key
        if (
            not first_snapshot
            or not namespace
            or message.id is None
            or key in self._message_seen
            or key in self._pending_assistant_messages
            or key in self._completed_model_messages
        ):
            return False
        for depth in range(len(namespace)):
            ancestor = (namespace[:depth], source_id)
            if self._inheritable_message_fingerprints.get(ancestor) == fingerprint:
                self._inherited_message_fingerprints[key] = fingerprint
                return True
        return False

    def _register_subagent_task_message(
        self,
        message: NativeMessageRecord,
        *,
        namespace: tuple[str, ...],
        key: tuple[tuple[str, ...], str],
        fingerprint: str,
    ) -> bool:
        """Deduplicate the proven child task input already owned by SubagentFact."""

        if (
            message.message_type != "human"
            or namespace in self._subagent_task_message_scopes
            or namespace not in self._active_subagents
        ):
            return False
        # Only the first observed user input can represent the delegated task. A
        # rewritten first input must not make a later real message eligible instead.
        self._subagent_task_message_scopes.add(namespace)
        if key in self._message_seen:
            return False
        description = self._subagent_task_descriptions.get(namespace)
        candidate = message.content
        if namespace not in self._subagent_descriptors:
            # Hydration retains sanitized arguments. Compare through the same Tool
            # capture rule so root JSON Pointer selection and business redaction keep
            # their meaning after a new observer session resumes the child scope.
            captured = self._capture_tool(
                tool_name="task",
                value={
                    "description": message.content,
                    "subagent_type": self._active_subagents[namespace][1],
                },
                target="arguments",
            )
            task_input = captured.value
            if isinstance(task_input, dict) and set(task_input) == {""}:
                task_input = task_input[""]
            candidate = (
                task_input.get("description") if isinstance(task_input, dict) else None
            )
        if description is None or candidate != description:
            return False
        self._subagent_task_message_keys.add(key)
        self._message_seen.add(key)
        self._message_completed.add(key)
        self._message_fingerprints[key] = fingerprint
        self._delivered_message_fingerprints[key] = fingerprint
        return True

    def _message_content(self, message: NativeMessageRecord) -> CapturedValue:
        return self._capture(
            message.content,
            content_kind="message",
            component_name=message.name,
        )
