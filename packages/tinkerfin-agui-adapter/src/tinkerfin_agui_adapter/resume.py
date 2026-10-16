"""Pure mapping from AG-UI resume entries to native LangGraph resume data."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Never, cast

from ag_ui.core.types import Interrupt as AgUiInterrupt
from ag_ui.core.types import ResumeEntry
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import JsonValue, ValidationError

from ._json_schema import (
    SchemaError,
    validate_json_schema_instance,
)
from ._json_schema import (
    ValidationError as JsonSchemaValidationError,
)
from .errors import (
    AgUiAdapterError,
    AgUiAdapterErrorCode,
    HitlCorrelationError,
)
from .hitl import (
    HitlActionRequest,
    HitlRequest,
    match_hitl_tool_call_id_groups,
)
from .ids import ScopedIdCodec
from .models import (
    AgentRuntimeInterrupt,
    JsonObject,
)
from .reasoning import json_values_equal, normalize_operational_data
from .tool_review import _parse_tool_review_interrupt


class ResumeMappingError(AgUiAdapterError, ValueError):
    """Resume data cannot be translated without losing native semantics."""

    def __init__(
        self,
        code: AgUiAdapterErrorCode,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        """Initialize a stable resume failure with its original cause.

        Args:
            code: Machine-readable resume error category.
            message: Client-safe failure description.
            cause: Original validation or correlation failure for trusted logs.

        Raises:
            ValueError: ``code`` is not a resume error category.
        """

        if not code.name.startswith("RESUME_"):
            raise ValueError("code must identify an AG-UI resume failure")
        self.code = code
        super().__init__(message, cause=cause)


def _empty_decisions_by_interrupt() -> dict[
    str,
    tuple[dict[str, object] | None, ...],
]:
    """Create isolated decision storage for one translation."""

    return {}


@dataclass(frozen=True, slots=True)
class ResumeTranslation:
    """Lossless classification of resolved, abandoned, and mixed resume data.

    A `command` translation contains stock Deep Agents resume data; `abandon`
    contains only cancellations; `custom` preserves resolved decisions and
    cancelled slots without pretending stock Deep Agents can execute the mix.
    This value owns no graph, checkpointer, or I/O resource.
    """

    mode: Literal["command", "abandon", "custom"]
    kind: Literal["tool", "runtime"]
    resume_data: JsonObject | None
    cancelled_interrupt_ids: tuple[str, ...] = ()
    prior_tool_call_ids: tuple[str, ...] = ()
    source_agent_names: tuple[str, ...] = ()
    unidentified_external_source: bool = False
    decisions_by_interrupt: Mapping[
        str,
        tuple[dict[str, object] | None, ...],
    ] = field(default_factory=_empty_decisions_by_interrupt)

    @property
    def root(self) -> dict[str, JsonValue]:
        """Return stock Deep Agents resume data for a fully resolved translation."""

        if self.resume_data is None:
            raise RuntimeError("abandoned resume does not contain command data")
        return self.resume_data.root


@dataclass(frozen=True, slots=True)
class _PendingInterruptAction:
    """One review action extracted from a runtime interrupt."""

    ag_ui_interrupt_id: str
    interrupt_id: str
    action_index: int
    action_name: str
    allowed_decisions: tuple[Literal["approve", "edit", "reject", "respond"], ...]
    args_schema: dict[str, JsonValue] | None


_PriorToolCallIdResolver = Callable[
    [Mapping[str, Sequence[bool]] | None],
    tuple[str, ...],
]


class ResumeMapper:
    """Translate complete AG-UI resume coverage without performing I/O.

    The mapper restores native interrupt grouping and action order, validates
    allowed decisions, and preserves `cancelled` as abandonment rather than a
    fabricated rejection. It neither queries a checkpointer nor constructs,
    invokes, or owns a graph command; the host decides how to execute the returned
    `ResumeTranslation`.
    """

    def map(
        self,
        *,
        entries: Sequence[ResumeEntry],
        interrupts: Sequence[AgentRuntimeInterrupt],
        messages_by_graph_namespace: Mapping[tuple[str, ...], Sequence[BaseMessage]]
        | None = None,
        interrupt_graph_namespaces: Mapping[str, tuple[str, ...]] | None = None,
    ) -> ResumeTranslation:
        """Classify AG-UI resume entries as command, abandonment, or custom data.

        Args:
            entries: Resume entries already validated by the AG-UI schema.
            interrupts: Pending interrupts supplied from a host checkpoint snapshot.
            messages_by_graph_namespace: Complete checkpoint messages grouped by their
                full graph namespace. Required when at least one review is resolved;
                an all-cancelled abandonment does not need Tool-call correlation.
            interrupt_graph_namespaces: Trusted Graph location of each native
                interrupt. Supply checkpoint evidence to distinguish identical
                actions in separate Graphs; never use locations from client input.

        Returns:
            A lossless translation preserving grouping and native action order.

        Raises:
            ResumeMappingError: Coverage is incomplete, IDs are invalid, checkpoint
                correlation evidence is missing or ambiguous, or a decision is not
                allowed.
        """

        from .runtime_resume import (
            classify_native_runtime_interrupts,
            translate_native_runtime_resume,
        )

        runtime_kind = classify_native_runtime_interrupts(interrupts)
        if runtime_kind == "mixed":
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                "runtime and Tool interrupts cannot share one resume batch",
            )
        if runtime_kind == "runtime":
            return translate_native_runtime_resume(
                entries=entries,
                interrupts=interrupts,
            )

        pending, action_groups = self._pending_actions(interrupts)
        group_ids = tuple(
            dict.fromkeys(action.interrupt_id for action in pending.values())
        )

        def resolve_prior_tool_call_ids(
            selected: Mapping[str, Sequence[bool]] | None,
        ) -> tuple[str, ...]:
            selected_slots = (
                None
                if selected is None
                else tuple(tuple(selected[group_id]) for group_id in group_ids)
            )
            if interrupt_graph_namespaces is not None:
                if set(interrupt_graph_namespaces) != set(group_ids):
                    raise ResumeMappingError(
                        AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                        "interrupt Graph locations must cover the pending review groups",
                    )
                groups_by_namespace: dict[tuple[str, ...], list[int]] = {}
                for index, group_id in enumerate(group_ids):
                    namespace = interrupt_graph_namespaces[group_id]
                    groups_by_namespace.setdefault(namespace, []).append(index)
                matched: list[str] = []
                for namespace, indices in groups_by_namespace.items():
                    if (
                        messages_by_graph_namespace is None
                        or namespace not in messages_by_graph_namespace
                    ):
                        raise ResumeMappingError(
                            AgUiAdapterErrorCode.RESUME_CHECKPOINT_MESSAGES_REQUIRED,
                            "each interrupted Graph must provide its checkpoint messages",
                        )
                    matched.extend(
                        self._prior_tool_call_ids(
                            [action_groups[index] for index in indices],
                            {namespace: messages_by_graph_namespace[namespace]},
                            selected_slots=(
                                None
                                if selected_slots is None
                                else [selected_slots[index] for index in indices]
                            ),
                        )
                    )
                return tuple(matched)
            return self._prior_tool_call_ids(
                action_groups,
                messages_by_graph_namespace,
                selected_slots=selected_slots,
            )

        return self._translate(
            entries=entries,
            pending=pending,
            resolve_prior_tool_call_ids=resolve_prior_tool_call_ids,
        )

    def map_agui(
        self,
        *,
        entries: Sequence[ResumeEntry],
        interrupts: Sequence[AgUiInterrupt],
    ) -> ResumeTranslation:
        """Translate resume entries from trusted, previously emitted interrupts.

        Args:
            entries: Resume entries already validated at the AG-UI request boundary.
            interrupts: Complete AG-UI interrupts persisted by the host from a prior
                run terminal. Client-supplied interrupt payloads are not trustworthy
                correlation evidence and must not be passed here.

        Returns:
            A lossless translation with Tool IDs already correlated at emission time.

        Raises:
            ResumeMappingError: Interrupt correlation is incomplete, inconsistent, or
                cannot be validated without weakening native resume semantics.
        """

        from .runtime_resume import (
            classify_ag_ui_runtime_interrupts,
            translate_ag_ui_runtime_resume,
        )

        runtime_kind = classify_ag_ui_runtime_interrupts(interrupts)
        if runtime_kind == "mixed":
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                "runtime and Tool interrupts cannot share one resume batch",
            )
        if runtime_kind == "runtime":
            return translate_ag_ui_runtime_resume(
                entries=entries,
                interrupts=interrupts,
            )

        pending, tool_ids_by_group = self._pending_agui_actions(interrupts)
        source_agent_names, unidentified_external_source = self._tool_sources(
            interrupts
        )

        def resolve_prior_tool_call_ids(
            selected: Mapping[str, Sequence[bool]] | None,
        ) -> tuple[str, ...]:
            resolved: list[str] = []
            for group_id, tool_ids in tool_ids_by_group.items():
                selected_group = (
                    tuple(True for _tool_id in tool_ids)
                    if selected is None
                    else tuple(selected[group_id])
                )
                resolved.extend(
                    tool_id
                    for tool_id, include in zip(
                        tool_ids,
                        selected_group,
                        strict=True,
                    )
                    if include
                )
            return tuple(resolved)

        return self._translate(
            entries=entries,
            pending=pending,
            resolve_prior_tool_call_ids=resolve_prior_tool_call_ids,
            source_agent_names=source_agent_names,
            unidentified_external_source=unidentified_external_source,
            include_cancelled_tool_ids=True,
        )

    @staticmethod
    def _tool_sources(
        interrupts: Sequence[AgUiInterrupt],
    ) -> tuple[tuple[str, ...], bool]:
        """Read trusted subagent provenance needed by custom execution policy."""

        names: list[str] = []
        unidentified = False
        for interrupt in interrupts:
            metadata = interrupt.metadata
            if not isinstance(metadata, Mapping):
                continue
            source = cast(Mapping[object, object], metadata).get("source")
            if not isinstance(source, Mapping):
                continue
            source_mapping = cast(Mapping[object, object], source)
            agent_type = source_mapping.get("agentType")
            agent_name = source_mapping.get("agentName")
            kind = source_mapping.get("kind")
            if agent_type == "subagent":
                if not isinstance(agent_name, str) or not agent_name:
                    raise ResumeMappingError(
                        AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                        "subagent Tool interrupt has no stable agent name",
                    )
                if agent_name not in names:
                    names.append(agent_name)
            elif kind == "compiled_subgraph":
                unidentified = True
        return tuple(names), unidentified

    def _translate(
        self,
        *,
        entries: Sequence[ResumeEntry],
        pending: Mapping[str, _PendingInterruptAction],
        resolve_prior_tool_call_ids: _PriorToolCallIdResolver,
        source_agent_names: tuple[str, ...] = (),
        unidentified_external_source: bool = False,
        include_cancelled_tool_ids: bool = False,
    ) -> ResumeTranslation:
        if not pending:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_NO_PENDING_INTERRUPT,
                "the thread has no pending review to resume",
            )

        expected_ids = set(pending)
        received_ids: set[str] = set()
        grouped_counts: dict[str, int] = {}
        grouped_decisions: dict[str, list[dict[str, object] | None]] = {}
        cancelled_interrupt_ids: list[str] = []
        for action in pending.values():
            grouped_counts[action.interrupt_id] = (
                grouped_counts.get(action.interrupt_id, 0) + 1
            )
        for interrupt_id, count in grouped_counts.items():
            grouped_decisions[interrupt_id] = [None] * count

        for entry in entries:
            interrupt_id = entry.interrupt_id
            if interrupt_id in received_ids:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_DUPLICATE_INTERRUPT_ID,
                    f"resume contains duplicate interruptId: {interrupt_id}",
                )
            if interrupt_id not in pending:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_UNKNOWN_INTERRUPT_ID,
                    f"resume contains unknown interruptId: {interrupt_id}",
                )
            received_ids.add(interrupt_id)
            pending_action = pending[interrupt_id]
            if entry.status == "cancelled":
                if entry.payload is not None:
                    raise ResumeMappingError(
                        AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID,
                        f"cancelled interruptId={entry.interrupt_id} cannot include payload",
                    )
                cancelled_interrupt_ids.append(entry.interrupt_id)
            else:
                grouped_decisions[pending_action.interrupt_id][
                    pending_action.action_index
                ] = self._decision(entry, pending_action)

        missing_ids = sorted(expected_ids - received_ids)
        if missing_ids:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_INCOMPLETE,
                "resume must cover every pending review; missing: "
                f"{', '.join(missing_ids)}",
            )

        if cancelled_interrupt_ids and len(cancelled_interrupt_ids) == len(pending):
            return ResumeTranslation(
                mode="abandon",
                kind="tool",
                resume_data=None,
                cancelled_interrupt_ids=tuple(cancelled_interrupt_ids),
                source_agent_names=source_agent_names,
                unidentified_external_source=unidentified_external_source,
                decisions_by_interrupt={
                    interrupt_id: tuple(decisions)
                    for interrupt_id, decisions in grouped_decisions.items()
                },
            )

        if cancelled_interrupt_ids:
            selected = (
                None
                if include_cancelled_tool_ids
                else {
                    interrupt_id: tuple(decision is not None for decision in decisions)
                    for interrupt_id, decisions in grouped_decisions.items()
                }
            )
            prior_tool_call_ids = resolve_prior_tool_call_ids(selected)
            return ResumeTranslation(
                mode="custom",
                kind="tool",
                resume_data=None,
                cancelled_interrupt_ids=tuple(cancelled_interrupt_ids),
                prior_tool_call_ids=prior_tool_call_ids,
                source_agent_names=source_agent_names,
                unidentified_external_source=unidentified_external_source,
                decisions_by_interrupt={
                    interrupt_id: tuple(decisions)
                    for interrupt_id, decisions in grouped_decisions.items()
                },
            )

        serialized_groups: dict[str, dict[str, object]] = {}
        for interrupt_id, decisions in grouped_decisions.items():
            if any(decision is None for decision in decisions):
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_INCOMPLETE,
                    f"interruptId={interrupt_id} has incomplete review decisions",
                )
            serialized_groups[interrupt_id] = {
                "decisions": [
                    decision for decision in decisions if decision is not None
                ]
            }

        if len(serialized_groups) == 1:
            resume_data = JsonObject.model_validate(
                next(iter(serialized_groups.values()))
            )
        else:
            resume_data = JsonObject.model_validate(serialized_groups)
        prior_tool_call_ids = resolve_prior_tool_call_ids(None)
        return ResumeTranslation(
            mode="command",
            kind="tool",
            resume_data=resume_data,
            prior_tool_call_ids=prior_tool_call_ids,
            source_agent_names=source_agent_names,
            unidentified_external_source=unidentified_external_source,
            decisions_by_interrupt={
                interrupt_id: tuple(decisions)
                for interrupt_id, decisions in grouped_decisions.items()
            },
        )

    @classmethod
    def _pending_agui_actions(
        cls,
        interrupts: Sequence[AgUiInterrupt],
    ) -> tuple[
        dict[str, _PendingInterruptAction],
        dict[str, tuple[str, ...]],
    ]:
        pending: dict[str, _PendingInterruptAction] = {}
        groups: dict[
            str,
            tuple[HitlRequest, list[str | None]],
        ] = {}
        seen_tool_ids: set[str] = set()
        codec = ScopedIdCodec()

        for interrupt in interrupts:
            try:
                parsed = _parse_tool_review_interrupt(interrupt)
                request = parsed.request
                correlation = parsed.metadata
                tool_call_id = interrupt.tool_call_id
                assert isinstance(tool_call_id, str)
                if tool_call_id in seen_tool_ids:
                    raise ValueError("interrupts reuse a scoped Tool call ID")
                kind, _namespace, _raw_id = codec.decode(tool_call_id)
                assert kind == "tool"
                native_id = correlation.native_interrupt_id
                action_index = correlation.action_index
                action = request.action_requests[action_index]
                review = request.review_configs[action_index]
            except (TypeError, ValueError, ValidationError) as error:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                    f"persisted AG-UI interrupt is not resumable: {interrupt.id}",
                    cause=error,
                ) from error

            existing = groups.get(native_id)
            if existing is None:
                slots: list[str | None] = [None] * len(request.action_requests)
                groups[native_id] = (request, slots)
            else:
                existing_request, slots = existing
                if not json_values_equal(
                    existing_request.model_dump(mode="json", by_alias=True),
                    request.model_dump(mode="json", by_alias=True),
                ):
                    raise ResumeMappingError(
                        AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                        f"persisted AG-UI interrupt group is inconsistent: {native_id}",
                    )
            if slots[action_index] is not None:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_DUPLICATE_PENDING_INTERRUPT_ID,
                    f"duplicate pending interruptId: {interrupt.id}",
                )
            if interrupt.id in pending:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_DUPLICATE_PENDING_INTERRUPT_ID,
                    f"duplicate pending interruptId: {interrupt.id}",
                )
            slots[action_index] = tool_call_id
            seen_tool_ids.add(tool_call_id)
            pending[interrupt.id] = _PendingInterruptAction(
                ag_ui_interrupt_id=interrupt.id,
                interrupt_id=native_id,
                action_index=action_index,
                action_name=action.name,
                allowed_decisions=tuple(review.allowed_decisions),
                args_schema=(
                    None
                    if review.args_schema is None
                    else dict(review.args_schema.root)
                ),
            )

        ordered_pending: dict[str, _PendingInterruptAction] = {}
        tool_ids_by_group: dict[str, tuple[str, ...]] = {}
        for native_id, (request, slots) in groups.items():
            if any(tool_id is None for tool_id in slots):
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_INCOMPLETE,
                    f"persisted AG-UI interrupt group is incomplete: {native_id}",
                )
            multi_action = len(request.action_requests) > 1
            for index in range(len(slots)):
                public_id = f"{native_id}#{index}" if multi_action else native_id
                ordered_pending[public_id] = pending[public_id]
            tool_ids_by_group[native_id] = tuple(
                cast(str, tool_id) for tool_id in slots
            )
        return ordered_pending, tool_ids_by_group

    @staticmethod
    def _prior_tool_call_ids(
        action_groups: Sequence[Sequence[HitlActionRequest]],
        messages_by_graph_namespace: Mapping[tuple[str, ...], Sequence[BaseMessage]]
        | None,
        *,
        selected_slots: Sequence[Sequence[bool]] | None = None,
    ) -> tuple[str, ...]:
        """Correlate complete action groups, then return the selected Tool call IDs."""

        if messages_by_graph_namespace is None:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_CHECKPOINT_MESSAGES_REQUIRED,
                "resolved Tool reviews require checkpoint messages grouped by "
                "their full graph namespace",
            )
        codec = ScopedIdCodec()
        scoped_messages: list[BaseMessage] = []
        try:
            for namespace, messages in messages_by_graph_namespace.items():
                if not isinstance(namespace, tuple):
                    raise TypeError("message namespace must be a tuple")
                for message in messages:
                    if not isinstance(message, BaseMessage):
                        raise TypeError(
                            "checkpoint messages must contain LangChain messages"
                        )
                    if isinstance(message, ToolMessage):
                        scoped_messages.append(
                            message.model_copy(
                                update={
                                    "tool_call_id": codec.encode(
                                        "tool", namespace, message.tool_call_id
                                    )
                                }
                            )
                        )
                        continue
                    if not isinstance(message, AIMessage):
                        scoped_messages.append(message)
                        continue
                    tool_calls = [
                        {
                            **call,
                            "id": codec.encode(
                                "tool",
                                namespace,
                                str(call.get("id") or ""),
                            ),
                        }
                        for call in message.tool_calls
                    ]
                    scoped_messages.append(
                        message.model_copy(update={"tool_calls": tool_calls})
                    )
        except (TypeError, ValueError) as error:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                "checkpoint messages do not have valid graph namespaces or Tool IDs",
                cause=error,
            ) from error
        try:
            matched_groups = match_hitl_tool_call_id_groups(
                action_groups,
                scoped_messages,
                selected_slots=selected_slots,
            )
        except HitlCorrelationError as error:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                "checkpoint review actions cannot be correlated to unique Tool calls",
                cause=error,
            ) from error
        return tuple(call_id for group in matched_groups for call_id in group)

    @staticmethod
    def _pending_actions(
        interrupts: Sequence[AgentRuntimeInterrupt],
    ) -> tuple[
        dict[str, _PendingInterruptAction],
        list[tuple[HitlActionRequest, ...]],
    ]:
        pending: dict[str, _PendingInterruptAction] = {}
        action_groups: list[tuple[HitlActionRequest, ...]] = []
        for interrupt in interrupts:
            try:
                request = HitlRequest.model_validate(interrupt.value)
            except ValidationError as exc:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                    "the thread contains an unsupported interrupt type",
                    cause=exc,
                ) from exc

            action_groups.append(tuple(request.action_requests))
            multi_action = len(request.action_requests) > 1
            for index, (action, review) in enumerate(
                zip(
                    request.action_requests,
                    request.review_configs,
                    strict=True,
                )
            ):
                ag_ui_interrupt_id = (
                    f"{interrupt.id}#{index}" if multi_action else interrupt.id
                )
                if ag_ui_interrupt_id in pending:
                    raise ResumeMappingError(
                        AgUiAdapterErrorCode.RESUME_DUPLICATE_PENDING_INTERRUPT_ID,
                        f"duplicate pending interruptId: {ag_ui_interrupt_id}",
                    )
                pending[ag_ui_interrupt_id] = _PendingInterruptAction(
                    ag_ui_interrupt_id=ag_ui_interrupt_id,
                    interrupt_id=interrupt.id,
                    action_index=index,
                    action_name=action.name,
                    allowed_decisions=tuple(review.allowed_decisions),
                    args_schema=(
                        None
                        if review.args_schema is None
                        else dict(review.args_schema.root)
                    ),
                )
        return pending, action_groups

    @staticmethod
    def _decision(
        entry: ResumeEntry,
        pending: _PendingInterruptAction,
    ) -> dict[str, object]:
        raw_payload = cast(object, entry.payload)
        if raw_payload is None:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_PAYLOAD_REQUIRED,
                f"interruptId={entry.interrupt_id} requires a payload",
            )
        if not isinstance(raw_payload, Mapping):
            ResumeMapper._raise_invalid_payload(entry.interrupt_id)
        payload = dict(cast(Mapping[object, object], raw_payload))
        decision_type = payload.get("type")
        if decision_type not in {"approve", "edit", "reject", "respond"}:
            ResumeMapper._raise_invalid_payload(entry.interrupt_id)
        decision = cast(
            Literal["approve", "edit", "reject", "respond"],
            decision_type,
        )
        ResumeMapper._require_decision(
            entry.interrupt_id,
            pending,
            decision,
        )

        if decision == "approve":
            if set(payload) != {"type"}:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            return {"type": "approve"}

        if decision == "edit":
            if set(payload) != {"type", "edited_action"}:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            edited_action = payload.get("edited_action")
            if not isinstance(edited_action, Mapping):
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            edited_mapping = cast(Mapping[object, object], edited_action)
            if set(edited_mapping) != {"name", "args"}:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            name = edited_mapping.get("name")
            args = edited_mapping.get("args")
            if not isinstance(name, str) or not name or not isinstance(args, Mapping):
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            args_mapping = cast(Mapping[object, object], args)
            # Deep Agents does not re-run review policy after an edit, so the Tool
            # identity is fixed and only its arguments may change.
            if name != pending.action_name:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            try:
                normalized = normalize_operational_data(args_mapping)
                normalized_args = JsonObject.model_validate(normalized).root
                if pending.args_schema is not None:
                    validate_json_schema_instance(
                        normalized_args,
                        pending.args_schema,
                    )
            except (
                TypeError,
                ValueError,
                SchemaError,
                JsonSchemaValidationError,
            ) as error:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID,
                    f"interruptId={entry.interrupt_id} has an invalid payload",
                    cause=error,
                ) from error
            return {
                "type": "edit",
                "edited_action": {"name": name, "args": normalized_args},
            }

        if decision == "reject":
            if not set(payload) <= {"type", "message"}:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            if "message" not in payload:
                return {"type": "reject"}
            message = payload["message"]
            if not isinstance(message, str):
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            return {"type": "reject", "message": message}

        if set(payload) != {"type", "message"}:
            ResumeMapper._raise_invalid_payload(entry.interrupt_id)
        message = payload.get("message")
        if not isinstance(message, str):
            ResumeMapper._raise_invalid_payload(entry.interrupt_id)
        return {"type": "respond", "message": message}

    @staticmethod
    def _raise_invalid_payload(interrupt_id: str) -> Never:
        raise ResumeMappingError(
            AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID,
            f"interruptId={interrupt_id} has an invalid payload",
        )

    @staticmethod
    def _require_decision(
        interrupt_id: str,
        pending: _PendingInterruptAction,
        decision: Literal["approve", "edit", "reject", "respond"],
    ) -> None:
        if decision in pending.allowed_decisions:
            return
        raise ResumeMappingError(
            AgUiAdapterErrorCode.RESUME_DECISION_NOT_ALLOWED,
            f"interruptId={interrupt_id} does not allow {decision}",
        )
