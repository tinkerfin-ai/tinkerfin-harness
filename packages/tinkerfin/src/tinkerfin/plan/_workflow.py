"""Standalone, read-only Planning workflow for Plan-capable Deep Agents."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Generic, Protocol, TypeAlias, cast
from uuid import uuid4

from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol
from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain.tools import ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Command, StateSnapshot, interrupt
from langgraph.typing import ContextT
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, create_model

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
from .._attachment_agents import _AttachmentMiddleware, attachment_filesystem
from .._hitl import _as_permissions
from .._tool_runtime import _ToolRuntimeMiddleware
from ._clarification import (
    ClarificationDiscussionResponse,
    ClarificationDismissResponse,
    build_response_schema,
    pending_contract_digest,
    restore_form,
    serialize_form,
    validate_clarification_response,
)
from ._config import PlanOptions
from ._content import serialize_plan_content
from ._contracts import (
    ApprovePlan,
    CancelPlan,
    DismissPlanReview,
    EditPlanBase,
    PlanClarificationMetadata,
    PlanClarificationPayload,
    RejectPlan,
    RespondToPlan,
    review_response_schema,
    validate_review_response,
)
from ._json_schema import require_valid_schema
from ._lifecycle import PlanLifecycle
from ._planner import (
    create_planner_filesystem,
    resolve_planner_model,
)
from ._resume import require_plan_schemas
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
from .clarification import ClarificationFormBase
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

_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)
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
    invocation_id = config.setdefault("run_id", uuid4())
    configurable["_plan_invocation_id"] = semantic_run_id or str(invocation_id)
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
            as_node="plan_lifecycle.after_agent",
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

        def initialize_node(state: PlanningWorkflowNodeState) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            messages = _messages(mapped)
            if PLAN_STATE_KEY in mapped:
                current = read_plan_state(mapped, self._options.content)
                if current.status is PlanStatus.AWAITING_INPUT:
                    require_plan_schemas(mapped, self._options)
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

        def open_question(
            current: PlanState[PlanContentModel],
            raw_form: ClarificationFormBase,
        ) -> dict[str, object]:
            form, form_payload = serialize_form(self._options.clarification, raw_form)
            response_schema = build_response_schema(self._options.clarification, form)
            return plan_state_update(
                current.model_copy(
                    update={
                        "status": PlanStatus.AWAITING_CLARIFICATION,
                        "effective_mode": "plan",
                        "pending_clarification": PendingClarification(
                            form=form_payload,
                            response_schema=response_schema,
                            contract_digest=pending_contract_digest(
                                form_payload, response_schema
                            ),
                        ),
                        "review_action": None,
                        "review_reason": None,
                    }
                )
            )

        def open_review(
            current: PlanState[PlanContentModel],
            raw_content: PlanContentModel,
        ) -> dict[str, object]:
            content, _ = serialize_plan_content(self._options.content, raw_content)
            revision = current.revision + 1
            draft = self._options.content.draft_type(
                revision=revision,
                content_schema=self._options.content.reference,
                content=content,
            )
            return plan_state_update(
                current.model_copy(
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
            )

        def discuss_card(
            current: PlanState[PlanContentModel],
            message: str | None,
            config: RunnableConfig,
        ) -> dict[str, object]:
            # One accepted run owns the user message; a replay uses the same ID.
            run_id = config.get("configurable", {}).get("_plan_invocation_id")
            if not isinstance(run_id, str) or not run_id:
                raise PlanModeConfigurationError(
                    "Plan discussion requires a run identity"
                )
            message_id = (
                None
                if message is None
                else "plan-discussion-" + hashlib.sha256(run_id.encode()).hexdigest()
            )
            context = self._options.content.discussion_type(
                message_id=message_id,
                clarification=current.pending_clarification.form
                if current.pending_clarification
                else None,
                draft=None if current.pending_clarification else current.draft,
                submitted_edit=current.pending_edit,
            )
            updated = current.model_copy(
                update={
                    "status": PlanStatus.AWAITING_INPUT
                    if message is None
                    else PlanStatus.PLANNING,
                    "request_message_id": current.request_message_id
                    if message_id is None
                    else message_id,
                    "pending_clarification": None,
                    "pending_edit": None,
                    "confirmed_plan": None,
                    "handoff": None,
                    "review_action": None
                    if message is None
                    else PlanReviewAction.RESPOND,
                    "review_reason": None,
                    "discussion_history": (*current.discussion_history, context),
                }
            )
            return {
                **plan_state_update(updated),
                "messages": []
                if message is None
                else [HumanMessage(content=message, id=message_id)],
            }

        def answer_clarification(
            state: PlanningWorkflowNodeState,
            config: RunnableConfig,
        ) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            require_plan_schemas(mapped, self._options)
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
            response = validate_clarification_response(
                self._options.clarification,
                form,
                pending.response_schema,
                interrupt(_runtime_interrupt_value(envelope)),
            )
            if isinstance(response, ClarificationDismissResponse):
                return discuss_card(current, None, config)
            if isinstance(response, ClarificationDiscussionResponse):
                return discuss_card(current, response.message, config)
            exchange = ClarificationExchange(
                form=pending.form,
                answers=response,
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

        def review_node(
            state: PlanningWorkflowNodeState, config: RunnableConfig
        ) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            require_plan_schemas(mapped, self._options)
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
                response_schema=review_response_schema(
                    self._options.contracts, revision=draft.revision
                ),
                metadata=_JSON_OBJECT.validate_python(
                    review_metadata.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    )
                ),
            )
            response = validate_review_response(
                self._options.contracts,
                interrupt(_runtime_interrupt_value(envelope)),
                revision=draft.revision,
            )

            if isinstance(response, DismissPlanReview):
                return discuss_card(current, None, config)
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
                discussed = current.model_copy(
                    update={"feedback": (*current.feedback, response.message)}
                )
                return discuss_card(discussed, response.message, config)
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

        async def ask_user_question(
            form: ClarificationFormBase,
            runtime: ToolRuntime[ContextT, PlanningWorkflowNodeState],
        ) -> Command[Any]:
            """Submit a clarification form; the workflow waits for the user next."""
            current = read_plan_state(runtime.state, self._options.content)
            return Command(
                update={
                    **open_question(current, form),
                    "messages": [
                        ToolMessage(
                            content="Questions submitted; awaiting the user.",
                            name="ask_user_question",
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                }
            )

        async def submit_plan(
            content: PlanContentModel,
            runtime: ToolRuntime[ContextT, PlanningWorkflowNodeState],
        ) -> Command[Any]:
            """Submit a complete draft for review without granting execution."""
            current = read_plan_state(runtime.state, self._options.content)
            if current.pending_edit is not None:
                raise PlanStructuredOutputError(
                    "An authoritative edit cannot be replaced"
                )
            return Command(
                update={
                    **open_review(current, content),
                    "messages": [
                        ToolMessage(
                            content="Draft submitted; awaiting user approval.",
                            name="submit_plan",
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                }
            )

        async def confirm_plan_edit(
            runtime: ToolRuntime[ContextT, PlanningWorkflowNodeState],
        ) -> Command[Any]:
            """Confirm the saved user edit without replacing its content."""
            current = read_plan_state(runtime.state, self._options.content)
            if current.pending_edit is None:
                raise PlanStructuredOutputError("No authoritative edit is pending")
            return Command(
                update={
                    **open_review(current, current.pending_edit),
                    "messages": [
                        ToolMessage(
                            content="Edited draft submitted for review.",
                            name="confirm_plan_edit",
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                }
            )

        class ConfirmArguments(BaseModel):
            model_config = ConfigDict(extra="forbid")

        def tool_arguments(base: type[BaseModel]) -> type[BaseModel]:
            # BaseTool validates injected arguments too. Exclude the runtime from
            # serialization and the model-facing Tool schema, not from validation.
            return create_model(
                base.__name__ + "WithRuntime",
                __base__=base,
                __config__=ConfigDict(arbitrary_types_allowed=True),
                runtime=(
                    ToolRuntime[Any, PlanningWorkflowNodeState],
                    Field(exclude=True),
                ),
            )

        plan_tools = [
            StructuredTool.from_function(
                coroutine=ask_user_question,
                name="ask_user_question",
                description="Ask for missing planning requirements using the complete configured form.",
                args_schema=tool_arguments(self._options.contracts.question_args),
            ),
            StructuredTool.from_function(
                coroutine=submit_plan,
                name="submit_plan",
                description="Present a complete plan draft for human approval. This does not execute the plan.",
                args_schema=tool_arguments(self._options.contracts.draft_args),
            ),
            StructuredTool.from_function(
                coroutine=confirm_plan_edit,
                name="confirm_plan_edit",
                description="Confirm that the saved authoritative user edit is complete. Do not provide replacement content.",
                args_schema=tool_arguments(ConfirmArguments),
            ),
        ]
        argument_schemas = {
            "ask_user_question": self._options.contracts.question_args,
            "submit_plan": self._options.contracts.draft_args,
            "confirm_plan_edit": ConfirmArguments,
        }
        options = self._options
        known_tools = {
            tool.name for tool in (*plan_tools, *read_only_tools, *filesystem.tools)
        }

        middleware = [
            PlanLifecycle[ContextT](
                options,
                initialize=initialize_node,
                answer=answer_clarification,
                review=review_node,
                validate_state=lambda state: require_plan_schemas(state, options),
                clarifications=lambda plan: _clarification_context(plan, options),
                argument_schemas=argument_schemas,
                known_tools=known_tools,
            ),
            attachment_filesystem(filesystem, spec.attachments)
            if spec.attachments
            else filesystem,
            _ToolRuntimeMiddleware(spec.tool_scope),
            *([_AttachmentMiddleware(spec.attachments)] if spec.attachments else []),
        ]
        parent = create_agent(
            model=resolved_model,
            tools=[*read_only_tools, *plan_tools],
            middleware=middleware,
            state_schema=state_schema,
            context_schema=context_schema,
            checkpointer=_checkpoint_saver(spec.checkpointer),
            store=spec.store,
            name="tinkerfin_planning_workflow",
        )
        return PlanningWorkflowGraph[ContextT](cast(_CompiledPlanningRuntime, parent))


def create_planning_graph(
    spec: AgentSpec[ContextT],
    *,
    options: PlanOptions,
    workspace: PreparedWorkspace[object, BackendProtocol] | None = None,
) -> PlanningWorkflowGraph[ContextT]:
    """Build the read-only planning workflow from its declared agent resources."""
    return _PlanningGraphFactory[ContextT](options)._build(spec, workspace=workspace)


__all__ = ["PlanningWorkflowGraph", "create_planning_graph"]
