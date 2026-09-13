"""Stateful conversion of Deep Agents v2 stream parts to AG-UI events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypeAlias, cast

from ag_ui.core import BaseEvent
from ag_ui.core import Interrupt as AgUiInterrupt
from ag_ui.core.types import Message
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    ToolCallChunk,
    ToolMessage,
)
from pydantic import JsonValue, ValidationError

from tinkerfin_native_stream import (
    NativeExtraStreamPart,
    NativeMessageStreamPart,
    NativeStreamContractError,
    NativeStreamFrame,
    NativeTaskResultPayload,
    NativeTasksStreamPart,
    NativeTaskStartPayload,
    NativeUpdatesStreamPart,
    NativeValidatedStreamPart,
    NativeValuesStreamPart,
    validate_native_stream_part,
)

from . import _adapter_contracts, _adapter_messages, _adapter_tasks
from ._adapter_contracts import (
    ActiveReasoning,
    ActiveToolCall,
    AgentSource,
    BufferedChildInterrupt,
    GraphScope,
    NativeToolCall,
    StreamMode,
    SubagentInvocation,
    TaskResultFingerprint,
    TaskStartFingerprint,
    ToolResultFingerprint,
)
from ._adapter_messages import _complete_ai_message_to_chunk
from .contracts import AgentRunOutcome, RunIdentity
from .errors import (
    AgUiAdapterError,
    AgUiStreamContractError,
    HitlCorrelationError,
)
from .hitl import HitlActionRequest
from .ids import ScopedIdCodec
from .models import AgentRuntimeInterrupt

ValidatedDeepAgentStreamPart: TypeAlias = NativeValidatedStreamPart
ValidatedExtraStreamPart: TypeAlias = NativeExtraStreamPart
ValidatedMessageStreamPart: TypeAlias = NativeMessageStreamPart
ValidatedTaskResultPayload: TypeAlias = NativeTaskResultPayload
ValidatedTaskStartPayload: TypeAlias = NativeTaskStartPayload
ValidatedTasksStreamPart: TypeAlias = NativeTasksStreamPart
ValidatedUpdatesStreamPart: TypeAlias = NativeUpdatesStreamPart
ValidatedValuesStreamPart: TypeAlias = NativeValuesStreamPart

ExtraStreamPart: TypeAlias = NativeExtraStreamPart
MessageStreamPart: TypeAlias = NativeMessageStreamPart
TaskResultPayload: TypeAlias = NativeTaskResultPayload
TasksStreamPart: TypeAlias = NativeTasksStreamPart
TaskStartPayload: TypeAlias = NativeTaskStartPayload
UpdatesStreamPart: TypeAlias = NativeUpdatesStreamPart
ValuesStreamPart: TypeAlias = NativeValuesStreamPart


def _stream_contract_error(part: object, error: Exception) -> AgUiStreamContractError:
    """Translate one structural or correlation failure without exposing payloads."""

    context: dict[str, str] = {}
    mode: object | None = None
    if isinstance(part, Mapping):
        mode = cast(Mapping[object, object], part).get("type")
    else:
        mode = getattr(part, "type", None)
    if isinstance(mode, str):
        context["mode"] = mode
    if isinstance(error, ValidationError):
        details = error.errors(include_input=False)
        message = (
            str(details[0]["msg"])
            if details
            else "Deep Agents StreamPart validation failed"
        )
    else:
        message = str(error)
    return AgUiStreamContractError(
        message,
        context=context,
        cause=error,
    )


def validate_deep_agent_stream_part(part: object) -> ValidatedDeepAgentStreamPart:
    """Validate one live v2 StreamPart before any consumer mutates state.

    This standalone boundary uses the authoritative
    ``tinkerfin_native_stream.validate_native_stream_part`` symbol also consumed by
    ``DeepAgentsV2StreamDriver``. TinkerFin Runtime integrations instead call
    :meth:`DeepAgentAgUiAdapter.process_frame` so the Adapter never validates a second
    time. The distinction keeps direct v2 conversion useful without making the generic
    frame path depend on v2.

    Args:
        part: Live LangGraph v2 envelope.

    Returns:
        The immutable mode-specific validated envelope.

    Raises:
        AgUiStreamContractError: The envelope or mode payload is malformed.
    """

    try:
        return validate_native_stream_part(part)
    except AgUiAdapterError:
        raise
    except NativeStreamContractError as error:
        translated = AgUiStreamContractError(
            error.message,
            context=error.context,
            cause=error,
        )
        raise translated from error
    except (TypeError, ValueError, ValidationError) as error:
        translated = _stream_contract_error(part, error)
        raise translated from error


class DeepAgentAgUiAdapter:
    """Convert individual Deep Agents v2 stream parts into AG-UI events.

    Each instance retains one caller-declared immutable `RunIdentity` and preserves
    event order and full namespace-scoped identifiers across `messages`, `tasks`,
    and `values` parts. It reads the run ID only when constructing protocol output.
    It supports ordinary compiled subgraphs as native graph scopes and enriches
    only verified Deep Agents `task` delegates with subagent identity, input, and
    parent Tool provenance.

    `process()` validates a complete part before mutating correlation state and
    propagates validation or correlation errors unchanged. `finish()` and
    `abort()` close open reasoning, text, and Tool lifecycles idempotently, but
    the caller owns the main `RUN_STARTED` and terminal events.

    The adapter owns no graph, checkpointer, transport, or server resource. The
    verified private provider metadata path is
    removed from public payloads regardless of `expose_reasoning_events`; that flag
    controls only emission from the adapter's supported reasoning sources and is
    not permission to expose raw provider payloads.
    """

    def __init__(
        self,
        *,
        identity: RunIdentity,
        prior_tool_call_ids: frozenset[str] = frozenset(),
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        private_state_keys: frozenset[str] = frozenset(),
    ) -> None:
        """Initialize request-scoped correlation without opening runtime resources.

        Args:
            identity: Public thread and run identity for emitted child events.
            prior_tool_call_ids: Scoped Tool IDs already emitted before a resume.
            expose_reasoning_events: Whether verified reasoning paths emit events.
            expose_subagent_events: Whether validated non-root events are emitted.
            private_state_keys: Top-level runtime channels excluded from all output.

        Raises:
            TypeError: An option has the wrong container or scalar type.
            ValueError: A private key or prior Tool ID is not canonical.
        """

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        if not isinstance(expose_reasoning_events, bool):
            raise TypeError("expose_reasoning_events must be a bool")
        if not isinstance(expose_subagent_events, bool):
            raise TypeError("expose_subagent_events must be a bool")
        if not isinstance(private_state_keys, frozenset):
            raise TypeError("private_state_keys must be a frozenset")
        if any(
            not isinstance(key, str) or not key or key != key.strip()
            for key in private_state_keys
        ):
            raise ValueError(
                "private_state_keys must contain canonical non-empty strings"
            )
        self._identity = identity
        self._expose_reasoning_events = expose_reasoning_events
        self._expose_subagent_events = expose_subagent_events
        self._private_state_keys = private_state_keys
        self._ids = ScopedIdCodec()
        self._active_messages: dict[tuple[str, ...], str] = {}
        self._active_reasoning: dict[tuple[str, str], ActiveReasoning] = {}
        self._active_tools: dict[str, ActiveToolCall] = {}
        self._tool_history: dict[tuple[tuple[str, ...], str], list[ActiveToolCall]] = {}
        self._native_tool_calls: dict[
            tuple[tuple[str, ...], str], list[NativeToolCall]
        ] = {}
        self._tool_ids_by_index: dict[tuple[tuple[str, ...], str, int | None], str] = {}
        self._tool_id_history_by_index: dict[
            tuple[tuple[str, ...], str, int | None], str
        ] = {}
        self._tool_names_by_id: dict[str, str] = {}
        self._prior_tool_call_ids: set[str] = set()
        self._started_tool_ids: set[str] = set()
        self._ended_tool_ids: set[str] = set()
        self._result_fingerprints: dict[str, ToolResultFingerprint] = {}
        if not isinstance(prior_tool_call_ids, frozenset):
            raise TypeError("prior_tool_call_ids must be a frozenset")
        for event_id in prior_tool_call_ids:
            try:
                kind, _namespace, _raw_tool_call_id = self._ids.decode(event_id)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "prior_tool_call_ids must contain complete scoped Tool IDs"
                ) from error
            if kind != "tool":
                raise ValueError(
                    "prior_tool_call_ids must contain complete scoped Tool IDs"
                )
            # The interrupt terminal already closed these proposals. Resume may emit
            # results only; unknown calls still receive a complete synthesized lifecycle.
            self._prior_tool_call_ids.add(event_id)
            self._started_tool_ids.add(event_id)
            self._ended_tool_ids.add(event_id)
        self._namespace_agent_names: dict[tuple[str, ...], str] = {}
        self._graph_scopes: dict[tuple[str, ...], GraphScope] = {}
        self._subagent_invocations: dict[tuple[str, ...], SubagentInvocation] = {}
        self._sub_namespaces_by_parent_tool_call: dict[
            tuple[tuple[str, ...], str], tuple[str, ...]
        ] = {}
        self._task_start_fingerprints: dict[
            tuple[tuple[str, ...], str], TaskStartFingerprint
        ] = {}
        self._task_result_fingerprints: dict[
            tuple[tuple[str, ...], str], TaskResultFingerprint
        ] = {}
        self._previous_root_state: dict[str, JsonValue] | None = None
        self._message_baseline_namespaces: set[tuple[str, ...]] = set()
        self._message_baselines: dict[tuple[tuple[str, ...], str], bytes] = {}
        self._baseline_tool_parent_ids: dict[str, str] = {}
        self._child_interrupts: dict[str, BufferedChildInterrupt] = {}
        self._resolved_child_interrupt_ids: set[str] = set()
        self._child_interrupt_ids_by_namespace: dict[
            tuple[str, ...], tuple[str, ...]
        ] = {}
        self._child_message_snapshots: dict[tuple[str, ...], tuple[Message, ...]] = {}
        self._interrupts_in_order: list[AgUiInterrupt] = []
        self._interrupts_by_id: dict[str, AgUiInterrupt] = {}

    def process(self, part: object) -> list[BaseEvent]:
        """Validate and convert one standalone Deep Agents v2 part.

        Returns events in protocol order. Validation and correlation errors are
        raised before this part changes lifecycle state. Runtime integrations that
        already own a Profile must use :meth:`process_frame` instead.

        Args:
            part: One complete live v2 StreamPart object from the standalone source.

        Returns:
            Visible AG-UI events in lifecycle order.

        Raises:
            AgUiStreamContractError: The v2 envelope is structurally invalid.
            AgUiAdapterError: Conversion or full-ID correlation fails.
        """

        return self.process_validated(validate_deep_agent_stream_part(part))

    def process_frame(self, frame: NativeStreamFrame) -> list[BaseEvent]:
        """Convert one Driver-owned canonical frame without parsing its source again.

        Args:
            frame: Exact normalization result produced for the upstream part.

        Returns:
            AG-UI events emitted in protocol order.

        Raises:
            TypeError: ``frame`` is not a canonical ``NativeStreamFrame``.
            AgUiAdapterError: Canonical correlation or conversion fails.
        """

        if not isinstance(frame, NativeStreamFrame):
            raise TypeError("frame must be a NativeStreamFrame")
        return self.process_validated(frame.canonical)

    def process_validated(
        self,
        part: ValidatedDeepAgentStreamPart,
    ) -> list[BaseEvent]:
        """Convert one structurally validated part without repeating validation.

        Correlation checks still run before this part mutates adapter state. Callers
        must obtain ``part`` from :func:`validate_deep_agent_stream_part`.

        Args:
            part: Structurally validated live Native part.

        Returns:
            Visible AG-UI events in lifecycle order.

        Raises:
            AgUiAdapterError: Conversion, privacy normalization, or correlation fails.
            TypeError: The validated model contains an unsupported live-object shape.
        """

        try:
            validated = part
            if isinstance(validated, MessageStreamPart):
                self._validate_message_part(validated)
                events = self._process_message_part(validated)
            elif isinstance(validated, TasksStreamPart):
                events = self._process_tasks_part(validated)
            elif isinstance(validated, ValuesStreamPart):
                events = self._process_values_part(validated)
            else:
                events = self._process_extra_part(validated)
            return self._visible_events(events)
        except AgUiAdapterError:
            raise
        except (TypeError, ValueError, ValidationError) as error:
            translated = _stream_contract_error(part, error)
            raise translated from error

    def _visible_events(self, events: list[BaseEvent]) -> list[BaseEvent]:
        """Suppress every event whose serialized provenance is a subgraph."""

        if self._expose_subagent_events:
            return events
        visible: list[BaseEvent] = []
        for event in events:
            raw_event = event.raw_event
            if isinstance(raw_event, Mapping):
                raw_mapping = cast(Mapping[object, object], raw_event)
                source = raw_mapping.get("source")
                if isinstance(source, Mapping) and cast(
                    Mapping[object, object], source
                ).get("graphNamespace"):
                    continue
                namespace = raw_mapping.get("ns")
                if (
                    isinstance(namespace, Sequence)
                    and not isinstance(namespace, (str, bytes, bytearray))
                    and namespace
                ):
                    continue
            visible.append(event)
        return visible

    def _validate_message_part(self, part: MessageStreamPart) -> None:
        """Validate stable message and Tool-fragment IDs before mutating state."""

        message = part.data.message
        if isinstance(message, ToolMessage):
            return
        if not isinstance(message, AIMessage):
            return
        raw_message_id = self._stable_message_id(message)
        chunk = (
            message
            if isinstance(message, AIMessageChunk)
            else _complete_ai_message_to_chunk(message)
        )
        available_slots = dict(self._tool_id_history_by_index)
        slot_by_tool_id = {
            tool_call_id: index_key
            for index_key, tool_call_id in available_slots.items()
        }
        tool_names = dict(self._tool_names_by_id)
        parent_message_id = self._message_id(part.ns, raw_message_id)
        # Checkpoint proposals retain their identity across resume. A different
        # assistant message must not reuse that identity for a new proposal.
        for tool_chunk in chunk.tool_call_chunks:
            raw_id = tool_chunk.get("id")
            if isinstance(raw_id, str) and raw_id:
                tool_id = self._tool_call_id(part.ns, raw_id)
                prior_parent = self._baseline_tool_parent_ids.get(tool_id)
                if prior_parent is not None and prior_parent != parent_message_id:
                    raise ValueError(
                        "new Tool proposals require unique IDs within their graph scope"
                    )
        for tool_chunk in chunk.tool_call_chunks:
            index = tool_chunk.get("index")
            if type(index) is not int:
                raise ValueError("tool-call fragments require an integer index")
            index_key = (part.ns, parent_message_id, index)
            raw_tool_call_id = tool_chunk.get("id")
            tool_name = tool_chunk.get("name")
            if raw_tool_call_id is not None or tool_name is not None:
                if (
                    not isinstance(raw_tool_call_id, str)
                    or not raw_tool_call_id
                    or not isinstance(tool_name, str)
                    or not tool_name
                ):
                    raise ValueError("tool-call starts require both id and name")
                tool_call_id = self._tool_call_id(part.ns, raw_tool_call_id)
                if (
                    tool_call_id in self._ended_tool_ids
                    and tool_call_id not in self._prior_tool_call_ids
                ):
                    raise ValueError("an in-run ended tool-call ID cannot start again")
                previous_slot = slot_by_tool_id.get(tool_call_id)
                if previous_slot is not None and previous_slot != index_key:
                    raise ValueError("tool-call IDs cannot span multiple indices")
                previous_id = available_slots.get(index_key)
                if previous_id is not None and previous_id != tool_call_id:
                    raise ValueError("tool-call indices cannot change IDs")
                previous_name = tool_names.get(tool_call_id)
                if previous_name is not None and previous_name != tool_name:
                    raise ValueError("tool-call IDs cannot change names")
                tool_names[tool_call_id] = tool_name
                if tool_call_id not in self._ended_tool_ids:
                    available_slots[index_key] = tool_call_id
                    slot_by_tool_id[tool_call_id] = index_key
            elif index_key not in available_slots:
                raise ValueError("tool-call args fragment has no scoped start")
            args = tool_chunk.get("args")
            if args is not None and not isinstance(args, str):
                raise TypeError("tool-call args fragments must be strings")

    def finish(self) -> list[BaseEvent]:
        """Close open child lifecycles without emitting a main terminal event."""

        unresolved = set(self._child_interrupts) - self._resolved_child_interrupt_ids
        if unresolved:
            raise HitlCorrelationError(
                "child interrupts were not propagated by root values: "
                f"{', '.join(sorted(unresolved))}"
            )
        events = self._close_all_reasoning()
        events.extend(self._close_all_messages())
        events.extend(self._close_all_tools())
        return self._visible_events(events)

    def abort(self) -> list[BaseEvent]:
        """Close open child lifecycles after failure, without a main terminal."""

        return self._visible_events(
            [
                *self._close_all_reasoning(),
                *self._close_all_messages(),
                *self._close_all_tools(),
            ]
        )

    def main_outcome(self) -> AgentRunOutcome:
        """Return success or the ordered interrupts observed at a values boundary."""

        if self._interrupts_in_order:
            return AgentRunOutcome(
                type="interrupt",
                interrupts=tuple(self._interrupts_in_order),
            )
        return AgentRunOutcome(type="success")

    def _process_message_part(self, part: MessageStreamPart) -> list[BaseEvent]:
        return _adapter_messages._process_message_part(
            self,
            part,
        )

    def _process_tasks_part(self, part: TasksStreamPart) -> list[BaseEvent]:
        """Publish task provenance and establish subgraph correlation on start."""

        return _adapter_tasks._process_tasks_part(
            self,
            part,
        )

    def _process_task_start(
        self,
        parent_namespace: tuple[str, ...],
        payload: TaskStartPayload,
    ) -> list[BaseEvent]:
        return _adapter_tasks._process_task_start(
            self,
            parent_namespace,
            payload,
        )

    def _process_task_result(
        self,
        namespace: tuple[str, ...],
        payload: TaskResultPayload,
    ) -> list[BaseEvent]:
        return _adapter_tasks._process_task_result(
            self,
            namespace,
            payload,
        )

    def _process_values_part(self, part: ValuesStreamPart) -> list[BaseEvent]:
        return _adapter_tasks._process_values_part(
            self,
            part,
        )

    def _process_extra_part(
        self, part: ExtraStreamPart | UpdatesStreamPart
    ) -> list[BaseEvent]:
        """Project an additional native mode without exposing runtime config."""

        return _adapter_tasks._process_extra_part(
            self,
            part,
        )

    def _emit_values_part(self, part: ValuesStreamPart) -> list[BaseEvent]:
        return _adapter_tasks._emit_values_part(
            self,
            part,
        )

    @staticmethod
    def _interrupt_value_json(interrupt: AgentRuntimeInterrupt) -> str:
        """Return a strict finite fingerprint for propagation comparisons."""

        return _adapter_contracts._interrupt_value_json(
            interrupt,
        )

    @staticmethod
    def _group_prepared_interrupts(
        native_interrupts: Sequence[AgentRuntimeInterrupt],
        prepared: Sequence[AgUiInterrupt],
    ) -> tuple[tuple[AgUiInterrupt, ...], ...]:
        """Restore native interrupt groups after public multi-action expansion."""

        return _adapter_contracts._group_prepared_interrupts(
            native_interrupts,
            prepared,
        )

    def _buffer_child_interrupts(
        self,
        part: ValuesStreamPart,
        source: AgentSource,
        raw_messages: object,
    ) -> None:
        """Validate and stage child interrupts until root propagation arrives."""

        return _adapter_contracts._buffer_child_interrupts(
            self,
            part,
            source,
            raw_messages,
        )

    def _prepare_root_interrupts(
        self,
        native_interrupts: Sequence[AgentRuntimeInterrupt],
        source: AgentSource,
        raw_messages: object,
    ) -> tuple[list[AgUiInterrupt], set[str], tuple[tuple[str, ...], ...]]:
        """Match root interrupts to buffered children and prepare root-local ones."""

        return _adapter_contracts._prepare_root_interrupts(
            self,
            native_interrupts,
            source,
            raw_messages,
        )

    def _merge_message_snapshots(
        self,
        root_messages: Sequence[Message],
        child_namespaces: Sequence[tuple[str, ...]],
    ) -> list[Message]:
        """Merge root-first scoped snapshots and reject conflicting duplicate IDs."""

        return _adapter_tasks._merge_message_snapshots(
            self,
            root_messages,
            child_namespaces,
        )

    def _process_ai_chunk(
        self,
        chunk: AIMessageChunk,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        return _adapter_messages._process_ai_chunk(
            self,
            chunk,
            source,
            raw_event,
        )

    def _process_tool_chunk(
        self,
        tool_chunk: ToolCallChunk,
        chunk: AIMessageChunk,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        return _adapter_messages._process_tool_chunk(
            self,
            tool_chunk,
            chunk,
            source,
            raw_event,
        )

    def _process_tool_result(
        self,
        message: ToolMessage,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        return _adapter_messages._process_tool_result(
            self,
            message,
            source,
            raw_event,
        )

    def _convert_reasoning_events(
        self,
        chunk: AIMessageChunk,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        """Convert model reasoning events only when the client enables them."""

        return _adapter_messages._convert_reasoning_events(
            self,
            chunk,
            source,
            raw_event,
        )

    @staticmethod
    def _reasoning_deltas(chunk: AIMessageChunk) -> list[str]:
        """Read reasoning deltas only from the locked provider metadata path."""

        return _adapter_messages._reasoning_deltas(
            chunk,
        )

    def _emit_reasoning(
        self,
        delta: str,
        chunk: AIMessageChunk,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        return _adapter_messages._emit_reasoning(
            self,
            delta,
            chunk,
            source,
            raw_event,
        )

    def _close_reasoning(
        self,
        namespace: tuple[str, ...],
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        return _adapter_messages._close_reasoning(
            self,
            namespace,
            raw_event,
        )

    def _close_message(
        self,
        namespace: tuple[str, ...],
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        return _adapter_messages._close_message(
            self,
            namespace,
            raw_event,
        )

    def _close_tool(
        self,
        tool_call_id: str,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        return _adapter_messages._close_tool(
            self,
            tool_call_id,
            raw_event,
        )

    def _close_tools(
        self,
        namespace: tuple[str, ...],
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        return _adapter_messages._close_tools(
            self,
            namespace,
            raw_event,
        )

    def _close_all_reasoning(self) -> list[BaseEvent]:
        return _adapter_messages._close_all_reasoning(
            self,
        )

    def _close_all_messages(self) -> list[BaseEvent]:
        return _adapter_messages._close_all_messages(
            self,
        )

    def _close_all_tools(self) -> list[BaseEvent]:
        return _adapter_messages._close_all_tools(
            self,
        )

    def _record_agent_name(
        self,
        namespace: tuple[str, ...],
        agent_name: str | None,
    ) -> None:
        return _adapter_messages._record_agent_name(
            self,
            namespace,
            agent_name,
        )

    def _require_started_source(self, source: AgentSource) -> None:
        return _adapter_messages._require_started_source(
            self,
            source,
        )

    def _source(
        self,
        namespace: tuple[str, ...],
    ) -> AgentSource:
        return _adapter_messages._source(
            self,
            namespace,
        )

    def _event_context(
        self,
        stream_mode: StreamMode,
        source: AgentSource,
        *,
        langgraph_node: str | None = None,
        interrupt_id: str | None = None,
        related_namespace: tuple[str, ...] | None = None,
        related_subagent_invocation_id: str | None = None,
        parent_tool_call_id: str | None = None,
        tool_result_status: Literal["success", "error"] | None = None,
    ) -> dict[str, JsonValue]:
        return _adapter_messages._event_context(
            self,
            stream_mode,
            source,
            langgraph_node=langgraph_node,
            interrupt_id=interrupt_id,
            related_namespace=related_namespace,
            related_subagent_invocation_id=related_subagent_invocation_id,
            parent_tool_call_id=parent_tool_call_id,
            tool_result_status=tool_result_status,
        )

    @staticmethod
    def _stable_message_id(message: AIMessage) -> str:
        """Require the framework-provided stable ID used for cross-frame correlation."""

        return _adapter_messages._stable_message_id(
            message,
        )

    def _message_id(
        self,
        namespace: tuple[str, ...],
        raw_id: str,
    ) -> str:
        """Encode a native message ID as a namespace-aware AG-UI ID."""

        return _adapter_messages._message_id(
            self,
            namespace,
            raw_id,
        )

    def _tool_call_id(
        self,
        namespace: tuple[str, ...],
        raw_id: str,
    ) -> str:
        """Encode a native Tool call ID as a namespace-aware AG-UI ID."""

        return _adapter_messages._tool_call_id(
            self,
            namespace,
            raw_id,
        )

    def _convert_messages(
        self,
        raw_messages: object,
        namespace: tuple[str, ...],
    ) -> list[Message]:
        """Project complete checkpoint messages into an authoritative AG-UI snapshot."""

        return _adapter_messages._convert_messages(
            self,
            raw_messages,
            namespace,
        )

    def _record_interrupts(
        self,
        interrupts: Sequence[AgUiInterrupt],
    ) -> None:
        """Record terminal interrupts in first-seen order and ignore replayed frames."""

        return _adapter_contracts._record_interrupts(
            self,
            interrupts,
        )

    def _prepare_ag_ui_interrupts(
        self,
        native_interrupts: Sequence[AgentRuntimeInterrupt],
        source: AgentSource,
        raw_messages: object,
    ) -> list[AgUiInterrupt]:
        """Validate all HITL Tool correlations before mutating adapter state."""

        return _adapter_contracts._prepare_ag_ui_interrupts(
            self,
            native_interrupts,
            source,
            raw_messages,
        )

    def _validate_prepared_interrupts(
        self,
        prepared: Sequence[AgUiInterrupt],
    ) -> None:
        """Reject public-ID collisions and conflicting committed replays."""

        return _adapter_contracts._validate_prepared_interrupts(
            self,
            prepared,
        )

    def _tool_call_id_groups_for_actions(
        self,
        namespace: tuple[str, ...],
        action_groups: Sequence[Sequence[HitlActionRequest]],
        raw_messages: object,
    ) -> list[list[str]]:
        """Require each action group to have one unique Tool-call assignment."""

        return _adapter_contracts._tool_call_id_groups_for_actions(
            self,
            namespace,
            action_groups,
            raw_messages,
        )
