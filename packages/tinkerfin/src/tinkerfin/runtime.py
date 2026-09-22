"""Request-scoped Deep Agents native and AG-UI streaming."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from functools import partial
from typing import TYPE_CHECKING, Any, Generic, TypeAlias, TypeVar, overload
from uuid import uuid4

from deepagents.graph import DeepAgentState
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    convert_to_messages,
    message_chunk_to_message,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.pregel.main import All, Durability, RunControl, StreamMode
from langgraph.store.base import BaseStore
from langgraph.types import Command
from langgraph.typing import ContextT

from tinkerfin_contracts import (
    RunIdentity,
    RunSourceContext,
    RunTerminalOutcome,
    RuntimeObserver,
    ThreadIdentity,
)
from tinkerfin_contracts.identity import validate_namespace
from tinkerfin_native_stream import (
    NativeRuntimeInterrupt,
    NativeStreamContractError,
    NativeStreamFrame,
    NativeValuesStreamPart,
)

from . import _runtime_streams
from ._agent_spec import (
    AgentBackend,
    AgentMiddlewareType,
    AgentSpec,
    ResponseFormatType,
    SubagentDefinition,
    ToolDefinition,
)
from ._compaction_observation import CompactionOperation
from ._failure_evidence import select_failure
from ._lazy_run import AgUiRunStream, NativeRunStream
from ._observation import (
    RuntimeObservationHub,
    native_input_kind,
    observer_tuple,
    source_context,
)
from ._optional_dependencies import require_agui
from ._run_owner import RunOwner
from ._run_resources import RunResources
from ._runtime_streams import _validate_timeout
from ._tasks import OwnedOperationFailures, join_task
from ._terminal_observer import TerminalCallbackObserver, TerminalObserver
from .compaction import CompactionResult
from .coordination import RunCoordinator
from .deep_agent import _AgentDefinition, bind_agent
from .errors import (
    AgUiSettlementTimeoutError,
    TinkerFinLifecycleError,
    TinkerFinStreamProtocolError,
)
from .media import AttachmentSupport
from .native import NativeStreamPart
from .native_driver import (
    NativeStreamDriver,
)
from .plan._clarification import create_clarification_binding
from .plan._config import (
    DEFAULT_ALLOWED_REVIEW_ACTIONS,
    AgentMode,
    PlanOptions,
    resolve_agent_mode,
    validate_agent_mode,
    validate_allowed_review_actions,
)
from .plan._content import create_plan_content_binding
from .plan._contracts import create_plan_contract_binding
from .plan.clarification import ClarificationFormBase, DefaultClarificationForm
from .plan.clarification_types import ClarificationType
from .plan.models import PlanContentModel, PlanReviewAction, StructuredPlanContent
from .runtime_profile import DeepAgentsRuntimeProfile, DeepAgentsV2RuntimeProfile
from .sse import (
    SseBody,
    SseEventIdResolver,
    SseMapper,
    SsePayload,
    SsePreflight,
)

PartT = TypeVar("PartT")

if TYPE_CHECKING:
    from ag_ui.core import BaseEvent, UserMessage

    from .agui import RuntimeAgUi
    from .agui_resume import (
        AgUiResumeBinding,
        AgUiResumeCheckpointObserver,
        AgUiResumeNotSavedObserver,
        AgUiResumeRequest,
    )

    EventObserver: TypeAlias = Callable[[BaseEvent], Awaitable[None]]
else:
    # Runtime annotations must stay importable without the AG-UI extra. Static analysis
    # uses the exact BaseEvent callback above; only the runtime alias is protocol-neutral.
    EventObserver: TypeAlias = Callable[[object], Awaitable[None]]

PartObserver: TypeAlias = Callable[[PartT], Awaitable[None]]
NativeFrameResolver = Callable[[object], NativeStreamFrame]
_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)
_DefinitionAny: TypeAlias = _AgentDefinition


def _translate_agui_conversion_error(error: Exception) -> Exception:
    """Translate Adapter-owned failures before they escape the Runtime facade.

    Native validation already uses the Runtime's stable error family on normal Profile
    paths. This fallback protects direct or initialization streams that do not carry a
    frame resolver, and also keeps later Adapter correlation failures from leaking a
    dependency-specific exception through TinkerFin.
    """

    require_agui()
    from tinkerfin_agui_adapter import AgUiAdapterError

    if not isinstance(error, AgUiAdapterError):
        return error
    native_cause = error.cause
    if isinstance(native_cause, NativeStreamContractError):
        translated = _runtime_streams._native_contract_error(native_cause)
    else:
        translated = TinkerFinStreamProtocolError(
            "AG-UI conversion violates the current Runtime contract",
            context=error.context,
            diagnostic_context={"adapter_code": error.code.value},
            cause=error,
        )
    return translated.with_traceback(error.__traceback__)


class _GraphRunStream(Generic[PartT]):
    """Internal single-use stream with deterministic upstream cleanup."""

    def __init__(
        self,
        *,
        source_factory: Callable[[], AsyncIterator[PartT]],
        source_preflight: Callable[[], Awaitable[object]] | None,
        coordination_factory: (Callable[[], AbstractAsyncContextManager[None]] | None),
        on_part: PartObserver[PartT] | None,
        identity: RunIdentity,
        observation: RuntimeObservationHub,
        stream_driver: NativeStreamDriver,
        on_settle: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize a lazy, single-use native stream.

        The stream owns the iterator returned by ``source_factory`` and its coordination
        scope. An optional retained preflight settles asynchronous source construction
        before the source iterator is opened. It never owns the observer or identity.
        External close cancels an active pull and retains cleanup until settlement.
        """

        self._source_factory = source_factory
        self._source_preflight = source_preflight
        self._coordination_factory = coordination_factory
        self._on_part = on_part
        self._identity = identity
        self._observation = observation
        self._stream_driver = stream_driver
        self._on_settle = on_settle
        self._native_frame: tuple[object, NativeStreamFrame] | None = None
        self._root_interrupt_ids: tuple[str, ...] = ()
        self._source: AsyncIterator[PartT] | None = None
        self._coordination: AbstractAsyncContextManager[None] | None = None
        self._run_resources: RunResources | None = None
        self._started = False
        self._closed = False
        self._ready_error: Exception | None = None
        self._ready_error_delivered = False
        self._active_task: asyncio.Task[object] | None = None
        self._observer_lineage = ContextVar(
            f"tinkerfin_graph_part_observer_lineage_{id(self)}",
            default=False,
        )
        self._active_observers = 0
        self._finish_task: asyncio.Task[None] | None = None
        self._owned_finish_done = False
        self._owned_finish_error: BaseException | None = None
        self._owned_operation_failures, _owns_failures = (
            OwnedOperationFailures.for_source()
        )
        self.error: Exception | None = None

    def _adopt_resources(self, resources: RunResources) -> None:
        """Take prepared ownership before startup without acquiring coordination twice."""

        if self._started or self._run_resources is not None:
            raise TinkerFinLifecycleError("Run resources must transfer before startup")
        resources.transfer()
        self._coordination_factory = None
        self._run_resources = resources
        resources.owner.bind_close(self._settle_owned)
        if self._observation.enabled:
            resources.owner.watch_failure(self._observation.wait_failure)

    async def _settle_owned(self, error: BaseException | None) -> None:
        """Close an idle or interrupted native Run inside its lifetime Task."""

        if isinstance(error, Exception) and not self._closed:
            self.error = error
            self._ready_error = error
        await self._finish(
            error,
            outcome=None
            if not self._started
            else (
                "cancelled" if error is None else _runtime_streams._error_outcome(error)
            ),
        )

    def _take_frame(self, part: object) -> NativeStreamFrame:
        """Consume the one canonical sidecar belonging to a delivered raw part.

        The sidecar is single-use because AG-UI, Native SSE, and Messaging are
        alternative consumers of one stream. A missing or mismatched sidecar is a
        lifecycle violation; reparsing the raw object here would reintroduce hidden
        third-party profile knowledge outside the selected Driver.

        Raises:
            TinkerFinLifecycleError: The part was not just delivered by this stream or
                another consumer already claimed its canonical frame.
        """

        binding = self._native_frame
        self._native_frame = None
        if binding is None or binding[0] is not part:
            raise TinkerFinLifecycleError(
                "the canonical Native frame is unavailable for this delivered part"
            )
        return binding[1]

    def __aiter__(self) -> _GraphRunStream[PartT]:
        """Return this single-use asynchronous iterator."""

        return self

    async def __anext__(self) -> PartT:
        """Pull, observe, and return the next native part."""

        resources = self._run_resources
        if resources is not None and not self._closed:
            return await resources.owner.call(partial(_runtime_streams.__anext__, self))
        return await _runtime_streams.__anext__(
            self,
        )

    async def aclose(self) -> None:
        """Close the native source and release coordination idempotently.

        Closure requested from a part-observer call chain preserves delivery of the
        part currently being observed. An external closer cancels an active pull.
        """

        resources = self._run_resources
        if resources is not None and not resources.owner.is_current():
            await resources.owner.aclose()
            if self._owned_finish_error is not None:
                raise self._owned_finish_error
            return
        return await _runtime_streams.aclose(
            self,
        )

    async def _ready(self) -> None:
        """Open the managed Observation and source boundary without pulling output."""

        return await _runtime_streams.ready(self)

    def to_sse(
        self,
        *,
        timeout: float | None = None,
        mapper: SseMapper[NativeStreamPart] | None = None,
        event_id_resolver: SseEventIdResolver[NativeStreamPart] | None = None,
    ) -> SseBody[bytes]:
        """Consume this native object stream as UTF-8 SSE bytes."""

        return _runtime_streams.to_sse(
            self,
            timeout=timeout,
            mapper=mapper,
            event_id_resolver=event_id_resolver,
        )

    async def _observe(self, part: PartT) -> None:
        return await _runtime_streams._observe(
            self,
            part,
        )

    async def _start(self) -> None:
        return await _runtime_streams._start(
            self,
        )

    async def _finish(
        self,
        error: BaseException | None,
        *,
        outcome: RunTerminalOutcome | None = None,
    ) -> None:
        return await _runtime_streams._finish(
            self,
            error,
            outcome=outcome,
        )

    async def _finish_once(
        self,
        error: BaseException | None,
        outcome: RunTerminalOutcome | None,
    ) -> None:
        return await _runtime_streams._finish_once(
            self,
            error,
            outcome,
        )


