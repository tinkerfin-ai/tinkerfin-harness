"""Standalone, read-only Planning workflow for Plan-capable Deep Agents."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Generic, Protocol, TypeAlias, TypeVar, cast

from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import StateSnapshot, interrupt
from langgraph.typing import ContextT
from pydantic import ConfigDict, JsonValue, TypeAdapter

from tinkerfin_contracts import PreparedWorkspace
from tinkerfin_native_stream import RuntimeInterruptEnvelope

from .._agent_spec import AgentSpec
from .._agui_lineage_state import (
    CHECKPOINT_ROLE_METADATA_KEY,
    LINEAGE_CONFIG_KEY,
    PLANNING_CHECKPOINT_ROLE,
    RUN_ID_METADATA_KEY,
    LineageMarker,
)
from .._hitl import _as_permissions
from ._clarification import (
    build_response_schema,
    pending_contract_digest,
    restore_form,
    serialize_form,
    validate_and_normalize_response,
)
from ._config import PlanOptions
from ._content import serialize_plan_content
from ._contracts import (
    ApprovePlan,
    CancelPlan,
    EditPlanBase,
    PlanClarificationMetadata,
    PlanClarificationPayload,
    RejectPlan,
    RespondToPlan,
)
from ._json_schema import require_valid_schema
from ._planner import (
    create_planner_agent,
    create_planner_filesystem,
    invoke_plan_review_reply,
    invoke_planner,
    resolve_planner_model,
)
from ._state import (
    PLAN_CHECKPOINT_RUN_ID,
    PLAN_CONTENT_SCHEMA_FINGERPRINT_KEY,
    PLAN_SCHEMA_FINGERPRINT_KEY,
    PLAN_STATE_KEY,
    PlanningWorkflowNodeState,
    create_plan_state_schema,
    plan_state_update,
    read_plan_state,
)
from .errors import PlanModeConfigurationError, PlanStructuredOutputError
from .models import (
    ClarificationExchange,
    ConfirmedPlan,
    PendingClarification,
    PlanContentModel,
    PlanHandoff,
    PlanHandoffPhase,
    PlanReviewAction,
    PlanState,
    PlanStatus,
)

_PlanningFactory = Callable[..., object]
_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)
_SchemaT = TypeVar("_SchemaT")
_JSON_OBJECT = TypeAdapter(
    dict[str, JsonValue],
    config=ConfigDict(allow_inf_nan=False),
)


class _CompiledPlanningRuntime(Protocol):
    """Typed subset of the locked CompiledStateGraph used by Planning."""

    checkpointer: object
    store: BaseStore | None

    def astream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[Mapping[str, object]]: ...

    async def aupdate_state(
        self,
        config: RunnableConfig,
        values: Mapping[str, object],
        as_node: str | None = None,
        task_id: str | None = None,
    ) -> RunnableConfig: ...

    async def aget_state(
        self,
        config: RunnableConfig,
        *,
        subgraphs: bool = False,
    ) -> StateSnapshot: ...


_SyncPlanNode: TypeAlias = Callable[
    [PlanningWorkflowNodeState],
    dict[str, object],
]
_ConfigPlanNode: TypeAlias = Callable[
    [PlanningWorkflowNodeState, RunnableConfig],
    dict[str, object],
]
_AsyncConfigPlanNode: TypeAlias = Callable[
    [PlanningWorkflowNodeState, RunnableConfig],
    Awaitable[dict[str, object]],
]
_PlanNode: TypeAlias = (
    _SyncPlanNode
    | _ConfigPlanNode
    | _AsyncConfigPlanNode
    | _CompiledPlanningRuntime
    | Runnable[object, object]
)
_PlanPath: TypeAlias = Callable[[PlanningWorkflowNodeState], str]


class _PlanningGraphBuilder(Protocol):
    """Typed subset of StateGraph isolated from third-party unknown generics."""

    def add_node(self, node: str, action: _PlanNode) -> object: ...

    def add_edge(self, start_key: str, end_key: str) -> object: ...

    def add_conditional_edges(
        self,
        source: str,
        path: _PlanPath,
        path_map: Mapping[str, str],
    ) -> object: ...

    def compile(
        self,
        *,
        checkpointer: _CheckpointSaver,
        store: BaseStore | None,
        name: str,
    ) -> _CompiledPlanningRuntime: ...


class _SignatureCallable(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


_COMPILED_ASTREAM = cast(
    _SignatureCallable,
    CompiledStateGraph.astream,  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
)


def _messages(state: Mapping[str, object]) -> tuple[BaseMessage, ...]:
    value = state.get("messages")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("Planning workflow state requires a message sequence")
    sequence = cast(Sequence[object], value)
    messages = tuple(item for item in sequence if isinstance(item, BaseMessage))
    if len(messages) != len(sequence):
        raise TypeError("Planning workflow messages must be LangChain message objects")
    return messages


def _request_message_id(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        if not isinstance(message.id, str) or not message.id:
            raise PlanModeConfigurationError(
                "Plan Mode requires a stable ID on the current user message"
            )
        return message.id
    raise PlanModeConfigurationError("Plan Mode requires a current user message")


def _clarification_context(
    plan: PlanState[PlanContentModel],
    options: PlanOptions,
) -> tuple[dict[str, JsonValue], ...]:
    context: list[dict[str, JsonValue]] = []
    for exchange in plan.clarification_history:
        form = restore_form(options.clarification, exchange.form)
        context.append(
            _JSON_OBJECT.validate_python(
                {
                    "form": form.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    ),
                    "answers": [
                        answer.model_dump(mode="json", by_alias=True)
                        for answer in exchange.answers
                    ],
                }
            )
        )
    return tuple(context)


def _require_schema_fingerprints(
    state: Mapping[str, object],
    options: PlanOptions,
) -> None:
    if state.get(PLAN_SCHEMA_FINGERPRINT_KEY) != options.clarification.fingerprint:
        raise PlanModeConfigurationError(
            "checkpoint clarification schema does not match this Runtime"
        )
    if (
        state.get(PLAN_CONTENT_SCHEMA_FINGERPRINT_KEY)
        != options.content.reference.fingerprint
    ):
        raise PlanModeConfigurationError(
            "checkpoint Plan content schema does not match this Runtime"
        )


def _json_schema(adapter: TypeAdapter[_SchemaT]) -> dict[str, JsonValue]:
    return _JSON_OBJECT.validate_python(adapter.json_schema(by_alias=True))


def _runtime_interrupt_value(
    envelope: RuntimeInterruptEnvelope,
) -> dict[str, JsonValue]:
    require_valid_schema(envelope.response_schema)
    return _JSON_OBJECT.validate_python(
        envelope.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
    )


def _checkpoint_saver(value: object) -> _CheckpointSaver:
    if not isinstance(value, BaseCheckpointSaver):
        raise PlanModeConfigurationError(
            "Plan Mode requires a concrete BaseCheckpointSaver"
        )
    return cast(_CheckpointSaver, value)


def _planning_config(value: object) -> RunnableConfig:
    """Derive a Planning config without overwriting semantic Run lineage.

    LangGraph's ``run_id`` index is reserved for the stable Planning checkpoint owner;
    the caller's semantic Run ID remains under TinkerFin private metadata. The copy
    prevents Profile and role injection from mutating host-owned config mappings.
    """

    if value is None:
        config = RunnableConfig()
    elif isinstance(value, Mapping):
        config = cast(RunnableConfig, dict(cast(Mapping[str, object], value)))
    else:
        raise TypeError("config must be a mapping or None")
    raw_configurable = config.get("configurable")
    if raw_configurable is None:
        configurable: dict[str, object] = {}
    elif isinstance(raw_configurable, Mapping):
        configurable = dict(cast(Mapping[str, object], raw_configurable))
    else:
        raise TypeError("config.configurable must be a mapping")
    existing = configurable.get("run_id")
    semantic_run_id = configurable.get(RUN_ID_METADATA_KEY)
    if semantic_run_id is None and existing not in (None, PLAN_CHECKPOINT_RUN_ID):
        raise PlanModeConfigurationError(
            "Plan Mode reserves config.configurable['run_id'] for Planning checkpoints"
        )
    if semantic_run_id is not None and (
        not isinstance(semantic_run_id, str) or not semantic_run_id
    ):
        raise PlanModeConfigurationError(
            "Plan Mode requires a canonical AG-UI semantic run ID"
        )
    configurable["run_id"] = PLAN_CHECKPOINT_RUN_ID
    configurable[CHECKPOINT_ROLE_METADATA_KEY] = PLANNING_CHECKPOINT_ROLE
    lineage = configurable.get(LINEAGE_CONFIG_KEY)
    if lineage is not None:
        if not isinstance(lineage, LineageMarker):
            raise PlanModeConfigurationError("Planning requires typed run ownership")
        configurable[LINEAGE_CONFIG_KEY] = lineage.model_copy(
            update={"role": "planning"}
        )
    config["configurable"] = configurable
    return config


def _create_handoff(
    confirmed: ConfirmedPlan[PlanContentModel],
    message_id: str,
) -> PlanHandoff:
    payload = {
        "messageId": message_id,
        "confirmedPlan": confirmed.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PlanHandoff(
        message_id=message_id,
        digest=hashlib.sha256(encoded).hexdigest(),
    )


class PlanningWorkflowGraph(Generic[ContextT]):
    """Compiled Planning graph with a mandatory synchronous durability boundary."""

    __slots__ = ("_graph", "_signature")

    def __init__(self, graph: _CompiledPlanningRuntime) -> None:
        """Wrap one compiled graph without taking ownership of its durable resources."""

        self._graph = graph
        self._signature = inspect.signature(graph.astream)

    @property
    def checkpointer(self) -> _CheckpointSaver:
        """Return the concrete borrowed checkpointer used by this graph."""

        return _checkpoint_saver(self._graph.checkpointer)

    @property
    def store(self) -> BaseStore | None:
        """Return the optional borrowed Store used by this graph."""

        return self._graph.store

    def astream(
        self, *args: object, **kwargs: object
    ) -> AsyncIterator[Mapping[str, object]]:
        """Stream Planning with synchronously committed interrupt boundaries."""

        bound = self._signature.bind(*args, **kwargs)
        durability = bound.arguments.get("durability")
        if durability not in (None, "sync"):
            raise PlanModeConfigurationError("Plan Mode requires durability='sync'")
        bound.arguments["durability"] = "sync"
        bound.arguments["config"] = _planning_config(bound.arguments.get("config"))
        return self._graph.astream(*bound.args, **bound.kwargs)

    async def aget_state(
        self,
        config: RunnableConfig,
        *,
        subgraphs: bool = False,
    ) -> StateSnapshot:
        """Read the current Planning checkpoint without changing ownership."""

        return await self._graph.aget_state(
            _planning_config(config),
            subgraphs=subgraphs,
        )

    async def mark_handoff_phase(
        self,
        config: RunnableConfig,
        plan: PlanState[PlanContentModel],
        *,
        phase: PlanHandoffPhase,
        native_checkpoint_id: str,
        completed_checkpoint_id: str | None = None,
    ) -> PlanState[PlanContentModel]:
        """Synchronously commit verified native progress on the Planning head.

        LangGraph 1.2.10 does not mutate the caller's resume config to its latest
        checkpoint. Handoff transitions therefore remove the consumed checkpoint ID and
        let the borrowed saver select the canonical Planning head; updating the original
        interrupt checkpoint would fork away durable resume evidence.

        Args:
            config: Current request configuration used to locate the Planning thread.
            plan: Approved Plan with the current durable handoff state.
            phase: Next legal native handoff phase.
            native_checkpoint_id: Native Graph checkpoint proving accepted work.
            completed_checkpoint_id: Optional checkpoint proving native completion.

        Returns:
            Updated immutable Plan state committed on the canonical Planning head.

        Raises:
            RuntimeError: The Plan or requested handoff transition is invalid.
            TinkerFinLifecycleError: Planning checkpoint ownership cannot be proven.
        """

        handoff = plan.handoff
        if plan.status is not PlanStatus.APPROVED or handoff is None:
            raise RuntimeError("only an approved Plan can advance a native handoff")
        if handoff.phase is phase:
            return plan
        expected = {
            PlanHandoffPhase.PENDING: PlanHandoffPhase.ACCEPTED,
            PlanHandoffPhase.ACCEPTED: PlanHandoffPhase.COMPLETED,
        }.get(handoff.phase)
        if phase is not expected:
            raise RuntimeError(
                f"invalid Plan handoff transition: {handoff.phase} -> {phase}"
            )
        updated = plan.model_copy(
            update={
                "handoff": handoff.model_copy(
                    update={
                        "phase": phase,
                        "native_checkpoint_id": native_checkpoint_id,
                        "completed_checkpoint_id": completed_checkpoint_id,
                    }
                )
            }
        )
        head_config = _planning_config(config)
        configurable = dict(head_config.get("configurable", {}))
        configurable.pop("checkpoint_id", None)
        head_config["configurable"] = configurable
        await self._graph.aupdate_state(
            head_config,
            plan_state_update(updated),
            as_node="review_plan",
        )
        return updated


setattr(
    PlanningWorkflowGraph.astream,
    "__signature__",
    inspect.signature(_COMPILED_ASTREAM),
)


class _PlanningGraphFactory(Generic[ContextT]):
    """Deferred builder that borrows one Deep Agent definition's resources."""

    def __init__(self, options: PlanOptions) -> None:
        self._options = options

    def _require_runtime_configuration(
        self,
        spec: AgentSpec[ContextT],
    ) -> str | BaseChatModel:
        model = self._options.planner_model or spec.model
        if not isinstance(model, (str, BaseChatModel)) or (
            isinstance(model, str) and not model.strip()
        ):
            raise PlanModeConfigurationError("Plan Mode requires an explicit model")
        _checkpoint_saver(spec.checkpointer)
        return model

    def _build(
        self,
        spec: AgentSpec[ContextT],
        *,
        workspace: PreparedWorkspace[object, BackendProtocol] | None = None,
    ) -> PlanningWorkflowGraph[ContextT]:
        model = self._require_runtime_configuration(spec)
        context_schema = spec.context_schema
        backend_value = spec.backend
        backend = (
            StateBackend()
            if backend_value is None
            else cast(BackendProtocol, backend_value)
        )
        base_state_schema = spec.state_schema
        caller_middleware = spec.middleware
        supplied_tools = spec.tools
        read_only_tools = (
            tuple(
                tool
                for tool in cast(Sequence[object], supplied_tools)
                if isinstance(tool, BaseTool)
                and tool.metadata
                and tool.metadata.get("read_only") is True
            )
            if isinstance(supplied_tools, Sequence)
            else ()
        )
        resolved_model = resolve_planner_model(self._options.planner_model or model)
        filesystem = create_planner_filesystem(
            backend,
            permissions=_as_permissions(spec.permissions),
            filesystem_instructions=(
                None if workspace is None else workspace.filesystem_instructions
            ),
        )
        state_schema = create_plan_state_schema(
            base_state_schema,
            middleware=(*caller_middleware, filesystem),
        )
        planner = create_planner_agent(
            resolved_model,
            filesystem=filesystem,
            attachments=spec.attachments,
            read_only_tools=read_only_tools,
            tool_scope=spec.tool_scope,
            clarification=self._options.clarification,
            content=self._options.content,
            response_type=self._options.contracts.planner_response_type,
            context_schema=context_schema,
        )
        edit_planner = create_planner_agent(
            resolved_model,
            filesystem=filesystem,
            attachments=spec.attachments,
            read_only_tools=read_only_tools,
            tool_scope=spec.tool_scope,
            clarification=self._options.clarification,
            content=self._options.content,
            response_type=self._options.contracts.planner_edit_response_type,
            context_schema=context_schema,
        )

        def initialize_node(state: PlanningWorkflowNodeState) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            messages = _messages(mapped)
            if PLAN_STATE_KEY in mapped:
                current = read_plan_state(mapped, self._options.content)
                if current.status is PlanStatus.AWAITING_INPUT:
                    _require_schema_fingerprints(mapped, self._options)
                    continued = current.model_copy(
                        update={
                            "status": PlanStatus.PLANNING,
                            "effective_mode": "plan",
                            "request_message_id": _request_message_id(messages),
                            "pending_clarification": None,
                            "pending_edit": None,
                            "confirmed_plan": None,
                            "handoff": None,
                            "review_action": None,
                            "review_reason": None,
                        }
                    )
                    return plan_state_update(continued)
            plan = self._options.content.state_type(
                request_message_id=_request_message_id(messages)
            )
            return {
                **plan_state_update(plan),
                PLAN_SCHEMA_FINGERPRINT_KEY: self._options.clarification.fingerprint,
                PLAN_CONTENT_SCHEMA_FINGERPRINT_KEY: (
                    self._options.content.reference.fingerprint
                ),
            }

        async def planner_node(
            state: PlanningWorkflowNodeState,
            config: RunnableConfig,
        ) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            _require_schema_fingerprints(mapped, self._options)
            current = read_plan_state(mapped, self._options.content)
            has_authoritative_edit = current.pending_edit is not None
            response_type = (
                self._options.contracts.planner_edit_response_type
                if has_authoritative_edit
                else self._options.contracts.planner_response_type
            )
            outcome = await invoke_planner(
                edit_planner if has_authoritative_edit else planner,
                _messages(mapped),
                current,
                response_type=response_type,
                clarification_history=_clarification_context(
                    current,
                    self._options,
                ),
                config=config,
                files=mapped.get("files"),
            )
            if outcome.type == "clarify":
                form, form_payload = serialize_form(
                    self._options.clarification,
                    outcome.clarification,
                )
                response_schema = build_response_schema(
                    self._options.clarification,
                    form,
                )
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.AWAITING_CLARIFICATION,
                        "effective_mode": "plan",
                        "pending_clarification": PendingClarification(
                            form=form_payload,
                            response_schema=response_schema,
                            contract_digest=pending_contract_digest(
                                form_payload,
                                response_schema,
                            ),
                        ),
                        "review_action": None,
                        "review_reason": None,
                    }
                )
                return plan_state_update(updated)

            if current.pending_edit is not None:
                if outcome.type != "accept_edit":
                    raise PlanStructuredOutputError(
                        "Planner must clarify or accept the authoritative edited draft"
                    )
                raw_content = current.pending_edit
            else:
                if outcome.type != "draft" or outcome.draft is None:
                    raise PlanStructuredOutputError(
                        "Planner cannot accept an edit when no edited draft exists"
                    )
                raw_content = outcome.draft

            content, _ = serialize_plan_content(
                self._options.content,
                raw_content,
            )

            revision = current.revision + 1
            draft = self._options.content.draft_type(
                revision=revision,
                content_schema=self._options.content.reference,
                content=content,
            )
            updated = current.model_copy(
                update={
                    "status": PlanStatus.AWAITING_REVIEW,
                    "effective_mode": "plan",
                    "pending_clarification": None,
                    "draft": draft,
                    "pending_edit": None,
                    "confirmed_plan": None,
                    "handoff": None,
                    "revision": revision,
                    "review_action": None,
                    "review_reason": None,
                }
            )
            return plan_state_update(updated)

        def answer_clarification(
            state: PlanningWorkflowNodeState,
        ) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            _require_schema_fingerprints(mapped, self._options)
            current = read_plan_state(mapped, self._options.content)
            pending = current.pending_clarification
            if pending is None:
                raise RuntimeError("Plan clarification requires a pending form")
            form = restore_form(self._options.clarification, pending.form)
            if pending.contract_digest != pending_contract_digest(
                pending.form,
                pending.response_schema,
            ):
                raise PlanModeConfigurationError(
                    "checkpoint clarification contract digest is invalid"
                )
            metadata = PlanClarificationMetadata(
                clarification=PlanClarificationPayload(form=pending.form),
            )
            envelope = RuntimeInterruptEnvelope(
                kind="tinkerfin:plan_clarification",
                message="Answer required questions and optionally refine the Plan.",
                response_schema=pending.response_schema,
                metadata=_JSON_OBJECT.validate_python(
                    metadata.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    )
                ),
            )
            ordered_answers = validate_and_normalize_response(
                self._options.clarification,
                form,
                pending.response_schema,
                interrupt(_runtime_interrupt_value(envelope)),
            )
            exchange = ClarificationExchange(
                form=pending.form,
                answers=ordered_answers,
            )
            updated = current.model_copy(
                update={
                    "status": PlanStatus.PLANNING,
                    "effective_mode": "plan",
                    "pending_clarification": None,
                    "clarification_history": (
                        *current.clarification_history,
                        exchange,
                    ),
                    "review_reason": None,
                }
            )
            return plan_state_update(updated)

        def review_node(state: PlanningWorkflowNodeState) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            _require_schema_fingerprints(mapped, self._options)
            current = read_plan_state(mapped, self._options.content)
            draft = current.draft
            if draft is None:
                raise RuntimeError("Plan review requires a current draft")
            review_payload = self._options.contracts.review_payload_type.model_validate(
                {"draft": draft}
            )
            review_metadata = (
                self._options.contracts.review_metadata_type.model_validate(
                    {"review": review_payload}
                )
            )
            envelope = RuntimeInterruptEnvelope(
                kind="tinkerfin:plan_review",
                message="Review the proposed Plan before execution begins.",
                response_schema=_json_schema(self._options.contracts.review_response),
                metadata=_JSON_OBJECT.validate_python(
                    review_metadata.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    )
                ),
            )
            response = cast(
                ApprovePlan | CancelPlan | EditPlanBase | RespondToPlan | RejectPlan,
                self._options.contracts.review_response.validate_python(
                    interrupt(_runtime_interrupt_value(envelope))
                ),
            )
            if response.base_revision != draft.revision:
                raise ValueError("Plan review baseRevision is stale")

            if isinstance(response, ApprovePlan):
                message_id = current.request_message_id
                if message_id is None:
                    raise RuntimeError("Plan approval requires its request message ID")
                confirmed = self._options.content.confirmed_type.from_draft(draft)
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.APPROVED,
                        "effective_mode": "default",
                        "confirmed_plan": confirmed,
                        "handoff": _create_handoff(confirmed, message_id),
                        "review_action": PlanReviewAction.APPROVE,
                        "review_reason": None,
                    }
                )
            elif isinstance(response, CancelPlan):
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.AWAITING_INPUT,
                        "effective_mode": "plan",
                        "pending_clarification": None,
                        "pending_edit": None,
                        "confirmed_plan": None,
                        "handoff": None,
                        "review_action": PlanReviewAction.CANCEL,
                        "review_reason": None,
                    }
                )
            elif isinstance(response, EditPlanBase):
                edited = getattr(response, "content", None)
                if not isinstance(edited, self._options.content.schema):
                    raise TypeError(
                        "Plan edit did not use the configured content schema"
                    )
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.PLANNING,
                        "effective_mode": "plan",
                        "pending_edit": edited,
                        "review_action": PlanReviewAction.EDIT,
                        "review_reason": None,
                    }
                )
            elif isinstance(response, RespondToPlan):
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.PLANNING,
                        "effective_mode": "plan",
                        "pending_edit": None,
                        "feedback": (*current.feedback, response.message),
                        "review_action": PlanReviewAction.RESPOND,
                        "review_reason": None,
                    }
                )
            elif isinstance(response, RejectPlan):
                feedback = (
                    current.feedback
                    if response.message is None
                    else (*current.feedback, response.message)
                )
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.AWAITING_INPUT,
                        "effective_mode": "plan",
                        "pending_clarification": None,
                        "pending_edit": None,
                        "confirmed_plan": None,
                        "handoff": None,
                        "feedback": feedback,
                        "review_action": PlanReviewAction.REJECT,
                        "review_reason": response.message,
                    }
                )
            else:  # pragma: no cover - discriminated adapter is exhaustive
                raise TypeError("unsupported Plan review response")
            return plan_state_update(updated)

        async def respond_to_review(
            state: PlanningWorkflowNodeState,
            config: RunnableConfig,
        ) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            _require_schema_fingerprints(mapped, self._options)
            current = read_plan_state(mapped, self._options.content)
            if current.status is not PlanStatus.AWAITING_INPUT:
                raise RuntimeError("Plan review reply requires awaiting input state")
            reply_messages = _messages(mapped)
            reply = await invoke_plan_review_reply(
                resolved_model,
                reply_messages,
                current,
                config=config,
                attachments=spec.attachments,
            )
            return {"messages": [reply]}

        def planner_path(state: PlanningWorkflowNodeState) -> str:
            current = read_plan_state(
                cast(Mapping[str, object], state),
                self._options.content,
            )
            return "clarify" if current.pending_clarification is not None else "review"

        def review_path(state: PlanningWorkflowNodeState) -> str:
            action = read_plan_state(
                cast(Mapping[str, object], state),
                self._options.content,
            ).review_action
            if action is None:
                raise RuntimeError("Plan review did not record an action")
            return action.value

        builder = cast(
            _PlanningGraphBuilder,
            StateGraph(state_schema, context_schema=context_schema),
        )
        builder.add_node("initialize_plan", initialize_node)
        builder.add_node("create_plan", planner_node)
        builder.add_node("clarify_plan", answer_clarification)
        builder.add_node("review_plan", review_node)
        builder.add_node("respond_to_review", respond_to_review)
        builder.add_edge(START, "initialize_plan")
        builder.add_edge("initialize_plan", "create_plan")
        builder.add_conditional_edges(
            "create_plan",
            planner_path,
            {"clarify": "clarify_plan", "review": "review_plan"},
        )
        builder.add_edge("clarify_plan", "create_plan")
        builder.add_conditional_edges(
            "review_plan",
            review_path,
            {
                "approve": END,
                "cancel": "respond_to_review",
                "edit": "create_plan",
                "respond": "create_plan",
                "reject": "respond_to_review",
            },
        )
        builder.add_edge("respond_to_review", END)

        parent = builder.compile(
            checkpointer=_checkpoint_saver(spec.checkpointer),
            store=spec.store,
            name="tinkerfin_planning_workflow",
        )
        return PlanningWorkflowGraph[ContextT](parent)


def create_planning_graph(
    spec: AgentSpec[ContextT],
    *,
    options: PlanOptions,
    workspace: PreparedWorkspace[object, BackendProtocol] | None = None,
) -> PlanningWorkflowGraph[ContextT]:
    """Build the read-only planning workflow from its declared agent resources."""
    return _PlanningGraphFactory[ContextT](options)._build(spec, workspace=workspace)


__all__ = ["PlanningWorkflowGraph", "create_planning_graph"]