class NativeGraphRunStream(_GraphRunStream[Mapping[str, object]]):
    """Raw upstream stream with one Driver-owned canonical replay sidecar."""

    @property
    def messaging_identity(self) -> RunIdentity:
        """Return the immutable durable run identity."""

        return self._identity

    @property
    def messaging_codec_profile(self) -> str:
        """Return the canonical native persistence profile."""

        return "tinkerfin.native-stream"

    @property
    def messaging_source_type(self) -> type[Mapping[str, object]]:
        """Return the declared live LangGraph envelope class."""

        return Mapping

    @property
    def messaging_codec_input_type(self) -> type[NativeStreamPart]:
        """Return the Driver-owned finite model accepted by the Native codec."""

        return NativeStreamPart

    def messaging_codec_input(
        self,
        item: Mapping[str, object],
    ) -> NativeStreamPart:
        """Claim the exact canonical replay model paired with a live item.

        Messaging calls this structural hook after pulling ``item`` and before the
        next source pull. The method transfers the already normalized frame sidecar;
        it never validates or serializes the upstream LangGraph mapping again.

        Args:
            item: Raw object most recently yielded by this source.

        Returns:
            The immutable finite replay model produced by the selected Driver.

        Raises:
            TinkerFinLifecycleError: The item does not own the pending frame or the
                frame was already consumed.
        """

        return self._take_frame(item).replay

    @property
    def messaging_replay_type(self) -> type[NativeStreamPart]:
        """Return the decoded durable replay class."""

        return NativeStreamPart


class AgUiEventStream:
    """Own one observed AG-UI stream and its independently budgeted close task."""

    @property
    def messaging_cancel_waits_for_first_item(self) -> bool:
        """Declare that durable cancellation must not overtake ``RUN_STARTED``.

        Messaging integrations use this structural fact to keep the first public
        lifecycle event ahead of a cancellation tail. Advanced sources can still
        configure their own explicit first-item barrier.
        """

        return True

    @property
    def messaging_codec_profile(self) -> str:
        """Return the canonical AG-UI event persistence profile."""

        return "agui.event"

    @property
    def messaging_source_type(self) -> type[BaseEvent]:
        """Return the declared live AG-UI event base class."""

        require_agui()
        from ag_ui.core import BaseEvent

        return BaseEvent

    @property
    def messaging_replay_type(self) -> type[BaseEvent]:
        """Return the decoded durable event base class."""

        require_agui()
        from ag_ui.core import BaseEvent

        return BaseEvent

    @property
    def messaging_identity(self) -> RunIdentity:
        """Return the immutable durable run identity."""

        return self._identity

    @property
    def messaging_cancel_callback(
        self,
    ) -> Callable[[], Awaitable[list[BaseEvent]]]:
        """Publish the stream-owned idempotent cancellation callback."""

        return self.abort

    def messaging_cancel_callback_matches(self, callback: object) -> bool:
        """Return whether a supplied callback names this stream's same owner."""

        return callback == self.abort

    def __init__(
        self,
        *,
        parts: AsyncIterable[object],
        identity: RunIdentity,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        prior_tool_call_ids: frozenset[str],
        private_state_keys: frozenset[str] = frozenset(),
        native_frame_resolver: NativeFrameResolver | None = None,
        timeout: float | None,
        settlement_timeout: float | None = None,
        on_event: EventObserver | None,
        parent_run_id: str | None = None,
    ) -> None:
        """Initialize one owned AG-UI conversion stream.

        ``parts`` is owned and closed exactly once. RunIdentity, parent lineage, and
        callbacks are borrowed. Timeouts bound caller waits without
        abandoning the retained upstream close task.

        Args:
            parts: Single-use Native source transferred to this stream.
            identity: Canonical public and Graph Run identity.
            expose_reasoning_events: Whether verified public reasoning is emitted.
            expose_subagent_events: Whether validated subagent events are emitted.
            prior_tool_call_ids: Scoped calls emitted before a resumed request.
            private_state_keys: Runtime-owned state channels omitted from AG-UI.
            native_frame_resolver: Optional single-normalization sidecar resolver.
            timeout: Optional total Native pull deadline in seconds.
            settlement_timeout: Optional caller wait for protected close settlement.
            on_event: Optional borrowed observer awaited before event delivery.
            parent_run_id: Optional branch or resume source in the same thread.

        Raises:
            TypeError: Identity, parent lineage, timeout, or callbacks are invalid.
            ModuleNotFoundError: The AG-UI extra is not installed.
        """

        require_agui()
        from tinkerfin_agui_adapter import (
            AgUiLifecycleEventFactory,
            DeepAgentAgUiAdapter,
            micro_batch,
        )

        from . import _runtime_agui

        self._runtime_agui = _runtime_agui
        self._lifecycle = AgUiLifecycleEventFactory()
        self._lifecycle.validate_identity(identity)
        self._lifecycle.validate_parent_run_id(parent_run_id, identity=identity)
        self._identity = identity
        self._parent_run_id = parent_run_id
        self._timeout = _validate_timeout(timeout)
        self._settlement_timeout = _validate_timeout(
            settlement_timeout,
            name="settlement_timeout",
        )
        self._deadline: float | None = None
        self._upstream = aiter(parts)
        self._start_parts = parts._ready if isinstance(parts, _GraphRunStream) else None
        self._upstream_closed = False
        self._adapter = DeepAgentAgUiAdapter(
            identity=identity,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=prior_tool_call_ids,
            private_state_keys=private_state_keys,
        )
        self._native_frame_resolver = native_frame_resolver
        self._source = aiter(micro_batch(self._convert()))
        self._on_event = on_event
        self._closed = False
        self._active_task: asyncio.Task[object] | None = None
        self._observer_lineage = ContextVar(
            f"tinkerfin_agui_observer_lineage_{id(self)}",
            default=False,
        )
        self._active_observers = 0
        self._close_task: asyncio.Task[None] | None = None
        self._main_started = False
        self._completed = False
        self._aborted = False
        self._abort_events_delivered = False
        self._secondary_error_notes: list[str] = []
        self._runtime_error_code = "runtime_error"
        self._initialization_failed = False
        self._resume_abandoned = False
        self.error: Exception | None = None

    def _adopt_resources(self, resources: RunResources) -> None:
        """Transfer preparation ownership to this conversion's native stream."""

        if not isinstance(self._upstream, NativeGraphRunStream):
            raise TinkerFinLifecycleError("Runtime preparation requires a native Run")
        self._upstream._adopt_resources(resources)

    def __aiter__(self) -> AgUiEventStream:
        """Return this single-use AG-UI asynchronous iterator."""

        return self

    async def __anext__(self) -> BaseEvent:
        """Return the next observed and lifecycle-safe AG-UI event."""

        return await self._runtime_agui.__anext__(
            self,
        )

    async def abort(self) -> list[BaseEvent]:
        """Cancel the active conversion and return one observed cancelled tail.

        Returns:
            The observed cancellation tail, or an empty list after a terminal state
            or prior abort delivery.

        Raises:
            RuntimeError: Called recursively from this stream's event observer before
                the main run reaches a terminal state.
        """

        return await self._runtime_agui.abort(
            self,
        )

    async def aclose(self) -> None:
        """Close this stream and its upstream parts idempotently.

        Closure requested from an event-observer call chain preserves delivery of
        the event currently being observed. An external closer cancels an active
        pull before waiting for the shared cleanup.
        """

        return await self._runtime_agui.aclose(
            self,
        )

    async def _ready(self) -> None:
        """Open the managed Native boundary without consuming an AG-UI event."""

        start_parts = self._start_parts
        if start_parts is not None:
            await start_parts()

    def to_sse(
        self,
        *,
        mapper: SseMapper[BaseEvent] | None = None,
        event_id_resolver: SseEventIdResolver[BaseEvent] | None = None,
    ) -> SseBody[bytes]:
        """Consume this AG-UI object stream as UTF-8 SSE bytes."""

        return self._runtime_agui.to_sse(
            self,
            mapper=mapper,
            event_id_resolver=event_id_resolver,
        )

    async def _close(
        self,
        primary: BaseException | None,
        *,
        active: asyncio.Task[object] | None = None,
    ) -> None:
        return await self._runtime_agui._close(
            self,
            primary,
            active=active,
        )

    def _close_finished(self, task: asyncio.Task[None]) -> None:
        """Consume a retained close failure even when no caller waits again."""

        return self._runtime_agui._close_finished(
            task,
        )

    async def _close_once(self, active: asyncio.Task[object] | None) -> None:
        return await self._runtime_agui._close_once(
            self,
            active,
        )

    async def _observe(self, event: BaseEvent) -> None:
        return await self._runtime_agui._observe(
            self,
            event,
        )

    async def _close_upstream(self, primary: BaseException | None) -> None:
        return await self._runtime_agui._close_upstream(
            self,
            primary,
        )

    def _record_secondary_error_note(self, note: str) -> None:
        """Retain cleanup evidence across the micro-batch cancellation boundary."""

        return self._runtime_agui._record_secondary_error_note(
            self,
            note,
        )

    async def _convert(self) -> AsyncIterator[BaseEvent]:
        primary: BaseException | None = None
        terminal = False
        initial_event = self._decorate_initialization_event(
            self._lifecycle.started(
                identity=self._identity,
                parent_run_id=self._parent_run_id,
            )
        )
        try:
            start_parts = self._start_parts
            if start_parts is not None:
                await start_parts()
            self._main_started = True
            yield initial_event
            while True:
                try:
                    part = await self._next_part()
                except StopAsyncIteration:
                    break
                if self._aborted:
                    return
                resolver = self._native_frame_resolver
                events = (
                    self._adapter.process(part)
                    if resolver is None
                    else self._adapter.process_frame(resolver(part))
                )
                for event in events:
                    yield event
            await self._close_upstream(None)
            if self._resume_abandoned:
                self._completed = True
                terminal = True
                yield self._lifecycle.failed(
                    identity=self._identity,
                    message="Agent resume cancelled",
                    code="resume_cancelled",
                    parent_run_id=self._parent_run_id,
                )
                return
            for event in self._adapter.finish():
                yield event
            outcome = self._adapter.main_outcome()
            self._completed = True
            terminal = True
            yield self._lifecycle.finished(
                identity=self._identity,
                outcome=outcome,
            )
        except asyncio.CancelledError as error:
            primary = error
            raise
        except GeneratorExit as error:
            primary = error
            raise
        except Exception as error:  # noqa: BLE001 - Runtime owns terminal conversion
            error = _translate_agui_conversion_error(error)
            self.error = error
            primary = error
            error_code = (
                "stream_timeout"
                if isinstance(error, self._runtime_agui._AgUiStreamDeadlineExceeded)
                else self._runtime_error_code
            )
            if not self._main_started:
                self._main_started = True
                yield initial_event
            try:
                await self._close_upstream(error)
            except asyncio.CancelledError as cancellation:
                primary = cancellation
                raise
            for event in self._adapter.abort():
                yield event
            if not terminal:
                terminal = True
                self._completed = True
                yield self._decorate_initialization_event(
                    self._lifecycle.failed(
                        identity=self._identity,
                        message="Agent run failed",
                        code=error_code,
                        parent_run_id=self._parent_run_id,
                    )
                )
        except BaseException as error:
            primary = error
            raise
        finally:
            await self._close_upstream(primary)

    def _decorate_initialization_event(self, event: BaseEvent) -> BaseEvent:
        """Mark only main lifecycle events emitted for initialization failure."""

        return self._runtime_agui._decorate_initialization_event(
            self,
            event,
        )

    async def _next_part(self) -> object:
        return await self._runtime_agui._next_part(
            self,
        )


class TinkerFin:
    """Configure an agent, then build a namespace-bound Runtime.

    Configuration methods return independent builders and borrow supplied resources.
    Building validates and captures configuration without opening execution resources.
    """

    __slots__ = (
        "_attachments",
        "_checkpointer",
        "_compaction_tool_enabled",
        "_namespace",
        "_observers",
        "_plan_options",
        "_run_coordinator",
        "_runtime_profile",
        "_store",
    )

    def __init__(
        self,
        *,
        checkpointer: _CheckpointSaver | None = None,
        run_coordinator: RunCoordinator | None = None,
        store: BaseStore | None = None,
        runtime_profile: DeepAgentsRuntimeProfile | None = None,
    ) -> None:
        """Configure shared resources without opening an execution.

        Args:
            checkpointer: Borrowed default checkpoint saver. The application opens
                and closes it.
            run_coordinator: Optional exclusive scope provider for run identities.
            store: Borrowed long-term memory store, isolated by Runtime namespace.
            runtime_profile: Optional Native stream and checkpoint integration.

        Raises:
            TypeError: The checkpointer, coordinator, or Runtime Profile has the wrong
                type.
            ValueError: The Runtime Profile ID is not canonical.
            StateSchemaCompositionError: The state schema is not a valid TypedDict.
        """

        if checkpointer is not None and not isinstance(
            checkpointer, BaseCheckpointSaver
        ):
            raise TypeError("checkpointer must be a BaseCheckpointSaver or None")
        if run_coordinator is not None and not callable(run_coordinator):
            raise TypeError("run_coordinator must be callable or None")
        if runtime_profile is not None and not isinstance(
            runtime_profile,
            DeepAgentsRuntimeProfile,
        ):
            raise TypeError(
                "runtime_profile must implement DeepAgentsRuntimeProfile or be None"
            )
        resolved_profile = runtime_profile or DeepAgentsV2RuntimeProfile()
        profile_id = resolved_profile.profile_id
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or profile_id != profile_id.strip()
        ):
            raise ValueError("runtime_profile.profile_id must be canonical text")
        if not isinstance(resolved_profile.astream_signature, inspect.Signature):
            raise TypeError("runtime_profile.astream_signature must be a Signature")
        if store is not None and not isinstance(store, BaseStore):
            raise TypeError("store must implement BaseStore or be None")
        self._checkpointer = checkpointer
        self._namespace: str | None = None
        self._run_coordinator = run_coordinator
        self._store = store
        self._runtime_profile = resolved_profile
        self._observers: tuple[RuntimeObserver, ...] = ()
        self._plan_options: PlanOptions | None = None
        self._attachments: AttachmentSupport | None = None
        self._compaction_tool_enabled = False

    @overload
    def build(
        self,
        model: str | BaseChatModel,
        tools: Sequence[ToolDefinition] | None = None,
        *,
        system_prompt: str | SystemMessage | None = None,
        middleware: Sequence[AgentMiddlewareType] = (),
        subagents: Sequence[SubagentDefinition] | None = None,
        skills: Sequence[str] | None = None,
        memory: Sequence[str] | None = None,
        permissions: Sequence[FilesystemPermission] | None = None,
        backend: AgentBackend = None,
        interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
        response_format: ResponseFormatType = None,
        state_schema: type[DeepAgentState] | None = None,
        context_schema: type[ContextT],
        name: str | None = None,
    ) -> AgentRuntime[ContextT]: ...

    @overload
    def build(
        self,
        model: str | BaseChatModel,
        tools: Sequence[ToolDefinition] | None = None,
        *,
        system_prompt: str | SystemMessage | None = None,
        middleware: Sequence[AgentMiddlewareType] = (),
        subagents: Sequence[SubagentDefinition] | None = None,
        skills: Sequence[str] | None = None,
        memory: Sequence[str] | None = None,
        permissions: Sequence[FilesystemPermission] | None = None,
        backend: AgentBackend = None,
        interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
        response_format: ResponseFormatType = None,
        state_schema: type[DeepAgentState] | None = None,
        context_schema: None = None,
        name: str | None = None,
    ) -> AgentRuntime[None]: ...

    def build(
        self,
        model: str | BaseChatModel,
        tools: Sequence[ToolDefinition] | None = None,
        *,
        system_prompt: str | SystemMessage | None = None,
        middleware: Sequence[AgentMiddlewareType] = (),
        subagents: Sequence[SubagentDefinition] | None = None,
        skills: Sequence[str] | None = None,
        memory: Sequence[str] | None = None,
        permissions: Sequence[FilesystemPermission] | None = None,
        backend: AgentBackend = None,
        interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
        response_format: ResponseFormatType = None,
        state_schema: type[DeepAgentState] | None = None,
        context_schema: type[ContextT] | None = None,
        name: str | None = None,
    ) -> AgentRuntime[Any]:
        """Build a reusable agent Runtime without model or resource I/O.

        The explicit model, tools, and optional capabilities describe this agent.
        Shared persistence is configured on TinkerFin. Configuration containers
        are copied, while models, tools, middleware, and resources stay borrowed.
        Each admitted run prepares and releases its own workspace and graph.

        Args:
            model: Chat model instance or provider:model identifier.
            tools: Additional tools, created before execution. Tools may request
                ToolRuntime to access the current run's workspace and context.
            system_prompt: Instructions supplied to the main model.
            middleware: Additional behavior or named replacements for default
                middleware. Configure delegation through subagents and tool review
                through interrupt_on; their middleware and tool names are reserved.
                Resource-bearing declarations use the Runtime store.
                A workspace owns its filesystem middleware and file tools. Without
                a workspace, replacing these is allowed only for roles without
                effective file permissions.
            subagents: Declared, compiled, or remote agents available for delegation.
                A general-purpose agent is added unless explicitly declared.
            skills: Backend directories containing this agent's skills.
            memory: Backend files loaded as persistent instructions.
            permissions: File-tool rules. These do not restrict arbitrary shell
                commands; executable backends only support protected non-shell routes.
            backend: Borrowed filesystem backend or lazy workspace declaration.
            interrupt_on: Tool-review rules requiring a configured checkpointer.
            response_format: Optional structured response schema or strategy.
            state_schema: Additional persistent fields for this agent.
            context_schema: Type of business context supplied to execution methods.
            name: Optional name identifying this agent in observations.

        Returns:
            A reusable Runtime bound to this namespace and context type.

        Raises:
            TypeError: Configuration contains invalid types or unsupported fields.
            ValueError: The namespace, model, or declarations are inconsistent,
                including filesystem replacements that conflict with workspace
                ownership or a role's effective file permissions.
            StateSchemaCompositionError: Persistent state fields conflict.
        """
        spec: AgentSpec[ContextT] = AgentSpec(
            model=model,
            tools=tools if tools is not None else (),
            system_prompt=system_prompt,
            middleware=middleware,
            compaction_tool_enabled=self._compaction_tool_enabled,
            subagents=subagents if subagents is not None else (),
            skills=skills,
            memory=memory,
            permissions=permissions if permissions is not None else (),
            backend=backend,
            interrupt_on=interrupt_on,
            response_format=response_format,
            state_schema=state_schema,
            context_schema=context_schema,
            checkpointer=self._checkpointer,
            store=self._store,
            name=name,
            attachments=self._attachments,
        )
        return bind_agent(self, spec)

    def with_compaction_tool(self, *, enabled: bool = True) -> TinkerFin:
        """Let the main agent request context compression through its native tool.

        The tool shares the effective summarization middleware's model, retention,
        backend and eligibility rules. It runs within the current conversation;
        declared subagents and the read-only Planner do not inherit this option.

        Args:
            enabled: Whether the main agent receives ``compact_conversation``.

        Returns:
            An independent builder retaining its resources and other options.

        Raises:
            TypeError: ``enabled`` is not a boolean.
        """
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        configured = TinkerFin(
            checkpointer=self._checkpointer,
            run_coordinator=self._run_coordinator,
            store=self._store,
            runtime_profile=self._runtime_profile,
        )
        configured._namespace = self._namespace
        configured._observers = self._observers
        configured._plan_options = self._plan_options
        configured._attachments = self._attachments
        configured._compaction_tool_enabled = enabled
        return configured

    def with_attachments(self, support: AttachmentSupport) -> TinkerFin:
        """Configure authorized attachment access for the selected models.

        Framework-created agents apply this policy after model routing. The reader
        remains borrowed; authorization and its resource lifetime belong to the host.

        Args:
            support: Borrowed per-model content policy and host-authorized file reader.

        Returns:
            An independent builder retaining its namespace and other options.

        Raises:
            TypeError: Support is not an AttachmentSupport instance.
        """
        if not isinstance(support, AttachmentSupport):
            raise TypeError("support must be AttachmentSupport")
        configured = TinkerFin(
            checkpointer=self._checkpointer,
            run_coordinator=self._run_coordinator,
            store=self._store,
            runtime_profile=self._runtime_profile,
        )
        configured._observers = self._observers
        configured._plan_options = self._plan_options
        configured._namespace = self._namespace
        configured._attachments = support
        configured._compaction_tool_enabled = self._compaction_tool_enabled
        return configured

    def with_observer(
        self,
        observer: RuntimeObserver | None = None,
        *,
        on_terminal: TerminalObserver | None = None,
    ) -> TinkerFin:
        """Observe runs with a full observer or an asynchronous terminal callback.

        Callbacks run in this process and are awaited. Their failures propagate without
        changing the selected outcome. Terminal notification can precede final cleanup.

        Args:
            observer: Borrowed observer opening one session per admitted run.
            on_terminal: Callback receiving the selected run outcome once. Exclusive
                with observer; it does not provide durable notification delivery.

        Returns:
            An independent builder retaining its namespace and other options.

        Raises:
            TypeError: The observer or callback has an invalid type.
            ValueError: Neither or both options are set, or an observer is duplicated.
        """

        if (observer is None) == (on_terminal is None):
            raise ValueError("provide exactly one of observer or on_terminal")
        if on_terminal is not None:
            if not callable(on_terminal):
                raise TypeError("on_terminal must be an async callable")
            observer = TerminalCallbackObserver(on_terminal)
        assert observer is not None
        observers = observer_tuple((*self._observers, observer))
        configured = TinkerFin(
            checkpointer=self._checkpointer,
            run_coordinator=self._run_coordinator,
            store=self._store,
            runtime_profile=self._runtime_profile,
        )
        configured._plan_options = self._plan_options
        configured._observers = observers
        configured._compaction_tool_enabled = self._compaction_tool_enabled
        configured._namespace = self._namespace
        configured._attachments = self._attachments
        return configured

    def with_plan(
        self,
        *,
        enabled: bool = True,
        default_mode: AgentMode = "default",
        planner_model: str | BaseChatModel | None = None,
        clarification_schema: type[ClarificationFormBase] = DefaultClarificationForm,
        clarification_types: Sequence[ClarificationType[Any, Any]] = (),
        content_schema: type[PlanContentModel] = StructuredPlanContent,
        allowed_review_actions: Sequence[
            PlanReviewAction
        ] = DEFAULT_ALLOWED_REVIEW_ACTIONS,
    ) -> TinkerFin:
        """Configure planning with read-only tools and human review.

        Existing builders and Runtimes retain their configuration. The returned
        builder borrows the same shared resources.

        Args:
            enabled: Whether built Runtimes support planning.
            default_mode: Mode used when execution does not select one.
            planner_model: Optional model dedicated to read-only planning.
            clarification_schema: Concrete host form used by the Planner.
            clarification_types: Additional custom semantic question types.
            content_schema: Concrete content model used for drafts and confirmed Plans.
            allowed_review_actions: Ordered decisions accepted for each Plan draft
                review.

        Returns:
            An independent builder retaining its namespace and other options.

        Raises:
            TypeError: ``enabled``, a model, or a review action has the wrong type.
            PlanModeConfigurationError: A mode or disabled configuration is invalid.
        """

        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        mode = validate_agent_mode(default_mode, name="default_mode")
        actions = validate_allowed_review_actions(allowed_review_actions)
        if isinstance(clarification_types, (str, bytes)) or not isinstance(
            clarification_types, Sequence
        ):
            raise TypeError("clarification_types must be a sequence")
        frozen_clarification_types = tuple(clarification_types)
        for name, model in (("planner_model", planner_model),):
            if model is not None and not isinstance(model, (str, BaseChatModel)):
                raise TypeError(
                    f"{name} must be a model string, BaseChatModel, or None"
                )
            if isinstance(model, str) and not model.strip():
                raise ValueError(f"{name} must not be blank")
        if not enabled and (
            mode != "default"
            or planner_model is not None
            or clarification_schema is not DefaultClarificationForm
            or frozen_clarification_types
            or content_schema is not StructuredPlanContent
            or actions != DEFAULT_ALLOWED_REVIEW_ACTIONS
        ):
            from .plan.errors import PlanModeConfigurationError

            raise PlanModeConfigurationError(
                "disabled Plan capability cannot configure a mode, model, form, "
                "clarification types, content schema, or review actions"
            )
        configured = TinkerFin(
            checkpointer=self._checkpointer,
            run_coordinator=self._run_coordinator,
            store=self._store,
            runtime_profile=self._runtime_profile,
        )
        configured._observers = self._observers
        configured._namespace = self._namespace
        configured._attachments = self._attachments
        configured._compaction_tool_enabled = self._compaction_tool_enabled
        if enabled:
            clarification = create_clarification_binding(
                clarification_schema,
                custom_types=frozen_clarification_types,
            )
            content = create_plan_content_binding(content_schema)
            configured._plan_options = PlanOptions(
                clarification=clarification,
                content=content,
                contracts=create_plan_contract_binding(
                    clarification,
                    content,
                    allowed_review_actions=actions,
                ),
                allowed_review_actions=actions,
                default_mode=mode,
                planner_model=planner_model,
            )
        return configured

    def with_namespace(self, namespace: str) -> TinkerFin:
        """Return an independent builder using the host's isolation namespace.

        Args:
            namespace: Opaque, case-sensitive namespace selected by the application.

        Returns:
            A builder borrowing the same resources and retaining other options.

        Raises:
            TypeError: The namespace is not a string.
            ValueError: The namespace is empty, padded, too long, or not UTF-8 encodable.
        """

        value = validate_namespace(namespace)
        configured = TinkerFin(
            checkpointer=self._checkpointer,
            run_coordinator=self._run_coordinator,
            store=self._store,
            runtime_profile=self._runtime_profile,
        )
        configured._namespace = value
        configured._compaction_tool_enabled = self._compaction_tool_enabled
        configured._observers = self._observers
        configured._plan_options = self._plan_options
        configured._attachments = self._attachments
        return configured

    def _require_namespace(self) -> str:
        if self._namespace is None:
            raise ValueError("set with_namespace(...) before build()")
        return self._namespace


class AgentRuntime(Generic[ContextT]):
    """Execute the agent configuration captured by TinkerFin.build().

    A Runtime has one immutable namespace and borrows its model, stores, observers,
    and coordinator. Each run owns its own preparation, graph, streams, and cleanup.
    Direct construction is unsupported; obtain a Runtime from a configured builder.
    """

    _definition: _DefinitionAny
    _namespace: str
    _checkpointer: _CheckpointSaver | None
    _observers: tuple[RuntimeObserver, ...]
    _plan_options: PlanOptions | None
    _run_coordinator: RunCoordinator | None
    _runtime_profile: DeepAgentsRuntimeProfile

    def __init__(self) -> None:
        """Reject construction without a bound agent configuration.

        Raises:
            TypeError: Always; use TinkerFin.with_namespace(...).build(...).
        """

        raise TypeError("AgentRuntime is created by TinkerFin.build()")

    @classmethod
    def _create(cls, builder: TinkerFin) -> AgentRuntime[ContextT]:
        runtime = cls.__new__(cls)
        runtime._namespace = builder._require_namespace()
        runtime._checkpointer = builder._checkpointer
        runtime._observers = builder._observers
        runtime._plan_options = builder._plan_options
        runtime._run_coordinator = builder._run_coordinator
        runtime._runtime_profile = builder._runtime_profile
        return runtime

    @property
    def agui(self) -> RuntimeAgUi:
        """Access AG-UI operations and history in this Runtime's namespace."""
        require_agui()
        from .agui import RuntimeAgUi

        return RuntimeAgUi._create(self)

    async def compact(
        self,
        *,
        thread_id: str,
        run_id: str,
        context: ContextT | None = None,
    ) -> CompactionResult:
        """Summarize older saved context without adding conversation messages.

        This operation requires a checkpointer and an idle conversation. It shares
        the configured coordinator and workspace lifecycle with ordinary runs.
        Hosts must use the same execution boundary for chatting and compression.
        Cancellation stops generation; after persistence starts the caller must
        inspect the saved result before asserting that context was unchanged.

        Args:
            thread_id: Conversation to compress in this Runtime's namespace.
            run_id: Identity of this compression operation.
            context: Context matching the schema supplied to build().

        Returns:
            A saved summary or the reason that no compression was applied.

        Raises:
            ValueError: No checkpointer is configured.
            TinkerFinLifecycleError: Pending work prevents compression.
            BaseException: Generation, persistence, observation or cleanup fails.
        """
        from ._compaction import COMPACTION_STATE_KEY

        identity = self.run_identity(thread_id, run_id)
        stream = NativeRunStream._create(
            identity,
            partial(
                self._open_native_prepared,
                identity,
                input={"messages": []},
                mode="default",
                context=context,
                compaction=True,
                stream_mode=["messages", "tasks", "values", "custom"],
            ),
            coordinator=self._run_coordinator,
        )
        result: CompactionResult | None = None
        try:
            async for part in stream:
                canonical = stream._take_frame(part).canonical
                if isinstance(canonical, NativeValuesStreamPart) and canonical.ns == ():
                    payload = canonical.data.get(COMPACTION_STATE_KEY)
                    if payload is not None:
                        result = CompactionResult.model_validate(payload)
        finally:
            await stream.aclose()
        if result is None:
            raise TinkerFinLifecycleError("compression completed without a result")
        return result

    @property
    def namespace(self) -> str:
        """Return the immutable namespace selected when this Runtime was built."""

        return self._namespace

    def thread_identity(self, thread_id: str) -> ThreadIdentity:
        """Identify a thread in this Runtime's namespace."""

        return ThreadIdentity(namespace=self._namespace, thread_id=thread_id)

    def run_identity(self, thread_id: str, run_id: str) -> RunIdentity:
        """Identify a run in this Runtime's namespace."""

        return RunIdentity(
            namespace=self._namespace, thread_id=thread_id, run_id=run_id
        )

    def open_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        input: InputAgentState | Command[object] | None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: ContextT | None = None,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        debug: bool | None = None,
    ) -> NativeRunStream:
        """Create a lazy Native run; preparation begins at preflight or first pull.

        Args:
            thread_id: Application thread identifier within this Runtime's namespace.
            run_id: Identifier of this run within its thread.
            input: New Agent state, a native command, or None to continue checkpointed
                work without new input. None does not answer pending approvals.
            mode: Optional execution or Plan mode for new input.
            config: Optional graph execution settings.
            context: Context matching the schema supplied to build().
            on_native_part: Optional asynchronous observation of each validated part.
            stream_mode: Optional native output selection supported by the integration.
            print_mode: Optional upstream diagnostic output.
            interrupt_before: Nodes to pause before executing.
            interrupt_after: Nodes to pause after executing.
            durability: Checkpoint persistence policy.
            control: Optional cooperative execution control.
            debug: Whether to enable upstream diagnostics.

        Returns:
            A single-use Native stream. Consume it under aclosing or close it explicitly.

        Raises:
            TypeError: Identifiers or callbacks have invalid types.
            ValueError: Identity values or the selected mode are invalid.
        """
        stream_options: dict[str, object] = {
            "stream_mode": stream_mode,
            "print_mode": print_mode or None,
            "interrupt_before": interrupt_before,
            "interrupt_after": interrupt_after,
            "durability": durability,
            "control": control,
            "debug": debug,
        }

        identity = self.run_identity(thread_id, run_id)
        self._validate_run_binding(identity=identity, on_part=on_native_part)
        resolve_agent_mode(mode, options=self._plan_options)
        bound = self._bind_native_invocation(
            self._runtime_profile.astream_signature,
            (input, config),
            {key: value for key, value in stream_options.items() if value is not None},
            identity=identity,
        )
        return NativeRunStream._create(
            identity,
            partial(
                self._open_native_prepared,
                identity,
                input=bound.arguments["input"],
                mode=mode,
                config=config,
                context=context,
                on_native_part=on_native_part,
                **{
                    key: value
                    for key, value in stream_options.items()
                    if value is not None
                },
            ),
            coordinator=self._run_coordinator,
        )

    async def ainvoke(
        self,
        *,
        thread_id: str,
        run_id: str,
        input: InputAgentState | Command[object] | None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: ContextT | None = None,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        debug: bool | None = None,
    ) -> Mapping[str, object]:
        """Execute one run and return its final state after resource cleanup.

        Args:
            thread_id: Application thread identifier in this Runtime's namespace.
            run_id: Identifier of this run within its thread.
            input: Agent state, native continuation command, or no new input.
            mode: Optional execution or Plan mode for new input.
            config: Optional graph execution settings.
            context: Context matching the schema supplied to build().
            on_native_part: Optional asynchronous observer of validated Native parts.
            stream_mode: Optional native output selection supported by the integration.
            print_mode: Optional upstream diagnostic output.
            interrupt_before: Nodes to pause before executing.
            interrupt_after: Nodes to pause after executing.
            durability: Checkpoint persistence policy.
            control: Optional cooperative execution control.
            debug: Whether to enable upstream diagnostics.

        Returns:
            The final root state, including interrupts when awaiting human input.

        Raises:
            TinkerFinLifecycleError: Execution produced no final root state.
            BaseException: Execution, observation, cancellation, or cleanup fails.
        """
        stream = self.open_run(
            thread_id=thread_id,
            run_id=run_id,
            input=input,
            mode=mode,
            config=config,
            context=context,
            on_native_part=on_native_part,
            stream_mode=stream_mode,
            print_mode=print_mode,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            durability=durability,
            control=control,
            debug=debug,
        )
        state: dict[str, object] | None = None
        pending: dict[str, NativeRuntimeInterrupt] = {}
        try:
            async for part in stream:
                canonical = stream._take_frame(part).canonical
                if (
                    not isinstance(canonical, NativeValuesStreamPart)
                    or canonical.ns != ()
                ):
                    continue
                state = dict(canonical.data)
                pending.update((item.id, item) for item in canonical.interrupts)
        finally:
            await stream.aclose()
        if state is None:
            raise TinkerFinLifecycleError(
                "managed invocation completed without a root values boundary"
            )
        if pending:
            state["__interrupt__"] = list(pending.values())
        return state

    @overload
    def open_agui_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        messages: Sequence[UserMessage | Mapping[str, object]],
        input: None = None,
        resume: None = None,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: ContextT | None = None,
        stream_timeout: float | None = None,
        cleanup_timeout: float | None = None,
        include_reasoning_events: bool = False,
        include_subagent_events: bool = True,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        on_agui_event: EventObserver | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        debug: bool | None = None,
    ) -> AgUiRunStream: ...

    @overload
    def open_agui_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        messages: None = None,
        input: InputAgentState,
        resume: None = None,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: ContextT | None = None,
        stream_timeout: float | None = None,
        cleanup_timeout: float | None = None,
        include_reasoning_events: bool = False,
        include_subagent_events: bool = True,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        on_agui_event: EventObserver | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        debug: bool | None = None,
    ) -> AgUiRunStream: ...

    @overload
    def open_agui_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        messages: None = None,
        input: None = None,
        resume: AgUiResumeRequest,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: ContextT | None = None,
        on_resume_saved: AgUiResumeCheckpointObserver | None = None,
        on_resume_not_saved: AgUiResumeNotSavedObserver | None = None,
        stream_timeout: float | None = None,
        cleanup_timeout: float | None = None,
        include_reasoning_events: bool = False,
        include_subagent_events: bool = True,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        on_agui_event: EventObserver | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        debug: bool | None = None,
    ) -> AgUiRunStream: ...

    def open_agui_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        messages: Sequence[UserMessage | Mapping[str, object]] | None = None,
        input: InputAgentState | None = None,
        resume: AgUiResumeRequest | None = None,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: ContextT | None = None,
        on_resume_saved: AgUiResumeCheckpointObserver | None = None,
        on_resume_not_saved: AgUiResumeNotSavedObserver | None = None,
        stream_timeout: float | None = None,
        cleanup_timeout: float | None = None,
        include_reasoning_events: bool = False,
        include_subagent_events: bool = True,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        on_agui_event: EventObserver | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        debug: bool | None = None,
    ) -> AgUiRunStream:
        """Create a lazy AG-UI run for messages, explicit input, or resume decisions.

        Args:
            thread_id: Application thread identifier in this Runtime's namespace.
            run_id: Identifier of this run within its thread.
            messages: Host-authorized user messages for ordinary conversation.
            input: Explicit graph state, exclusive with messages and resume.
            resume: Human decisions for an interrupted run, exclusive with other inputs.
            parent_run_id: Optional related run in the same namespace and thread.
            mode: Optional execution or Plan mode for new input.
            config: Optional graph execution settings.
            context: Context matching the schema supplied to build().
            on_resume_saved: Optional settlement after resume evidence is durable.
            on_resume_not_saved: Optional settlement when no resume evidence was saved.
            stream_timeout: Optional total Native pull deadline in seconds.
            cleanup_timeout: Optional wait limit for protected cleanup in seconds.
            include_reasoning_events: Whether to emit supported public reasoning events.
            include_subagent_events: Whether to emit subagent events.
            on_native_part: Optional asynchronous observer of validated Native parts.
            on_agui_event: Optional asynchronous observer of outgoing AG-UI events.
            stream_mode: Optional native output selection supported by the integration.
            print_mode: Optional upstream diagnostic output.
            interrupt_before: Nodes to pause before executing.
            interrupt_after: Nodes to pause after executing.
            durability: Checkpoint persistence policy.
            control: Optional cooperative execution control.
            debug: Whether to enable upstream diagnostics.

        Returns:
            A single-use event stream suitable for direct consumption or channel.open_sse().

        Raises:
            TypeError: Identifiers, decisions, or callbacks have invalid types.
            ValueError: Inputs conflict or resume callbacks accompany ordinary input.
        """
        stream_options: dict[str, object] = {
            "stream_mode": stream_mode,
            "print_mode": print_mode or None,
            "interrupt_before": interrupt_before,
            "interrupt_after": interrupt_after,
            "durability": durability,
            "control": control,
            "debug": debug,
        }

        require_agui()
        from tinkerfin_agui_adapter import AgUiLifecycleEventFactory

        from .agui_resume import AgUiResumeRequest as ResumeRequest

        identity = self.run_identity(thread_id, run_id)
        self._validate_run_binding(identity=identity, on_part=on_native_part)
        if sum(value is not None for value in (messages, input, resume)) != 1:
            raise ValueError(
                "exactly one of messages, input or resume must be provided"
            )
        if resume is not None and not isinstance(resume, ResumeRequest):
            raise TypeError("resume must be an AgUiResumeRequest")
        if resume is None and (
            on_resume_saved is not None or on_resume_not_saved is not None
        ):
            raise ValueError("resume settlement callbacks require resume input")
        for name, callback in (
            ("on_resume_saved", on_resume_saved),
            ("on_resume_not_saved", on_resume_not_saved),
            ("on_agui_event", on_agui_event),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be an async callable")
        _validate_timeout(stream_timeout, name="stream_timeout")
        _validate_timeout(cleanup_timeout, name="cleanup_timeout")
        resolve_agent_mode(mode, options=self._plan_options)
        AgUiLifecycleEventFactory.validate_parent_run_id(
            parent_run_id, identity=identity
        )
        bound = self._bind_native_invocation(
            self._runtime_profile.astream_signature,
            (input, config),
            {key: value for key, value in stream_options.items() if value is not None},
            identity=identity,
        )
        return AgUiRunStream._create(
            identity,
            partial(
                self._open_agui_prepared,
                identity,
                messages=messages,
                input=bound.arguments["input"],
                resume=resume,
                parent_run_id=parent_run_id,
                mode=mode,
                config=config,
                context=context,
                on_resume_saved=on_resume_saved,
                on_resume_not_saved=on_resume_not_saved,
                stream_timeout=stream_timeout,
                cleanup_timeout=cleanup_timeout,
                include_reasoning_events=include_reasoning_events,
                include_subagent_events=include_subagent_events,
                on_native_part=on_native_part,
                on_agui_event=on_agui_event,
                **{
                    key: value
                    for key, value in stream_options.items()
                    if value is not None
                },
            ),
            coordinator=self._run_coordinator,
        )

    async def _open_native_prepared(
        self,
        identity: RunIdentity,
        owner: RunOwner,
        *,
        input: InputAgentState | Command[object] | None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: object | None = None,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        compaction: bool = False,
        **stream_options: object,
    ) -> NativeGraphRunStream:
        """Prepare one bound Native execution after admission."""

        async with RunResources(owner) as resources:
            from .plan._state import PLAN_PRIVATE_STATE_KEYS

            self._validate_run_binding(identity=identity, on_part=on_native_part)
            requested_mode = (
                self._plan_options.default_mode
                if mode is None and self._plan_options is not None
                else ("default" if mode is None else mode)
            )
            resolved_mode = validate_agent_mode(requested_mode, name="mode")
            try:
                owner.require_admission()
                definition = self._definition
                stream = await definition._open_native_run(
                    identity=identity,
                    resources=resources,
                    input=input,
                    mode=mode,
                    config=config,
                    context=context,
                    on_part=on_native_part,
                    stream_options=stream_options,
                    compaction=compaction,
                )
                stream._adopt_resources(resources)
                await stream._ready()
                return stream
            except Exception as error:  # noqa: BLE001 - Runtime owns failed Observation
                setup_error = error
                input_kind = "compaction" if compaction else native_input_kind(input)
                source = source_context(
                    identity=identity,
                    runtime_profile=self._runtime_profile.profile_id,
                    input_kind=input_kind,
                    parent_run_id=None,
                    mode=resolved_mode,
                    graph_input=input,
                    config={} if config is None else config,
                    private_state_keys=frozenset({"_tinkerfin_resume"})
                    | PLAN_PRIVATE_STATE_KEYS,
                )
                observation = self._observation_hub(
                    source, initialization_error=setup_error
                )

                async def failed_source() -> AsyncIterator[Mapping[str, object]]:
                    if False:  # pragma: no cover - establish async iterator shape
                        yield {}
                    if compaction:
                        await CompactionOperation("manual").fail(setup_error)
                    raise setup_error

                failed_stream = self._run_native(
                    failed_source,
                    identity=identity,
                    on_part=on_native_part,
                    observation=observation,
                )
                failed_stream.error = setup_error
                failed_stream._adopt_resources(resources)
                await failed_stream._ready()
                return failed_stream

    async def _open_agui_prepared(
        self,
        identity: RunIdentity,
        owner: RunOwner,
        *,
        messages: Sequence[UserMessage | Mapping[str, object]] | None = None,
        input: InputAgentState | None = None,
        resume: AgUiResumeRequest | None = None,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: object | None = None,
        on_resume_saved: AgUiResumeCheckpointObserver | None = None,
        on_resume_not_saved: AgUiResumeNotSavedObserver | None = None,
        stream_timeout: float | None = None,
        cleanup_timeout: float | None = None,
        include_reasoning_events: bool = False,
        include_subagent_events: bool = True,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        on_agui_event: EventObserver | None = None,
        compaction: bool = False,
        **stream_options: object,
    ) -> AgUiEventStream:
        """Prepare one bound AG-UI execution and settle resume failures."""

        async with RunResources(owner) as resources:
            require_agui()

            definition = self._definition
            settlement_checkpointer = (
                None if resume is None else definition._resume_checkpointer()
            )
            settlement_authority_proven = settlement_checkpointer is not None
            requested_mode = (
                self._plan_options.default_mode
                if mode is None and self._plan_options is not None
                else ("default" if mode is None else mode)
            )
            resolved_mode = validate_agent_mode(requested_mode, name="mode")
            try:
                owner.require_admission()
                if messages is not None:
                    from .agui_input import _user_messages_to_input

                    input = _user_messages_to_input(messages)
                stream = await definition._open_agui_run(
                    identity=identity,
                    resources=resources,
                    input=input,
                    resume_request=resume,
                    parent_run_id=parent_run_id,
                    mode=mode,
                    config=config,
                    context=context,
                    on_resume_checkpointed=on_resume_saved,
                    on_resume_not_saved=on_resume_not_saved,
                    timeout=stream_timeout,
                    settlement_timeout=cleanup_timeout,
                    expose_reasoning_events=include_reasoning_events,
                    expose_subagent_events=include_subagent_events,
                    on_part=on_native_part,
                    on_event=on_agui_event,
                    stream_options=stream_options,
                    compaction=compaction,
                )
                stream._adopt_resources(resources)
                await stream._ready()
                return stream
            # Resume claim settlement covers every setup exit before a request Runtime can
            # install its own marker guard. Ordinary failures become one AG-UI error
            # lifecycle; cancellation and process control settle first and then propagate.
            except BaseException as error:
                if (
                    resume is not None
                    and on_resume_not_saved is not None
                    and settlement_authority_proven
                ):
                    try:
                        await self._settle_resume_not_saved(
                            identity=identity,
                            checkpointer=settlement_checkpointer,
                            callback=on_resume_not_saved,
                        )
                    except BaseException as settlement_error:  # noqa: BLE001 - retain both initialization and resume-release failures
                        selected = select_failure(
                            error,
                            settlement_error,
                            label="resume not-saved settlement also failed",
                        )
                        if selected is not error:
                            raise selected
                if not isinstance(error, Exception):
                    raise
                failed_stream = self._failed_agui_run(
                    error,
                    identity=identity,
                    parent_run_id=parent_run_id,
                    mode=resolved_mode,
                    input=input,
                    config=config,
                    resume_request=resume,
                    compaction=compaction,
                )
                failed_stream._adopt_resources(resources)
                await failed_stream._ready()
                # Keep construction failures in the owning preparation scope until
                # observation startup succeeds. No await separates this handoff from
                # returning the stream that retains the original error.
                failures, _owns_failures = OwnedOperationFailures.for_source()
                failures.transfer(error)
                return failed_stream

    async def _settle_resume_not_saved(
        self,
        *,
        identity: RunIdentity,
        checkpointer: object | None,
        callback: AgUiResumeNotSavedObserver,
    ) -> None:
        """Probe and settle one pre-marker claim as a single owned operation."""

        from ._agui_lineage import agui_resume_marker_is_durable

        if checkpointer is None:
            raise TinkerFinLifecycleError(
                "resume marker state is unknowable without a concrete checkpointer"
            )

        async def settle() -> None:
            if await agui_resume_marker_is_durable(
                checkpointer,
                identity=identity,
                runtime_profile=self._runtime_profile,
            ):
                return
            await callback()

        task = asyncio.create_task(
            settle(),
            name="tinkerfin-resume-not-saved-settlement",
        )
        await join_task(task)

    def _failed_agui_run(
        self,
        error: Exception,
        *,
        identity: RunIdentity,
        parent_run_id: str | None = None,
        mode: AgentMode = "default",
        input: object = None,
        config: object = None,
        resume: AgUiResumeBinding | None = None,
        resume_request: AgUiResumeRequest | None = None,
        compaction: bool = False,
    ) -> AgUiEventStream:
        """Create one observed AG-UI lifecycle for a pre-Graph setup failure.

        The Runtime records the input, failed terminal, and close when preparation
        fails before execution. A resume request preserves the input kind even when
        checkpoint resolution fails before a validated binding exists.

        Args:
            error: Original setup failure retained as trusted causal evidence.
            identity: Canonical identity already accepted by the host.
            parent_run_id: Optional branch or resume lineage within the same thread.
            mode: Requested default or Plan route.
            input: Ordinary Graph input available before setup failed.
            config: Graph configuration available before setup failed.
            resume: Validated resume binding when the failed request was a resume.
            resume_request: Untrusted framework-owned resume intent when binding
                resolution failed. It is never converted into a native command or
                treated as validated checkpoint evidence.
            compaction: Whether the failed preparation belongs to context compression.

        Returns:
            A single-use AG-UI stream with one standard initialization error terminal.

        Raises:
            TypeError: An argument has the wrong public type.
            ValueError: Parent lineage, mode, or resume arguments are invalid.
        """

        require_agui()
        from tinkerfin_agui_adapter import AgUiLifecycleEventFactory

        from .agui_resume import AgUiResumeBinding as AgUiResumeBindingType
        from .agui_resume import AgUiResumeRequest as AgUiResumeRequestType

        if not isinstance(error, Exception):
            raise TypeError("error must be an Exception")
        self._validate_run_binding(identity=identity, on_part=None)
        AgUiLifecycleEventFactory.validate_parent_run_id(
            parent_run_id,
            identity=identity,
        )
        resolved_mode = validate_agent_mode(mode, name="mode")
        if resume is not None and not isinstance(resume, AgUiResumeBindingType):
            raise TypeError("resume must be an AgUiResumeBinding or None")
        if resume_request is not None and not isinstance(
            resume_request,
            AgUiResumeRequestType,
        ):
            raise TypeError("resume_request must be an AgUiResumeRequest or None")
        if resume is not None and resume_request is not None:
            raise ValueError("resume and resume_request are mutually exclusive")
        input_kind = (
            "compaction"
            if compaction
            else (
                resume.mode
                if resume is not None
                else (
                    "resume"
                    if resume_request is not None
                    else ("branch" if parent_run_id is not None else "ordinary")
                )
            )
        )
        source_input = (
            resume.model_dump(mode="json", by_alias=True)
            if resume is not None
            else (
                resume_request.model_dump(mode="json", by_alias=True)
                if resume_request is not None
                else input
            )
        )
        context = source_context(
            identity=identity,
            runtime_profile=self._runtime_profile.profile_id,
            input_kind=input_kind,
            parent_run_id=parent_run_id,
            mode=resolved_mode,
            graph_input=source_input,
            config={} if config is None else config,
            private_state_keys=frozenset(),
            resume=() if resume is None else resume._observation_summaries(),
        )
        observation = self._observation_hub(context, initialization_error=error)

        async def failed_parts() -> AsyncIterator[Mapping[str, object]]:
            if False:  # pragma: no cover - supplies the async iterator shape
                yield {}
            if compaction:
                await CompactionOperation("manual").fail(error)
            raise error

        stream = self._run_agui(
            failed_parts,
            identity=identity,
            parent_run_id=parent_run_id,
            on_part=None,
            timeout=None,
            settlement_timeout=None,
            expose_reasoning_events=False,
            expose_subagent_events=True,
            prior_tool_call_ids=frozenset(),
            private_state_keys=frozenset(),
            on_event=None,
            observation=observation,
        )
        stream._runtime_error_code = "runtime_initialization_error"
        stream._initialization_failed = True
        stream.error = error
        return stream

    def _validate_run_binding(
        self,
        *,
        identity: RunIdentity | None,
        on_part: object | None,
    ) -> None:
        """Validate a request binding without opening Graph or source resources."""

        coordinator = self._run_coordinator
        if identity is not None and not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity or None")
        if coordinator is not None and identity is None:
            raise ValueError("identity is required when run_coordinator is configured")
        if identity is not None and identity.namespace != self._namespace:
            raise ValueError("run identity belongs to another namespace")
        if on_part is not None and not callable(on_part):
            raise TypeError("on_part must be an async callable or None")

    def _bind_native_invocation(
        self,
        signature: inspect.Signature,
        args: tuple[object, ...],
        options: Mapping[str, object],
        *,
        identity: RunIdentity,
    ) -> inspect.BoundArguments:
        """Bind execution and observation to the same user message identities."""

        bound = self._runtime_profile.stream_driver.bind_invocation(
            signature,
            args,
            options,
            identity=identity,
            runtime_profile=self._runtime_profile.profile_id,
        )
        graph_input = bound.arguments.get("input")
        if isinstance(graph_input, dict):
            raw_messages = graph_input.get("messages")
            if isinstance(raw_messages, list):
                messages = list(raw_messages)
                for index, message in enumerate(convert_to_messages(raw_messages)):
                    if isinstance(message, HumanMessage):
                        message = message_chunk_to_message(message)
                        # LangGraph add_messages assigns IDs after RunInput capture.
                        # Normalize user input and assign missing IDs without mutating
                        # callers so tracing and state refer to the same message.
                        messages[index] = (
                            message.model_copy(update={"id": str(uuid4())})
                            if message.id is None
                            else message
                        )
                bound.arguments["input"] = {**graph_input, "messages": messages}
        return bound

    def _run_native(
        self,
        source_factory: Callable[[], AsyncIterator[Mapping[str, object]]],
        *,
        identity: RunIdentity,
        on_part: PartObserver[Mapping[str, object]] | None,
        observation: RuntimeObservationHub,
        on_settle: Callable[[], Awaitable[None]] | None = None,
        source_preflight: Callable[[], Awaitable[object]] | None = None,
    ) -> NativeGraphRunStream:
        """Create one canonical stream bound to the selected Runtime Profile."""

        self._validate_run_binding(identity=identity, on_part=on_part)
        coordinator = self._run_coordinator
        return NativeGraphRunStream(
            source_factory=source_factory,
            source_preflight=source_preflight,
            coordination_factory=(
                None if coordinator is None else lambda: coordinator(identity)
            ),
            identity=identity,
            on_part=on_part,
            observation=observation,
            stream_driver=self._runtime_profile.stream_driver,
            on_settle=on_settle,
        )

    def _run_agui(
        self,
        source_factory: Callable[[], AsyncIterator[Mapping[str, object]]],
        *,
        identity: RunIdentity,
        parent_run_id: str | None,
        on_part: PartObserver[Mapping[str, object]] | None,
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        prior_tool_call_ids: frozenset[str],
        private_state_keys: frozenset[str],
        on_event: EventObserver | None,
        observation: RuntimeObservationHub,
        on_settle: Callable[[], Awaitable[None]] | None = None,
        source_preflight: Callable[[], Awaitable[object]] | None = None,
    ) -> AgUiEventStream:
        """Convert one framework-bound native source into an AG-UI event stream."""

        if on_event is not None and not callable(on_event):
            raise TypeError("on_event must be an async callable or None")
        native = self._run_native(
            source_factory,
            identity=identity,
            on_part=on_part,
            observation=observation,
            on_settle=on_settle,
            source_preflight=source_preflight,
        )
        return AgUiEventStream(
            parts=native,
            identity=identity,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=prior_tool_call_ids,
            private_state_keys=private_state_keys,
            on_event=on_event,
            parent_run_id=parent_run_id,
            native_frame_resolver=native._take_frame,
        )

    def _observation_hub(
        self,
        context: RunSourceContext,
        *,
        initialization_error: Exception | None = None,
    ) -> RuntimeObservationHub:
        """Bind observers and preserve one known pre-Graph initialization failure."""

        return RuntimeObservationHub(
            context=context,
            observers=self._observers,
            initialization_error=initialization_error,
        )


__all__ = [
    "AgUiEventStream",
    "AgUiRunStream",
    "AgUiSettlementTimeoutError",
    "AgentRuntime",
    "EventObserver",
    "NativeGraphRunStream",
    "NativeRunStream",
    "NativeStreamPart",
    "PartObserver",
    "SseBody",
    "SseEventIdResolver",
    "SseMapper",
    "SsePayload",
    "SsePreflight",
    "TinkerFin",
]
